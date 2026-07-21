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
                assert properties["effort"]["enum"] == [
                    "default",
                    "low",
                    "medium",
                    "high",
                    "xhigh",
                    "max",
                ]
                assert properties["mode"]["default"] == "default"
                assert properties["mode"]["enum"] == ["default", "advisory"]
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


@pytest.mark.skipif(
    os.name == "nt", reason="portable fake executable uses a POSIX shebang"
)
@pytest.mark.anyio
async def test_headless_claude_effort_reaches_actual_executable(tmp_path):
    """MCP -> server -> adapter -> executable argv, without spending plan tokens."""
    capture = tmp_path / "claude-argv.json"
    fake = tmp_path / "claude"
    fake.write_text(
        f"""#!{sys.executable}
import json
import os
import sys

with open(os.environ["HARDLINE_CAPTURE_ARGV"], "w", encoding="utf-8") as fh:
    json.dump(sys.argv[1:], fh)
print(json.dumps({{"type": "system", "subtype": "init", "model": "claude-fable-5"}}))
print(json.dumps({{"type": "result", "subtype": "success", "result": "captured"}}))
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "hardline_mcp.server"],
        env={
            **os.environ,
            "HARDLINE_DB": str(tmp_path / "mb.db"),
            "HARDLINE_CLAUDE_CMD": str(fake),
            "HARDLINE_CAPTURE_ARGV": str(capture),
        },
    )

    with anyio.fail_after(60):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                payload = _tool_dict(
                    await session.call_tool(
                        "ask_claude",
                        {
                            "prompt": "capture this",
                            "model": "fable",
                            "effort": "xhigh",
                        },
                    )
                )

    assert payload["ok"] is True
    argv = json.loads(capture.read_text(encoding="utf-8"))
    assert argv[argv.index("--model") + 1] == "fable"
    assert argv[argv.index("--effort") + 1] == "xhigh"
    assert argv[-2:] == ["--", "capture this"]
