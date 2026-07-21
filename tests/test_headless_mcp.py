"""Headless end-to-end test over the REAL MCP protocol.

The other tests exercise the mailbox and adapters directly (in-process). This
one proves the piece they can't: two *independent* hardline-mcp server
subprocesses — as two agents would each run — sharing one mailbox db, driven by
real MCP clients over stdio. A cross-instance send -> inbox -> ack round-trip
through JSON-RPC is the closest a test can get to "agent A messages agent B"
without launching the agents themselves.
"""

from __future__ import annotations

import json
import os
import sys

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _params(db_path) -> StdioServerParameters:
    # A fresh server process pointed at an isolated shared db via HARDLINE_DB.
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "hardline_mcp.server"],
        env={**os.environ, "HARDLINE_DB": str(db_path)},
    )


def _tool_dict(result) -> dict:
    """Extract the dict a tool returned from a CallToolResult."""
    if result.structuredContent is not None:
        return result.structuredContent
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError(f"no structured/text content in tool result: {result!r}")


@pytest.mark.anyio
async def test_headless_cross_instance_round_trip(tmp_path):
    db = tmp_path / "shared.db"
    params = _params(db)

    with anyio.fail_after(60):
        # Instance A ("claude") — a full server subprocess — sends a message.
        async with stdio_client(params) as (ra, wa):
            async with ClientSession(ra, wa) as a:
                await a.initialize()
                names = {t.name for t in (await a.list_tools()).tools}
                assert {"send", "inbox", "ack", "history"} <= names
                ask_claude = next(
                    t for t in (await a.list_tools()).tools if t.name == "ask_claude"
                )
                properties = ask_claude.inputSchema["properties"]
                assert properties["model"]["anyOf"][0]["type"] == "string"
                assert properties["effort"]["default"] == "default"
                assert properties["mode"]["default"] == "default"
                sent = _tool_dict(
                    await a.call_tool(
                        "send",
                        {
                            "from_agent": "claude",
                            "to_agent": "hermes",
                            "message": "headless hi",
                        },
                    )
                )
                assert sent["ok"] is True
                message_id = sent["message_id"]

        # Instance B ("hermes") — a SEPARATE server subprocess — reads the shared
        # db over the protocol and sees A's message.
        async with stdio_client(params) as (rb, wb):
            async with ClientSession(rb, wb) as b:
                await b.initialize()
                inbox = _tool_dict(await b.call_tool("inbox", {"agent": "hermes"}))
                assert inbox["count"] == 1
                assert inbox["messages"][0]["body"] == "headless hi"
                assert inbox["messages"][0]["sender"] == "claude"

                acked = _tool_dict(await b.call_tool("ack", {"message_id": message_id}))
                assert acked["ok"] is True

                after = _tool_dict(await b.call_tool("inbox", {"agent": "hermes"}))
                assert after["count"] == 0  # acked -> no longer unread


@pytest.mark.anyio
async def test_headless_unknown_agent_rejected_over_protocol(tmp_path):
    # The server's recipient validation must surface over the wire, not just
    # in the in-process unit test.
    with anyio.fail_after(60):
        async with stdio_client(_params(tmp_path / "mb.db")) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = _tool_dict(
                    await s.call_tool(
                        "send",
                        {"from_agent": "claude", "to_agent": "nobody", "message": "x"},
                    )
                )
                assert res["ok"] is False and "unknown" in res["error"].lower()
