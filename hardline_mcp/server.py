"""FastMCP server exposing inter-agent messaging tools.

The only module that imports ``mcp``. It wires the pure logic in
:mod:`hardline_mcp.mailbox` (durable SQLite mailbox) and
:mod:`hardline_mcp.adapters` (native per-agent push/query) into MCP tools.

Every tool is ``async def`` and runs its blocking body (SQLite I/O,
subprocess spawns) in a worker thread via ``anyio.to_thread.run_sync`` — the
installed FastMCP invokes sync tools directly on the asyncio event loop, so a
plain ``def`` tool would block the whole server (pings included) for the
duration of every DB write or ``ask_*`` agent spawn.

Identity is self-declared (``from_agent`` on ``send``): there's no OS-level
way for an MCP server to verify which agent is calling, and every process
runs as the same user on one machine, so this is accepted-risk convention —
the same posture as the sibling vram-mcp's claim ledger.
"""

from __future__ import annotations

import atexit
import functools
import json
import os
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Literal

import anyio.to_thread
from mcp.server.fastmcp import FastMCP

from . import adapters, jobs, mailbox, sessions

mcp = FastMCP("hardline-mcp")

ClaudeEffort = Literal["default", "low", "medium", "high", "xhigh", "max"]
ClaudeMode = Literal["default", "advisory"]
CodexEffort = Literal["default", "low", "medium", "high", "xhigh", "max", "ultra"]
CodexMode = Literal["default", "advisory"]

# ask_*_async fire real ask_claude/ask_codex subprocess calls (each bounded by
# its own _CLAUDE_TIMEOUT_S/_CODEX_TIMEOUT_S) in the background.
# A bare `threading.Thread` per call has no ceiling - repeated dispatches (a
# runaway caller, or several concurrent write=True requests) would pile up
# unbounded concurrent agent subprocesses. Route through a small fixed-size
# pool instead: excess dispatches queue rather than spawning without limit.
# Override for local tuning; not exposed as a per-call parameter since it
# bounds the whole hardline-mcp process, not one request.
_ASYNC_MAX_WORKERS = adapters.positive_int_env("HARDLINE_ASYNC_MAX_WORKERS", 4)

# How long ask_*_async waits before calling a dispatch "started". Long enough
# to catch an arrive-and-die failure (a rejected model returns in well under a
# second), short enough that a real dispatch still returns promptly. Runs off
# the event loop via _in_thread, so it delays only its own caller.
_ASYNC_EARLY_FAILURE_S = 2.0

# Per-message body cap for a batched inbox read. Bounding the message COUNT is
# not enough on its own: a single async result here has reached 35k characters,
# so a small batch of them still lands tens of thousands of tokens in the
# caller's context. Truncation is display-only - the row is untouched and
# peek(message_id) returns the body whole.
_MAX_BODY_CHARS = 4000
_TRUNCATION_NOTE = "\n...[{n} characters truncated - call peek(message_id={mid}) for the full body]"

# Ceiling on a WHOLE response, not just one body. Capping per-message was not
# enough: a 50-row history page of 4000-char bodies is ~200KB, which the host
# then truncated itself - so the response was cut anyway and the `truncated`
# flag understated it. Bound the aggregate and the per-body cap becomes an
# upper bound rather than the actual size.
_MAX_RESPONSE_CHARS = 60_000
# Never shrink a body below this; past a point an excerpt stops carrying any
# signal and peek() is the honest answer instead.
_MIN_BODY_CHARS = 400
# Rough size of the appended truncation note. Budgeted for rather than added
# on top, so an aggregate cap is not quietly exceeded by its own bookkeeping.
_NOTE_OVERHEAD = 100
# Per-row cap on a job result inside a LISTING. job_result() returns one whole.
_JOB_RESULT_PREVIEW_CHARS = 600

_async_executor = ThreadPoolExecutor(
    max_workers=_ASYNC_MAX_WORKERS, thread_name_prefix="hardline-async"
)


def _shorten(msg: dict, cap: int) -> dict:
    """Return a copy of ``msg`` with its body capped at ``cap`` characters.

    Copies rather than mutating in place - the caller's dicts come straight
    from the db layer and nothing downstream should see a body that silently
    lost content. The stored row is never touched.
    """
    body = msg.get("body") or ""
    if len(body) <= cap:
        return msg
    out = dict(msg)
    out["body"] = body[:cap] + _TRUNCATION_NOTE.format(
        n=len(body) - cap, mid=msg.get("message_id")
    )
    out["body_truncated"] = True
    out["body_length"] = len(body)
    return out


def _fit_response(
    messages: list[dict],
    *,
    budget: int = _MAX_RESPONSE_CHARS,
    allow_drop: bool,
) -> tuple[list[dict], int, int]:
    """Fit a batch inside an aggregate character budget.

    Returns ``(messages, truncated, dropped)``.

    Two strategies, because the two callers have different obligations:

    ``allow_drop=False`` (inbox) SHRINKS. A consuming read must hand back
    every message it consumed - dropping one would lose it from the caller's
    view while the mailbox considers it delivered - so the per-body cap is
    lowered until the batch fits, floored at ``_MIN_BODY_CHARS``.

    ``allow_drop=True`` (history) TRUNCATES THE PAGE. Nothing is consumed, so
    the honest response is a shorter page plus a cursor to continue from.
    """
    if not messages:
        return messages, 0, 0

    cap = _MAX_BODY_CHARS
    fitted = [_shorten(m, cap) for m in messages]
    total = sum(len(m.get("body") or "") for m in fitted)

    if total > budget and not allow_drop:
        # Shrink every body to an equal share, floored so an excerpt still
        # carries signal.
        #
        # The share must pay for the truncation NOTE too. It is appended after
        # the cap, so budgeting on the cap alone overshoots by ~80 chars per
        # message - small individually, and exactly the kind of drift that put
        # the per-body cap over the host's limit in the first place.
        share = budget // max(1, len(messages)) - _NOTE_OVERHEAD
        cap = min(cap, max(_MIN_BODY_CHARS, share))
        fitted = [_shorten(m, cap) for m in messages]

    dropped = 0
    if allow_drop:
        kept: list[dict] = []
        running = 0
        for msg in fitted:
            size = len(msg.get("body") or "")
            if kept and running + size > budget:
                break
            kept.append(msg)
            running += size
        dropped = len(fitted) - len(kept)
        fitted = kept

    truncated = sum(1 for m in fitted if m.get("body_truncated"))
    return fitted, truncated, dropped


def _drain_async_executor_at_exit() -> None:
    """Drop queued-but-unstarted dispatches so shutdown isn't unbounded.

    ThreadPoolExecutor's workers are non-daemon and it joins every one of
    them at interpreter shutdown, so a full queue would run to completion
    before the process could exit - serially, each up to its configured agent
    timeout. The per-call ``threading.Thread(daemon=True)`` this pool
    replaced was never joined and exited instantly, so that ceiling is a
    regression this restores: teardown is now bounded by the longest
    *already-running* call rather than by everything still queued behind
    it. A running dispatch is deliberately still awaited - its subprocess
    can't be interrupted safely mid-call. A queued one never started, so
    the mailbox result it would have written isn't owed to anyone.

    Registered through threading's registry, not ``atexit``: verified
    empirically that ``threading._shutdown()`` (where ThreadPoolExecutor's
    own joining hook lives) runs *before* ``atexit`` callbacks, so an
    ``atexit`` registration here is a silent no-op - the join has already
    happened by the time it fires. Registering after ``concurrent.futures``
    is imported puts this ahead of that hook, since the registry is LIFO.
    The API is private, so treat its absence on some future Python as
    merely losing this optimization rather than an import-time crash.
    """
    _async_executor.shutdown(wait=False, cancel_futures=True)


_register_threading_atexit = getattr(threading, "_register_atexit", None)
if _register_threading_atexit is not None:  # pragma: no branch - present on 3.10+
    _register_threading_atexit(_drain_async_executor_at_exit)
else:  # pragma: no cover - only on a Python that dropped the private hook
    atexit.register(_drain_async_executor_at_exit)


async def _in_thread(fn, *args, **kwargs):
    """Run a blocking tool body off the event loop."""
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


# ── session registry ─────────────────────────────────────────────────────────

def _announce_self(agent: str | None = None, label: str | None = None) -> str | None:
    """Record this process in the session registry; return the lane, or None.

    Writes every time rather than caching "already registered". A row can
    vanish while its session is very much alive - a liveness probe that fails
    for an unrelated reason, a store rebuilt underneath the running fleet, an
    operator clearing the table - and a process that believed it had registered
    once would stay invisible for the rest of its life: unaddressable, and
    reported to senders as a lane nobody holds. Re-announcing is one small
    UPDATE, and this is called at startup and from ``list_agents``, not from
    the polling path.

    Does nothing when the agent cannot be determined: a Codex or Hermes session
    is genuinely anonymous until it calls ``register_session``, and inventing a
    name would put a destination in the registry that nobody can be sure how to
    reach.

    A session holding no lane registers nothing either, but that needs no guard
    here - it owns no recipients, so there is nothing to write, and ``register``
    returns early. Worth knowing because it is load-bearing: a default-mode
    ``ask_codex`` spawns Codex with the operator's real config, so a one-shot
    review subprocess loads its own hardline. Those have no lane, so they cannot
    appear as live sessions however many of them run.
    """
    agent = agent or adapters.self_agent()
    if not agent:
        return None
    lane = adapters.lane_for(agent)
    try:
        # EVERY held lane, not just the current name. The process consumes mail
        # for all of them, and a registry that recorded only the newest would
        # report the older ones unheld - so they would read as dead, senders
        # would be told nobody could receive them, and another session could
        # claim one out from under this still-consuming process.
        sessions.register(
            agent=agent, lanes=adapters.owned_recipients(agent), label=label
        )
    except Exception:  # noqa: BLE001 - discovery is a convenience, never
        # a reason to fail the call the caller actually made.
        traceback.print_exc()
        return None
    return lane


def _lane_advice(to_agent: str) -> str | None:
    """Warn when a lane-qualified send has no live session to receive it.

    A lane-qualified message is consumable ONLY by the process holding that
    lane, so addressing one nobody holds does not mean "delivered late" - it
    means never. That is how 51 messages accumulated across 11 dead lanes.

    A warning rather than a rejection, deliberately: a Claude lane is keyed on
    the session id and survives a ``/mcp`` reconnect, so a session that is
    momentarily between hardline processes will legitimately come back to the
    same lane and collect its mail. Refusing would break that. The message
    still persists either way; the caller is simply told what it just did.
    """
    if ":" not in to_agent:
        return None
    try:
        if sessions.holders(to_agent):
            return None
        live = [s["lane"] for s in sessions.live(agent=adapters.base_agent(to_agent))]
    except Exception:  # noqa: BLE001 - never fail a send over discovery
        return None
    known = ", ".join(sorted(live)) if live else "none"
    return (
        f"no live session holds {to_agent!r}, so nothing can consume this "
        f"message unless that exact lane comes back (a Claude lane survives a "
        f"/mcp reconnect; a claimed one does not outlive its process). "
        f"Live lanes for {adapters.base_agent(to_agent)!r}: {known}. "
        "Call list_agents() to see every live session."
    )


# ── mailbox tools ────────────────────────────────────────────────────────────


def _send_impl(from_agent: str, to_agent: str, message: str, deliver: bool) -> dict:
    # Reject unknown agents up front: a typo'd recipient would otherwise persist
    # forever, unread and undeliverable — a silent black hole. Validate before
    # writing anything. A lane-qualified name ("claude:fonts.1a2b3c4d") is
    # validated on its base, so lanes are addressable while "clod:x" is still
    # caught — the guard's actual purpose is preserved.
    known = adapters.known_agents()
    unknown = [a for a in (from_agent, to_agent) if adapters.base_agent(a) not in known]
    if unknown:
        return {
            "ok": False,
            "error": f"unknown agent(s) {unknown}; known: {sorted(known)}",
        }

    result = mailbox.send(from_agent, to_agent, message)
    result["ok"] = True
    # The base-name guard above catches "clod:x" but not "codex:typo": any lane
    # is accepted after a valid agent, which is the same silent black hole one
    # level down. The registry can finally tell the difference.
    advice = _lane_advice(to_agent)
    if advice:
        result["warning"] = advice
    if deliver:
        notice = (
            f"[hardline] new message #{result['message_id']} from {from_agent}. "
            f"Call hardline-mcp inbox(agent='{to_agent}') to read it."
        )
        # Push to the CLI behind the lane, not the lane name: adapters.deliver
        # dispatches on exact roster membership, so a qualified recipient
        # ("claude:fonts.1a2b3c4d") persisted fine and then failed delivery
        # with "unknown agent" - a half-succeeded send. The notice still
        # carries the full lane so the reader queries the right inbox.
        result["delivery"] = adapters.deliver(adapters.base_agent(to_agent), notice)
    return result


@mcp.tool()
async def send(
    from_agent: str, to_agent: str, message: str, deliver: bool = False
) -> dict:
    """Send a message from one agent to another.

    Always persists to the durable mailbox. If ``deliver`` is true, also pushes
    a one-shot notice to the recipient via its native mechanism (hermes chat /
    codex exec / claude -p) so it sees the message without polling.

    ``from_agent``/``to_agent`` are one of: claude, hermes, codex; an unknown
    agent is rejected. Returns ``{"ok": true, "message_id", "created_at"}``
    (plus ``delivery`` when ``deliver`` set), or ``{"ok": false, "error"}``.
    """
    return await _in_thread(_send_impl, from_agent, to_agent, message, deliver)


@mcp.tool()
async def inbox(
    agent: str,
    unread_only: bool = True,
    limit: int = mailbox.DEFAULT_INBOX_LIMIT,
    auto_ack: bool = True,
) -> dict:
    """Read messages addressed to ``agent``, oldest first — one bounded batch.

    Reads this session's own lane AND the shared unqualified name, so
    ``inbox(agent="claude")`` returns results dispatched by THIS session plus
    anything broadcast to every claude — without seeing other sessions'
    results. Nothing to opt into: the lane comes from the session that spawned
    this server. An already lane-qualified ``agent`` reads ONLY that lane.

    ``unread_only`` (default true) hides messages already ack'd.

    ``limit`` caps the batch (ceiling ``MAX_INBOX_LIMIT``) and ``auto_ack``
    (default true) consumes exactly what it returns, so each poll advances
    instead of re-reading the same backlog. Poll again while ``remaining``
    is non-zero.

    Two documented exceptions to "consumes what it returns":
    ``auto_ack`` has NO effect when ``unread_only`` is false — browsing
    re-serves acked rows, which cannot be consumed, so honouring it there
    would re-pin the caller on the same page forever. And a message
    qualified to another session's lane is shown but never consumed, the
    same ownership rule ``ack`` enforces.

    ``remaining`` counts only what THIS caller could consume, so reading a
    lane you do not own reports zero rather than looping forever.

    Bodies over ``_MAX_BODY_CHARS`` are truncated with a marker — call
    ``peek(message_id)`` for one message in full.

    Returns ``{"messages", "count", "remaining", "truncated"}``.
    """
    # An explicit lane means exactly that lane. Previously this still unioned
    # in the caller's OWN lane (lane_for strips the qualifier and re-adds this
    # process's), so inbox("claude:other") quietly returned this session's
    # messages too - contradicting the documented behavior and widening rather
    # than narrowing the read.
    if ":" in agent:
        agents = [agent]
    else:
        # EVERY lane this process holds, not just its current one. A session
        # that renamed itself is still owed the results it dispatched under its
        # previous name - their recipient was fixed at dispatch time.
        #
        # Qualified with the agent being READ, so a bare name still returns
        # broadcasts. Ownership below is qualified with what this process
        # actually IS, so asking for another agent's mailbox widens what is
        # shown but never what can be consumed.
        agents = [agent] + [
            lane for lane in adapters.owned_recipients(agent) if lane != agent
        ]
    msgs, remaining = await _in_thread(
        mailbox.inbox,
        agents,
        unread_only=unread_only,
        limit=limit,
        auto_ack=auto_ack,
        # Same identity the explicit ack tool uses, so a consuming read cannot
        # drain a lane this session does not own.
        owned=adapters.owned_recipients(),
    )
    msgs, truncated, _ = _fit_response(msgs, allow_drop=False)
    response = {
        "messages": msgs,
        "count": len(msgs),
        "remaining": remaining,
        "truncated": truncated,
    }
    if msgs:
        # Recovery cursor. A consuming read commits the ack before this
        # response can reach the caller, so if the enclosing response is lost
        # the batch is read but unseen. These ids make recovering it a
        # mechanical call rather than a search:
        #   history(agent=..., before_id=last_message_id + 1)
        # re-fetches exactly this batch, because history ignores acked_at.
        response["first_message_id"] = msgs[0]["message_id"]
        response["last_message_id"] = msgs[-1]["message_id"]
        response["recover_with"] = (
            f"history(agent={agent!r}, before_id={msgs[-1]['message_id'] + 1})"
        )
    return response


@mcp.tool()
async def list_agents() -> dict:
    """Who can be addressed, what names carry traffic, and who YOU are.

    Agent identity was undiscoverable by inspection: ``history`` filtered by a
    name that carries no traffic returns an empty list, which reads as "no
    messages" rather than "wrong name" — one agent searched its own display
    name before learning its mailbox identity was ``hermes``.

    ``agents`` is the dispatchable roster. ``live_sessions`` is who is actually
    running right now and can therefore receive lane-qualified mail.
    ``observed`` is every recipient the mailbox has ever seen, which is a
    HISTORY and not a destination list — each lane-qualified entry is marked
    ``live`` so the two are not confused. They were, for a long time: a dead
    session's lane looks exactly like a live one in the mailbox, so 11 of them
    were presented as places to send mail and 51 messages went there.

    ``you`` is this process's own identity — the answer to "what do I pass as
    from_agent", and now also "am I addressable individually".
    """
    await _in_thread(_announce_self)
    observed = await _in_thread(mailbox.recipients)
    seen_senders = await _in_thread(mailbox.senders)
    live = await _in_thread(sessions.live)
    # EVERY lane each session holds, not just the name it is currently
    # addressed by. A renamed session still consumes its previous lanes, so
    # collapsing to the current name here would mark them dead and count their
    # mail as stranded - undoing, in the reporting layer, the whole reason the
    # registry records more than one lane per session.
    live_lanes = {lane for s in live for lane in s["lanes"]}

    stale = 0
    for entry in observed:
        recipient = entry["recipient"]
        if ":" not in recipient:
            continue
        entry["live"] = recipient in live_lanes
        if not entry["live"] and entry.get("unread"):
            stale += 1

    suffix = adapters.lane_suffix()
    held = adapters.held_lanes()
    agent = adapters.self_agent()
    you: dict = {
        "agent": agent,
        "lane_suffix": suffix or None,
        "lane_for_claude": adapters.lane_for("claude"),
        "held_lanes": list(held),
        "note": (
            "Pass a bare roster name as from_agent; results are delivered "
            "to your lane automatically."
        ),
    }
    if not agent:
        you["addressable"] = False
        you["how_to_register"] = (
            "This process cannot tell which agent it serves, so it is absent "
            "from live_sessions and cannot be addressed individually. Call "
            "register_session(label='...', agent='codex'|'hermes'|'claude') to "
            "claim a name for it."
        )
    elif not suffix:
        you["addressable"] = False
        you["how_to_register"] = (
            f"Every {agent} session shares the unqualified name {agent!r}, so "
            "mail cannot be aimed at this one. Call register_session"
            "(label='...') to claim a lane."
        )
    else:
        you["addressable"] = True

    result = {
        "agents": list(adapters.known_agents()),
        "you": you,
        "live_sessions": live,
        "observed_recipients": observed,
        "observed_senders": seen_senders,
        "recipient_syntax": (
            "'<agent>' addresses everyone with that name; '<agent>:<lane>' "
            "addresses one session. inbox('<agent>') reads the bare name AND "
            "every lane you hold. Only a lane's holder may consume a "
            "lane-qualified message — so a lane with no live holder is a "
            "message nobody can ever read."
        ),
    }
    if stale:
        result["stale_lanes_note"] = (
            f"{stale} lane(s) below hold unread mail but have no live session. "
            "Those messages are unconsumable unless that exact lane returns."
        )
    return result


@mcp.tool()
async def register_session(label: str, agent: str | None = None) -> dict:
    """Claim ``label`` as this session's name, so mail can be aimed at it.

    Answers the question a static MCP registration cannot: a Codex or Hermes
    config supplies ONE env block to every session it launches, so
    ``HARDLINE_AGENT_LABEL`` cannot give two of them different names, and
    without a name every Codex session shares the unqualified identity
    ``codex``. Claiming one at runtime is per-session by construction.

    After this, ``send(to_agent="codex:construction", ...)`` reaches THIS
    session and only this session, and ``list_agents`` reports it as live.

    ``agent`` may be omitted where it is inferable (a Claude Code session, or
    ``HARDLINE_AGENT`` set in the registration); Codex and Hermes must pass it.

    The previous lane is kept, not surrendered: results already dispatched
    under it were addressed when the job started and must stay consumable.
    Renaming therefore never strands mail. Refused if a LIVE session already
    holds the name — a dead holder's claim is ignored, so a label does not
    become unusable forever because the session that used it crashed.
    """
    agent = (agent or adapters.self_agent() or "").strip().lower()
    if not agent:
        return {
            "ok": False,
            "error": (
                "cannot tell which agent this session serves; pass agent="
                f"{sorted(adapters.known_agents())}"
            ),
        }
    if agent not in adapters.known_agents():
        return {
            "ok": False,
            "error": (
                f"unknown agent {agent!r}; known: {sorted(adapters.known_agents())}"
            ),
        }
    problem = adapters.validate_label(label)
    if problem:
        return {"ok": False, "error": problem}

    label = label.strip()
    # Ask BEFORE writing anything durable. A local refusal after the registry
    # row exists leaves a lane this process never adopted and does not read,
    # advertised to senders as held and unclaimable by anyone else.
    refused = adapters.claim_refusal(label)
    if refused:
        return {"ok": False, "error": refused}

    claimed = await _in_thread(
        sessions.claim,
        agent=agent,
        label=label,
        lanes=adapters.owned_recipients(agent),
    )
    if not claimed.get("ok"):
        return claimed

    # Only adopt the lane locally once the registry has accepted it. Claiming
    # first would rename this process to a name it lost the race for, and every
    # subsequent dispatch would address results to a lane held by someone else.
    refused = adapters.claim_lane(label)
    if refused:
        # The pre-check passed and this still refused, so two concurrent
        # register_session calls filled the last slot between them. Undo the
        # durable half rather than leave the orphan the pre-check exists to
        # prevent.
        await _in_thread(sessions.drop_lane, claimed["lane"])
        return {"ok": False, "error": refused, "lane": claimed["lane"]}

    # Remember the identity too, not just the name. Every later ownership check
    # re-derives it, and for the sessions that must pass `agent` explicitly
    # there is nothing in the environment to re-derive it FROM.
    adapters.declare_agent(agent)
    await _in_thread(_announce_self, agent, label)
    return {
        "ok": True,
        "lane": claimed["lane"],
        "agent": agent,
        "label": label,
        "held_lanes": list(adapters.held_lanes()),
        "note": (
            f"Others can now reach this session as {claimed['lane']!r}. "
            "inbox() continues to read every lane you have held."
        ),
    }


@mcp.tool()
async def server_info() -> dict:
    """Version, limits, and timeout budgets of THIS running hardline-mcp.

    Deployment here is an editable install, so a process runs whatever the
    working tree said when it spawned — "is the fix live?" has repeatedly been
    answered by counting processes and diffing tool rosters. Reporting the
    version and the effective limits makes that one call.
    """
    try:
        from importlib.metadata import version as _pkg_version

        pkg_version = _pkg_version("hardline-mcp")
    except Exception:  # noqa: BLE001 - version is diagnostic, never fatal
        pkg_version = "unknown"

    timeouts: dict[str, object] = {}
    for agent in adapters.known_agents():
        try:
            timeouts[agent] = adapters._timeout_for(agent)
        except ValueError as exc:  # a bad HARDLINE_*_TIMEOUT_S env value
            timeouts[agent] = f"invalid: {exc}"

    write_ok, write_err = adapters._write_enabled()
    return {
        "version": pkg_version,
        "schema_version": mailbox.SCHEMA_VERSION,
        "module_path": str(Path(mailbox.__file__).resolve()),
        "db_path": str(mailbox._resolve_db(None)),
        "pid": os.getpid(),
        "jobs": await _in_thread(jobs.counts),
        "limits": {
            "inbox_default": mailbox.DEFAULT_INBOX_LIMIT,
            "inbox_max": mailbox.MAX_INBOX_LIMIT,
            "history_default": mailbox.DEFAULT_HISTORY_LIMIT,
            "history_max": mailbox.MAX_HISTORY_LIMIT,
            "max_body_chars": _MAX_BODY_CHARS,
            "min_body_chars": _MIN_BODY_CHARS,
            "max_response_chars": _MAX_RESPONSE_CHARS,
            # The honest ceiling, not the target. A consuming read may not
            # drop a message it has consumed, so at the per-body floor a
            # maximal batch necessarily exceeds the target - reporting only
            # the target would advertise a bound the code can exceed.
            "inbox_worst_case_chars": mailbox.MAX_INBOX_LIMIT
            * (_MIN_BODY_CHARS + _NOTE_OVERHEAD),
            "budget_note": (
                "Budgets bound message BODIES. Envelope keys, timestamps and "
                "JSON escaping are extra."
            ),
        },
        "timeouts_s": timeouts,
        "async_max_workers": _ASYNC_MAX_WORKERS,
        "write_enabled": write_ok,
        "write_note": write_err,
    }


@mcp.tool()
async def peek(message_id: int) -> dict:
    """Return ONE message by id, body in full and never truncated.

    The escape hatch for a message ``inbox`` shortened. Read-only: unlike
    ``inbox`` this never acks, and it is deliberately not lane-scoped for the
    same reason ``history`` is not — recovering a payload another session
    already consumed is exactly what it is for.

    Returns ``{"ok": true, "message": {...}}`` or ``{"ok": false, "error"}``.
    """
    msg = await _in_thread(mailbox.peek, message_id)
    if msg is None:
        return {"ok": False, "error": f"no message with id {message_id}"}
    return {"ok": True, "message": msg}


@mcp.tool()
async def ack(message_id: int) -> dict:
    """Mark a message read so it stops appearing in the unread inbox.

    Refuses messages belonging to a DIFFERENT session's lane — one session
    can no longer hide another's results. Unqualified messages stay shared
    and ackable by anyone.

    Returns ``{"ok": true}`` only if a still-unread message with that id
    existed and was ackable by this session (idempotent — a second ack
    returns false).
    """
    return await _in_thread(
        mailbox.ack, message_id, owned=adapters.owned_recipients()
    )


@mcp.tool()
async def history(
    limit: int = mailbox.DEFAULT_HISTORY_LIMIT,
    agent: str | None = None,
    before_id: int | None = None,
) -> dict:
    """Recent messages, newest first — the visibility / audit feed.

    ``agent``, if given, filters to messages where it is either sender or
    recipient. Never acks and never hides acked messages, which is what makes
    it the recovery path for anything ``inbox`` consumed.

    ``limit`` is capped at ``MAX_HISTORY_LIMIT`` and bodies are truncated on
    the same rule as ``inbox`` — an audit feed returning whole bodies without
    a ceiling is the same context flood through another door. Use
    ``peek(message_id)`` for one body in full.

    ``before_id`` pages backward: pass the lowest ``message_id`` you have seen
    to get the page before it — or just use the returned ``next_before_id``.
    Since ``inbox`` consumes what it returns, this is the route to a result
    whose response was lost, and without paging anything older than one capped
    page was unreachable.

    The WHOLE response is capped, not only each body: a full page of
    4000-char bodies is ~200KB, which the host truncated itself, so the
    response was cut anyway and ``truncated`` understated it. When the cap
    bites, the page is shortened and ``next_before_id`` continues from there.

    Returns ``{"messages", "count", "truncated", "dropped", "next_before_id",
    "has_more"}``.
    """
    msgs = await _in_thread(mailbox.history, limit, agent, before_id=before_id)
    full_page = len(msgs)
    msgs, truncated, dropped = _fit_response(msgs, allow_drop=True)
    response = {
        "messages": msgs,
        "count": len(msgs),
        "truncated": truncated,
        "dropped": dropped,
    }
    if msgs:
        # Always present, not only when capped, so paging is one uniform loop
        # rather than a special case the caller has to detect.
        response["next_before_id"] = msgs[-1]["message_id"]
    # More to fetch if the aggregate cap bit, or the page came back full.
    response["has_more"] = bool(dropped) or full_page >= max(1, min(limit, mailbox.MAX_HISTORY_LIMIT))
    if not msgs and agent is not None:
        # An empty page for a filtered query is ambiguous: no traffic, or the
        # wrong name? That ambiguity cost real time — an agent filtered on its
        # display name, got nothing, and had no way to tell the name was wrong.
        # Only answer when the name genuinely carries no traffic.
        known = set(adapters.known_agents())
        if adapters.base_agent(agent) not in known:
            response["hint"] = (
                f"{agent!r} is not a known agent and has no messages; "
                f"addressable agents are {sorted(known)}. "
                "Call list_agents() for the names actually in use."
            )
    return response


# ── live query tools ─────────────────────────────────────────────────────────


@mcp.tool()
async def job_status(job_id: str) -> dict:
    """State and timings of one async dispatch, without consuming anything.

    The lifecycle answer the mailbox could not give: polling an inbox tells
    you a result has not arrived, never whether the work is still running,
    already finished, or died with its owner.

    ``state`` is one of queued / running / completed / failed / cancelled /
    lost. ``lost`` is resolved here rather than written by the process that
    died — it means the owner exited without recording a terminal state.
    """
    job = await _in_thread(jobs.get, job_id)
    if job is None:
        return {"ok": False, "error": f"no job {job_id!r}"}
    return {"ok": True, "job": job}


@mcp.tool()
async def job_result(job_id: str) -> dict:
    """The terminal result of a finished job, in full and never truncated.

    Survives the mailbox entirely: even if the delivered message was consumed
    by another read, lost with an interrupted response, or never sent because
    delivery itself failed, the result is recorded against the job before
    delivery is attempted.
    """
    job = await _in_thread(jobs.get, job_id)
    if job is None:
        return {"ok": False, "error": f"no job {job_id!r}"}
    if job["state"] not in jobs.TERMINAL_STATES:
        return {
            "ok": False,
            "error": f"job is {job['state']}, not finished",
            "state": job["state"],
        }
    return {
        "ok": True,
        "job_id": job_id,
        "state": job["state"],
        "result": job.get("result"),
        "error": job.get("error"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }


@mcp.tool()
async def job_cancel(job_id: str) -> dict:
    """Stop a running dispatch and mark it cancelled.

    Works across processes: cancellation goes through the child's recorded
    pid, not an in-process handle, so a session can stop a job another session
    started. The whole child tree is killed — ``claude``/``codex`` are
    launchers, so killing only the recorded pid leaves the real worker running
    invisibly.
    """
    return await _in_thread(jobs.request_cancel, job_id)


@mcp.tool()
async def list_jobs(
    state: str | None = None,
    agent: str | None = None,
    requester: str | None = None,
    active_only: bool = False,
    limit: int = jobs.DEFAULT_JOB_LIMIT,
) -> dict:
    """Recent jobs, newest first, with a state-count summary.

    ``active_only`` narrows to queued/running — the "what is still in flight?"
    question. Bounded like every other read here: results are summarised
    rather than inlined, because a listing that embedded 200 full agent
    replies would be the same context flood this package spent a release
    bounding everywhere else. Use ``job_result(job_id)`` for one in full.
    """
    # One sweep for the page AND the summary. Two calls meant two liveness
    # sweeps per request, and each dead row a sweep finds commits its own
    # transaction - real write contention with ~26 servers on one store.
    rows, summary = await _in_thread(
        jobs.listing_with_counts,
        state=state,
        agent=agent,
        requester=requester,
        active_only=active_only,
        limit=limit,
    )
    for row in rows:
        result = row.get("result")
        if result is None:
            continue
        encoded = json.dumps(result, default=str)
        row["result_chars"] = len(encoded)
        row["result"] = (
            result
            if len(encoded) <= _JOB_RESULT_PREVIEW_CHARS
            else {
                "preview": encoded[:_JOB_RESULT_PREVIEW_CHARS],
                "truncated": True,
                "full_via": f"job_result(job_id={row['job_id']!r})",
            }
        )
    return {"jobs": rows, "count": len(rows), "counts_by_state": summary}


@mcp.tool()
async def ask_hermes(prompt: str) -> dict:
    """Ask the Hermes agent (MrAnderson) a question and wait for its reply.

    Spawns a one-shot ``hermes chat -q`` — this is slower and heavier than the
    async mailbox; use it when you need an answer NOW. Returns
    ``{"ok", "reply"}`` or ``{"ok": false, "error"}``.
    """
    return await _in_thread(adapters.ask, "hermes", prompt)


@mcp.tool()
async def ask_codex(
    prompt: str,
    model: str | None = None,
    effort: CodexEffort = "default",
    mode: CodexMode = "default",
    workdir: str | None = None,
    write: bool = False,
) -> dict:
    """Ask Codex a question and wait for its reply.

    Spawns an ephemeral ``codex exec``. Omitting ``model`` passes no
    ``--model`` flag, so Codex's own configured default applies. When set,
    ``model`` must be Codex's full model identifier (e.g. ``gpt-5.6-sol``,
    ``gpt-5.6-terra``) — not a shorthand like ``"sol"``. hardline does not
    validate or expand it; an unrecognized value is rejected by Codex itself
    with a clear error rather than silently substituted. Optional
    model/effort selection enables JSONL usage/thread telemetry.
    Advisory mode uses ChatGPT auth preflight, a temporary auth-only CODEX_HOME,
    a neutral read-only directory, ignored user/project configuration, and
    stripped API-provider overrides.
    ``workdir`` targets a repository in default mode and is rejected in advisory
    mode. ``write=True`` opts into a workspace-write sandbox with approvals
    disabled (unattended) — it requires ``workdir``, is rejected in advisory
    mode, and is refused unless this hardline-mcp process has
    ``HARDLINE_ALLOW_WRITE`` set to a recognized truthy value
    (``1``/``true``/``yes``, case-insensitive); omitted, Codex stays
    read-only. Codex JSONL does not currently report served model/effective
    effort, so those telemetry fields remain null rather than being guessed.

    DAMAGED OUTPUT: if any output line could not be parsed, the result comes
    back ``ok: false`` with ``malformed_lines`` and the recovered text under
    ``partial_reply`` instead of ``reply`` — content preserved but NOT
    certified, because a skipped line may have been a later answer or the
    terminal event, making the survivor stale. Do not discard a
    ``partial_reply`` on ``ok: false`` alone; read it and judge it.
    """
    return await _in_thread(
        adapters.ask_codex,
        prompt,
        model=model,
        effort=effort,
        mode=mode,
        workdir=workdir,
        write=write,
    )


def _ask_async_impl(
    agent: str,
    ask_fn,
    prompt: str,
    from_agent: str,
    *,
    label: str | None,
    model: str | None,
    effort: str,
    mode: str,
    workdir: str | None,
    write: bool,
) -> dict:
    """Shared body for ask_codex_async/ask_claude_async - only the adapter
    function and the mailbox sender identity differ between agents.

    Briefly waits to see whether the dispatch fails immediately, so callers
    MUST route this through _in_thread rather than calling it directly: the
    wait would otherwise block the event loop for every other tool, including
    pings.
    """
    known = adapters.known_agents()
    if adapters.base_agent(from_agent) not in known:
        return {
            "ok": False,
            "error": f"unknown from_agent {from_agent!r}; known: {sorted(known)}",
        }
    # Deliver back to THIS session's lane, not the shared name. A result is
    # owed to the session that asked for it; addressing it to bare "claude"
    # is what let another session ack it out of sight.
    recipient = adapters.lane_for(from_agent)

    # Durable identity BEFORE the work starts. Fire-and-forget meant a restart
    # lost the task with no record it had existed, and the only lifecycle API
    # was polling a mailbox that cannot answer "is it still running?".
    job_id = jobs.create(
        agent=agent,
        requester=recipient,
        label=label,
        request={
            "prompt_chars": len(prompt),
            "model": model,
            "effort": effort,
            "mode": mode,
            "workdir": workdir,
            "write": write,
        },
    )

    def _run() -> dict:
        # ask_fn (adapters.ask_claude/ask_codex) is designed to never raise -
        # every failure mode it recognizes already comes back as an
        # {"ok": False, "error"} dict. This is a backstop for whatever it
        # doesn't recognize: without it, an uncaught exception here would
        # kill this pool thread silently, mailbox.send would never run, and
        # a caller polling inbox(from_agent) would wait forever with no
        # error ever surfacing anywhere.
        if not jobs.mark_running(job_id):
            # The queued -> running claim failed, which means a cancel landed
            # first. Spawning anyway would run the whole expensive call while
            # the row said cancelled - the one outcome a cancel must prevent.
            cancelled = {
                "ok": False,
                "error": "job was cancelled before it started",
                "job_id": job_id,
                "cancelled_before_start": True,
            }
            if label is not None:
                cancelled["label"] = label
            try:
                mailbox.send(agent, recipient, json.dumps(cancelled))
            except Exception:  # noqa: BLE001 - notification is best effort
                traceback.print_exc()
            return cancelled
        try:
            result = ask_fn(
                prompt,
                model=model,
                effort=effort,
                mode=mode,
                workdir=workdir,
                write=write,
                # Recorded so a cancel issued from ANOTHER session can reach a
                # run this process is blocked on - the dispatcher is often not
                # the one watching it.
                on_spawn=lambda pid: jobs.set_child_pid(
                    job_id, pid, started_key=jobs.process_key(pid)
                ),
            )
        except Exception as exc:  # noqa: BLE001 - last-resort dispatch backstop
            result = {
                "ok": False,
                "error": f"{agent} async dispatch raised {type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        if label is not None:
            result["label"] = label
        result["job_id"] = job_id
        # Terminal state recorded before delivery: the mailbox send below can
        # fail, and a job whose result exists only in a message that never
        # arrived is the failure this table exists to prevent.
        try:
            jobs.finish(job_id, result=result)
        except Exception:  # noqa: BLE001 - bookkeeping must not eat the result
            traceback.print_exc()
        # The delivery itself was outside the backstop above, so a failure
        # here - lock contention, a full disk, an unserializable result -
        # discarded an expensive completed run inside an unobserved future,
        # leaving the caller polling an inbox that would never fill. Retry
        # with a minimal payload, which fails only if the mailbox is
        # unreachable entirely; then at least the traceback reaches a log.
        try:
            mailbox.send(agent, recipient, json.dumps(result))
        except Exception:  # noqa: BLE001 - delivery is the last thing owed
            traceback.print_exc()
            try:
                mailbox.send(
                    agent,
                    recipient,
                    json.dumps(
                        {
                            "ok": False,
                            "error": f"{agent} result could not be delivered",
                            "label": label,
                        }
                    ),
                )
            except Exception:  # noqa: BLE001 - mailbox itself is unreachable
                traceback.print_exc()
        return result

    future = _async_executor.submit(_run)

    # Wait briefly to see whether this dies on arrival. The receipt used to be
    # returned the instant the task was QUEUED, so a call that failed in under
    # a second - a rejected model, a bad workdir - still reported
    # {"ok": true, "dispatched": true}. Two separate sessions read that as
    # proof work was running and waited on results that never existed; one
    # dispatched five agents and believed all five were in flight. A receipt
    # that cannot distinguish "started" from "already failed" is a write-only
    # signal, so spend a moment to make it mean something.
    try:
        early = future.result(timeout=_ASYNC_EARLY_FAILURE_S)
    except FuturesTimeout:
        early = None  # still running - genuinely dispatched, report it as such

    if early is not None and not early.get("ok", False):
        # Failed before we finished waiting. The mailbox copy is still written
        # (above), so the record survives; this just refuses to call it a
        # successful dispatch.
        return {
            "ok": False,
            "dispatched": False,
            "error": early.get("error", f"{agent} dispatch failed immediately"),
            "job_id": job_id,
            "label": label,
            "lane": recipient,
        }
    return {
        "ok": True,
        "dispatched": True,
        # The durable handle. A label is a correlation aid the caller chooses
        # and may reuse; this identifies the run even across a restart.
        "job_id": job_id,
        "label": label,
        "lane": recipient,
        "track_with": f"job_status(job_id={job_id!r})",
    }


@mcp.tool()
async def ask_codex_async(
    prompt: str,
    from_agent: str,
    label: str | None = None,
    model: str | None = None,
    effort: CodexEffort = "default",
    mode: CodexMode = "default",
    workdir: str | None = None,
    write: bool = False,
) -> dict:
    """Dispatch a Codex task in the background; returns immediately.

    Runs the same ``ask_codex`` in a background thread, then delivers the
    result through the existing mailbox as a message from "codex" to
    ``from_agent`` — poll it with ``inbox(agent=from_agent)``. The delivered
    message body is the JSON-encoded ``ask_codex`` result, plus ``label`` if
    supplied (use it to match results when firing several concurrent
    dispatches). ``from_agent`` must be a known agent. Fire-and-forget: not
    persisted, so a hardline-mcp restart before completion loses the task.

    ``mode`` mirrors ``ask_codex`` — background review is exactly where
    advisory isolation is wanted, and omitting it here was a parity gap.
    """
    return await _in_thread(
        _ask_async_impl,
        "codex",
        adapters.ask_codex,
        prompt,
        from_agent,
        label=label,
        model=model,
        effort=effort,
        mode=mode,
        workdir=workdir,
        write=write,
    )


@mcp.tool()
async def ask_claude(
    prompt: str,
    model: str | None = None,
    effort: ClaudeEffort = "default",
    mode: ClaudeMode = "default",
    workdir: str | None = None,
    write: bool = False,
) -> dict:
    """Ask Claude Code a question and wait for its reply.

    With no options, preserves the original one-shot ``claude -p`` behavior
    and response shape — omitting ``model`` passes no ``--model`` flag, so
    Claude Code's own configured default applies — plus (parity with Codex)
    Edit/Write/NotebookEdit are denied by default — inspection tools like
    Read/Grep/Bash still work. ``model`` pins a Claude alias/full model ID.
    ``effort`` is one of ``default|low|medium|high|xhigh|max``; ``default``
    omits the flag. ``workdir`` targets a repository in default mode and is
    rejected in advisory mode. ``write=True`` opts into full tool access plus
    ``--permission-mode bypassPermissions`` (unattended — stdin is
    ``/dev/null``, so an interactive prompt would hang to timeout instead of
    being answered); it requires ``workdir``, is rejected in advisory mode,
    and is refused unless this hardline-mcp process has
    ``HARDLINE_ALLOW_WRITE`` set to a recognized truthy value
    (``1``/``true``/``yes``, case-insensitive). Mode ``advisory`` disables tools/project
    customizations, runs in a neutral cwd, strips API-provider overrides, and
    fails closed unless response telemetry verifies first-party account auth
    without overage. Optioned calls return actual-model, usage, rate-limit,
    auth-verification, and safeguard fallback metadata in addition to
    ``ok``/``reply``.

    DAMAGED OUTPUT: if any output line could not be parsed, the result comes
    back ``ok: false`` with ``malformed_lines`` and the recovered text under
    ``partial_reply`` instead of ``reply`` — content preserved but NOT
    certified, because a skipped line may have been a later answer or the
    terminal event, making the survivor stale. Do not discard a
    ``partial_reply`` on ``ok: false`` alone; read it and judge it.
    """
    return await _in_thread(
        adapters.ask_claude,
        prompt,
        model=model,
        effort=effort,
        mode=mode,
        workdir=workdir,
        write=write,
    )


@mcp.tool()
async def ask_claude_async(
    prompt: str,
    from_agent: str,
    label: str | None = None,
    model: str | None = None,
    effort: ClaudeEffort = "default",
    mode: ClaudeMode = "default",
    workdir: str | None = None,
    write: bool = False,
) -> dict:
    """Dispatch a Claude task in the background; returns immediately.

    Runs the same ``ask_claude`` in a background thread, then delivers the
    result through the existing mailbox as a message from "claude" to
    ``from_agent`` — poll it with ``inbox(agent=from_agent)``. The delivered
    message body is the JSON-encoded ``ask_claude`` result, plus ``label`` if
    supplied (use it to match results when firing several concurrent
    dispatches). ``from_agent`` must be a known agent. Fire-and-forget: not
    persisted, so a hardline-mcp restart before completion loses the task.

    ``mode`` mirrors ``ask_claude`` — background review is exactly where
    advisory isolation is wanted, and omitting it here was a parity gap.
    """
    return await _in_thread(
        _ask_async_impl,
        "claude",
        adapters.ask_claude,
        prompt,
        from_agent,
        label=label,
        model=model,
        effort=effort,
        mode=mode,
        workdir=workdir,
        write=write,
    )


def main() -> None:
    """Console-script entry point (``hardline-mcp``). Serves over stdio."""
    # Announce before serving, so a session is discoverable from its first
    # moment rather than only after it happens to call a tool. Best effort by
    # construction (see _announce_self): a registry problem must not stop the
    # server from doing its actual job.
    _announce_self()
    atexit.register(_unregister_self)
    mcp.run()


def _unregister_self() -> None:
    """Drop this process's registry row on a clean exit.

    Not required for correctness - a vanished process is pruned by the next
    reader either way - but it closes the window where a session that has just
    exited still looks like a destination.
    """
    try:
        sessions.unregister()
    except Exception:  # noqa: BLE001 - shutdown must not raise
        pass


if __name__ == "__main__":
    main()
