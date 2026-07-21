"""LIVE integration test — spawns the REAL agent CLIs and hits their brains.

Everything else in this suite stubs the agents (subprocess mocked, or the
"agent" is just a string parameter). This test does the opposite: it drives
``adapters.ask`` against the actual installed ``hermes`` / ``codex`` / ``claude``
CLIs, so a passing run proves the bridge works end-to-end against reality —
e.g. Hermes answering on its configured ChatGPT brain.

Because that costs real plan tokens and needs the CLIs installed, it is
DOUBLE-GATED and off by default:

  1. Opt-in: only runs when ``HARDLINE_LIVE_TESTS=1`` is set. A plain
     ``pytest`` (and CI, which never sets it) skips the whole module — no
     surprise token burn, nothing to break the CI matrix.
  2. Per-agent: even when enabled, each agent is skipped unless its CLI is
     actually resolvable (via the same env-override / discovery / PATH logic
     production uses). Missing ``hermes``? Only that parameter skips.

Run it on a real machine with:

    # hermes isn't on PATH — point at its bundled venv, same as production
    HARDLINE_LIVE_TESTS=1 HARDLINE_HERMES_CMD="C:/.../hermes.exe" python -m pytest tests/test_live_agents.py -v
"""

from __future__ import annotations

import os
import shutil
import sys
import json

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from hardline_mcp import adapters

pytestmark = pytest.mark.skipif(
    not os.environ.get("HARDLINE_LIVE_TESTS"),
    reason="live agent integration disabled; set HARDLINE_LIVE_TESTS=1 to run "
    "(spawns real agent sessions and consumes plan tokens)",
)


def _resolved_exe(agent: str) -> str:
    """The executable production would launch for this agent (honors the
    HARDLINE_*_CMD override and codex auto-discovery)."""
    return adapters._prefix_for(agent)[0]


def _agent_available(agent: str) -> bool:
    exe = _resolved_exe(agent)
    looks_like_path = os.sep in exe or (len(exe) > 1 and exe[1] == ":")
    return os.path.exists(exe) if looks_like_path else shutil.which(exe) is not None


@pytest.mark.parametrize("agent", ["hermes", "codex", "claude"])
def test_live_ask_reaches_real_agent(agent):
    if not _agent_available(agent):
        pytest.skip(
            f"{agent} CLI not reachable on this machine (resolved: {_resolved_exe(agent)!r})"
        )

    token = f"HARDLINE-LIVE-{agent.upper()}"
    result = adapters.ask(agent, f"Reply with exactly this and nothing else: {token}")

    assert result["ok"] is True, f"{agent} bridge failed: {result.get('error')}"
    assert token in result["reply"], (
        f"{agent} replied without the token (bridge reached the CLI but the "
        f"answer was unexpected): {result['reply']!r}"
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_live_claude_model_and_effort_over_mcp():
    """Real E2E: MCP schema -> server -> Claude CLI -> Fable subscription."""
    if not _agent_available("claude"):
        pytest.skip(
            f"claude CLI not reachable on this machine (resolved: {_resolved_exe('claude')!r})"
        )

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "hardline_mcp.server"],
        env=dict(os.environ),
    )
    token = "HARDLINE-FABLE-EFFORT-E2E"
    with anyio.fail_after(240):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                response = await session.call_tool(
                    "ask_claude",
                    {
                        "prompt": f"Reply with exactly: {token}",
                        "model": "fable",
                        "effort": "low",
                        "mode": "advisory",
                    },
                )

    payload = response.structuredContent
    if payload is None:
        text = next(
            (
                getattr(block, "text", None)
                for block in response.content
                if getattr(block, "text", None)
            ),
            None,
        )
        assert text is not None, response
        payload = json.loads(text)
    assert payload["ok"] is True, payload.get("error")
    assert token in payload["reply"]
    assert payload["requested_model"] == "fable"
    assert payload["actual_model"] == "claude-fable-5"
    assert payload["requested_effort"] == "low"
    assert payload["api_key_source"] == "none"
    assert payload["fallback"] is None
