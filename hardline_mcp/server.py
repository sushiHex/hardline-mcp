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

import anyio.to_thread
from mcp.server.fastmcp import FastMCP

from . import adapters, mailbox

mcp = FastMCP("hardline-mcp")


async def _in_thread(fn, *args, **kwargs):
    """Run a blocking tool body off the event loop."""
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


# ── mailbox tools ────────────────────────────────────────────────────────────

def _send_impl(from_agent: str, to_agent: str, message: str, deliver: bool) -> dict:
    result = mailbox.send(from_agent, to_agent, message)
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

    ``from_agent``/``to_agent`` are one of: claude, hermes, codex. Returns
    ``{"message_id", "created_at"}`` (plus ``delivery`` when ``deliver`` set).
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
async def ask_codex(prompt: str) -> dict:
    """Ask Codex a question and wait for its reply.

    Spawns a one-shot ``codex exec``. Slower/heavier than the mailbox; use for
    live answers. Returns ``{"ok", "reply"}`` or ``{"ok": false, "error"}``.
    """
    return await _in_thread(adapters.ask, "codex", prompt)


@mcp.tool()
async def ask_claude(prompt: str) -> dict:
    """Ask Claude Code a question and wait for its reply.

    Spawns a one-shot headless ``claude -p`` — the heaviest of the three (a
    full Claude session per call). Use sparingly, for live answers. Returns
    ``{"ok", "reply"}`` or ``{"ok": false, "error"}``.
    """
    return await _in_thread(adapters.ask, "claude", prompt)


def main() -> None:
    """Console-script entry point (``hardline-mcp``). Serves over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
