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
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Literal

import anyio.to_thread
from mcp.server.fastmcp import FastMCP

from . import adapters, mailbox

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

_async_executor = ThreadPoolExecutor(
    max_workers=_ASYNC_MAX_WORKERS, thread_name_prefix="hardline-async"
)


def _truncate_bodies(messages: list[dict]) -> tuple[list[dict], int]:
    """Shorten oversized bodies for a batch read. Returns (messages, n_truncated).

    Copies each row it shortens rather than mutating in place - the caller's
    dicts come straight from the db layer and nothing downstream should see a
    body that silently lost content.
    """
    out: list[dict] = []
    truncated = 0
    for msg in messages:
        body = msg.get("body") or ""
        if len(body) <= _MAX_BODY_CHARS:
            out.append(msg)
            continue
        dropped = len(body) - _MAX_BODY_CHARS
        msg = dict(msg)
        msg["body"] = body[:_MAX_BODY_CHARS] + _TRUNCATION_NOTE.format(
            n=dropped, mid=msg.get("message_id")
        )
        msg["body_truncated"] = True
        msg["body_length"] = len(body)
        out.append(msg)
        truncated += 1
    return out, truncated


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
        lane = adapters.lane_for(agent)
        agents = [agent] if lane == agent else [agent, lane]
    msgs, remaining = await _in_thread(
        mailbox.inbox,
        agents,
        unread_only=unread_only,
        limit=limit,
        auto_ack=auto_ack,
        # Same lane identity the explicit ack tool uses, so a consuming read
        # cannot drain a lane this session does not own.
        lane_suffix=adapters.lane_suffix(),
    )
    msgs, truncated = _truncate_bodies(msgs)
    return {
        "messages": msgs,
        "count": len(msgs),
        "remaining": remaining,
        "truncated": truncated,
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
    return await _in_thread(mailbox.ack, message_id, lane_suffix=adapters.lane_suffix())


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
    to get the page before it. Since ``inbox`` consumes what it returns, this
    is the route to a result whose response was lost — and without paging,
    anything older than one capped page was unreachable.

    Returns ``{"messages", "count", "truncated"}``.
    """
    msgs = await _in_thread(mailbox.history, limit, agent, before_id=before_id)
    msgs, truncated = _truncate_bodies(msgs)
    return {"messages": msgs, "count": len(msgs), "truncated": truncated}


# ── live query tools ─────────────────────────────────────────────────────────


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

    def _run() -> dict:
        # ask_fn (adapters.ask_claude/ask_codex) is designed to never raise -
        # every failure mode it recognizes already comes back as an
        # {"ok": False, "error"} dict. This is a backstop for whatever it
        # doesn't recognize: without it, an uncaught exception here would
        # kill this pool thread silently, mailbox.send would never run, and
        # a caller polling inbox(from_agent) would wait forever with no
        # error ever surfacing anywhere.
        try:
            result = ask_fn(
                prompt, model=model, effort=effort, workdir=workdir, write=write
            )
        except Exception as exc:  # noqa: BLE001 - last-resort dispatch backstop
            result = {
                "ok": False,
                "error": f"{agent} async dispatch raised {type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        if label is not None:
            result["label"] = label
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
            "label": label,
            "lane": recipient,
        }
    return {"ok": True, "dispatched": True, "label": label, "lane": recipient}


@mcp.tool()
async def ask_codex_async(
    prompt: str,
    from_agent: str,
    label: str | None = None,
    model: str | None = None,
    effort: CodexEffort = "default",
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
        workdir=workdir,
        write=write,
    )


def main() -> None:
    """Console-script entry point (``hardline-mcp``). Serves over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
