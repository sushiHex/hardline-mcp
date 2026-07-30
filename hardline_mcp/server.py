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

import functools
import json
import os
import traceback
from concurrent.futures import ThreadPoolExecutor
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
# its own _CLAUDE_TIMEOUT_S/_CODEX_TIMEOUT_S, up to 900s) in the background.
# A bare `threading.Thread` per call has no ceiling - repeated dispatches (a
# runaway caller, or several concurrent write=True requests) would pile up
# unbounded concurrent agent subprocesses. Route through a small fixed-size
# pool instead: excess dispatches queue rather than spawning without limit.
# Override for local tuning; not exposed as a per-call parameter since it
# bounds the whole hardline-mcp process, not one request.
_ASYNC_MAX_WORKERS = int(os.environ.get("HARDLINE_ASYNC_MAX_WORKERS", "4"))
_async_executor = ThreadPoolExecutor(
    max_workers=_ASYNC_MAX_WORKERS, thread_name_prefix="hardline-async"
)


async def _in_thread(fn, *args, **kwargs):
    """Run a blocking tool body off the event loop."""
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


# ── mailbox tools ────────────────────────────────────────────────────────────


def _send_impl(from_agent: str, to_agent: str, message: str, deliver: bool) -> dict:
    # Reject unknown agents up front: a typo'd recipient would otherwise persist
    # forever, unread and undeliverable — a silent black hole. Validate before
    # writing anything.
    known = adapters.known_agents()
    unknown = [a for a in (from_agent, to_agent) if a not in known]
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
        result["delivery"] = adapters.deliver(to_agent, notice)
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
async def inbox(agent: str, unread_only: bool = True) -> dict:
    """Read messages addressed to ``agent``, oldest first.

    ``unread_only`` (default true) hides messages already ack'd. Returns
    ``{"messages": [...], "count": N}``.
    """
    msgs = await _in_thread(mailbox.inbox, agent, unread_only=unread_only)
    return {"messages": msgs, "count": len(msgs)}


@mcp.tool()
async def ack(message_id: int) -> dict:
    """Mark a message read so it stops appearing in the unread inbox.

    Returns ``{"ok": true}`` only if a still-unread message with that id
    existed (idempotent — a second ack returns false).
    """
    return await _in_thread(mailbox.ack, message_id)


@mcp.tool()
async def history(limit: int = 50, agent: str | None = None) -> dict:
    """Recent messages, newest first — the visibility / audit feed.

    ``agent``, if given, filters to messages where it is either sender or
    recipient. Returns ``{"messages": [...], "count": N}``.
    """
    msgs = await _in_thread(mailbox.history, limit, agent)
    return {"messages": msgs, "count": len(msgs)}


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
    ``--model`` flag, so Codex's own configured default applies. Optional
    model/effort selection enables JSONL usage/thread telemetry.
    Advisory mode uses ChatGPT auth preflight, a temporary auth-only CODEX_HOME,
    a neutral read-only directory, ignored user/project configuration, and
    stripped API-provider overrides.
    ``workdir`` targets a repository in default mode and is rejected in advisory
    mode. ``write=True`` opts into a workspace-write sandbox with approvals
    disabled (unattended) — it requires ``workdir``, is rejected in advisory
    mode, and is refused unless this hardline-mcp process has
    ``HARDLINE_ALLOW_WRITE=1`` set; omitted, Codex stays read-only. Codex
    JSONL does not currently report served model/effective effort, so those
    telemetry fields remain null rather than being guessed.
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

    Does not block (validation is trivial, and submitting to the executor
    returns immediately), so callers invoke this directly rather than through
    _in_thread - routing a call this cheap through the worker-thread pool
    would just add a wasted thread-pool round trip.
    """
    known = adapters.known_agents()
    if from_agent not in known:
        return {
            "ok": False,
            "error": f"unknown from_agent {from_agent!r}; known: {sorted(known)}",
        }

    def _run() -> None:
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
        mailbox.send(agent, from_agent, json.dumps(result))

    _async_executor.submit(_run)
    return {"ok": True, "dispatched": True, "label": label}


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
    return _ask_async_impl(
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
    ``HARDLINE_ALLOW_WRITE=1`` set. Mode ``advisory`` disables tools/project
    customizations, runs in a neutral cwd, strips API-provider overrides, and
    fails closed unless response telemetry verifies first-party account auth
    without overage. Optioned calls return actual-model, usage, rate-limit,
    auth-verification, and safeguard fallback metadata in addition to
    ``ok``/``reply``.
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
    return _ask_async_impl(
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
