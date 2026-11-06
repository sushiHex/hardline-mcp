"""Server-side guards the rest of the suite passed straight through
(companion to test_guards.py; the catalog case beside each is its removal)."""

from concurrent.futures import Future
from pathlib import Path

import pytest

from hardline_mcp import server
from test_server import _enable_quota_decision, anyio_backend  # noqa: F401 - fixtures


@pytest.mark.anyio
async def test_a_write_request_is_blocked_not_redirected_to_advisory(monkeypatch):
    _enable_quota_decision(monkeypatch, "chatgpt")
    monkeypatch.setattr(
        server.adapters, "ask_claude", lambda *a, **k: pytest.fail("Claude must not spawn")
    )
    redirected = []
    monkeypatch.setattr(
        server.adapters, "ask_codex", lambda *a, **k: redirected.append(k) or {"ok": True}
    )
    result = await server.ask_claude(prompt="edit it", write=True)
    assert redirected == [], "a write is not redirectable"
    assert result["ok"] is False
    assert result["routing"]["decision"] == "blocked"
    assert "write-enabled" in result["error"]


@pytest.mark.anyio
async def test_a_short_page_that_the_budget_cut_still_has_more(monkeypatch, tmp_path, flood):
    """Twenty rows are far under the limit, but not all of them fit the
    response: the caller must be told to page on."""
    db = tmp_path / "mb.db"
    monkeypatch.setenv("HARDLINE_DB", str(db))
    flood(db, ["y" * 8_000 for _ in range(20)])
    page = await server.history(agent="hermes", limit=50)
    assert page["dropped"] > 0 and len(page["messages"]) + page["dropped"] == 20
    assert page["has_more"] is True


def test_a_finished_job_leaves_the_pending_table(monkeypatch):
    class Slots:
        def release(self):
            pass

    monkeypatch.setattr(server, "_pending_jobs", {})
    future = Future()
    server._track_job(Path("db"), "job", future, Slots())
    assert list(server._pending_jobs) == [(Path("db"), "job")]
    future.set_result(None)
    assert server._pending_jobs == {}
