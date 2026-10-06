"""``github`` survives every route a request can take to an adapter (#43).

Each route is its own whitelist of keyword arguments, and a whitelist that
forgets a key drops it silently: the reviewer runs, answers, and never saw the
PR. So every route gets a test that the evidence request arrives.
"""

import json

import pytest

from hardline_mcp import server
from test_server import _enable_quota_decision, _immediate_submit

GITHUB = {"github": "octo/repo#7", "github_exclude": ["*.lock"]}


def _recorder(calls, reply="reviewed"):
    def ask(prompt, **kwargs):
        calls.append(kwargs)
        return {"ok": True, "reply": reply}

    return ask


@pytest.fixture
def async_store(monkeypatch, tmp_path):
    monkeypatch.setenv("HARDLINE_DB", str(tmp_path / "mb.db"))
    monkeypatch.setattr(server._async_executor, "submit", _immediate_submit)


@pytest.mark.anyio
async def test_direct_claude_forwards_read_tools(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(server.adapters, "ask_claude", _recorder(calls))
    await server.ask_claude(
        prompt="review", workdir=str(tmp_path), github_tools="read", **GITHUB
    )
    assert calls[0].get("github_tools") == "read"
    assert calls[0].get("github") == "octo/repo#7"


@pytest.mark.anyio
async def test_the_reserve_guard_forwards_github(monkeypatch):
    _enable_quota_decision(monkeypatch, "claude")
    calls = []
    monkeypatch.setattr(server.adapters, "ask_claude", _recorder(calls))
    result = await server.ask_claude(prompt="review", **GITHUB)
    assert calls[0].get("github") == "octo/repo#7"
    assert calls[0].get("github_exclude") == ["*.lock"]
    assert result["routing"].get("invocation_overrides", {}).get("github") == "octo/repo#7"


@pytest.mark.anyio
async def test_a_redirect_to_chatgpt_carries_the_evidence(monkeypatch):
    _enable_quota_decision(monkeypatch, "chatgpt")
    calls = []
    monkeypatch.setattr(server.adapters, "ask_codex", _recorder(calls))
    result = await server.ask_claude(prompt="review", **GITHUB)
    assert calls[0].get("github") == "octo/repo#7"
    assert calls[0].get("github_exclude") == ["*.lock"]
    assert result["routing"]["option_mapping"].get("github", {}).get("applied") == "octo/repo#7"


@pytest.mark.anyio
async def test_async_claude_through_the_guard_forwards_github(monkeypatch, async_store):
    _enable_quota_decision(monkeypatch, "claude")
    calls = []
    monkeypatch.setattr(server.adapters, "ask_claude", _recorder(calls))
    receipt = await server.ask_claude_async(prompt="review", from_agent="codex", **GITHUB)
    assert receipt["accepted"] is True
    assert calls[0].get("github") == "octo/repo#7"
    assert calls[0].get("github_exclude") == ["*.lock"]


@pytest.mark.anyio
async def test_async_claude_redirected_forwards_github(monkeypatch, async_store):
    _enable_quota_decision(monkeypatch, "chatgpt")
    calls = []
    monkeypatch.setattr(server.adapters, "ask_codex", _recorder(calls))
    await server.ask_claude_async(prompt="review", from_agent="codex", **GITHUB)
    assert calls[0].get("github") == "octo/repo#7"


@pytest.mark.anyio
async def test_async_codex_forwards_github_and_records_only_the_reference(
    monkeypatch, async_store
):
    calls = []
    monkeypatch.setattr(server.adapters, "ask_codex", _recorder(calls))
    receipt = await server.ask_codex_async(prompt="review", from_agent="claude", **GITHUB)
    assert calls[0].get("github") == "octo/repo#7"
    request = server.jobs.get(receipt["job_id"])["request"]
    assert request.get("github") == "octo/repo#7"
    assert request.get("github_exclude") == ["*.lock"]


@pytest.mark.anyio
async def test_async_admission_rejects_bad_github_before_queueing(monkeypatch, async_store):
    calls = []
    monkeypatch.setattr(server.adapters, "ask_codex", _recorder(calls))
    receipt = await server.ask_codex_async(
        prompt="review", from_agent="claude", github="nonsense"
    )
    assert receipt["accepted"] is False and "owner/repo#123" in receipt.get("error", "")
    assert calls == [], "an invalid request must never reach a worker"


@pytest.mark.anyio
async def test_github_snapshot_tool_reaches_the_adapter(monkeypatch):
    seen = []
    monkeypatch.setattr(
        server.adapters,
        "github_snapshot",
        lambda ref, exclude=None: seen.append((ref, exclude)) or {"ok": True},
    )
    assert (await server.github_snapshot(ref="octo/repo#7", exclude=["*.md"]))["ok"]
    assert seen == [("octo/repo#7", ["*.md"])]
