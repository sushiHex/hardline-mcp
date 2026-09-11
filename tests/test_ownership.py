from concurrent.futures import ThreadPoolExecutor
import os
import threading
import time

import pytest

from hardline_mcp import adapters, mailbox, sessions, server


@pytest.mark.parametrize("explicit", [False, True])
def test_acquisition_serializes_check_and_write(tmp_path, monkeypatch, explicit):
    db = tmp_path / "mb.db"
    monkeypatch.setattr(sessions, "instance_state", lambda *args: "alive")
    monkeypatch.setattr(sessions, "process_key", lambda pid: str(pid))
    original = sessions._refusal
    rendezvous = threading.Barrier(2)

    def slow_check(*args):
        result = original(*args)
        if result is None:
            time.sleep(0.3)  # force an unprotected contender to finish its SELECT
        return result

    monkeypatch.setattr(sessions, "_refusal", slow_check)

    def acquire(pid):
        rendezvous.wait(timeout=5)
        if explicit:
            return sessions.claim(agent="codex", label="shared", pid=pid, db_path=db)
        return sessions.register(
            agent="codex", lane="codex:shared", pid=pid, db_path=db
        )

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(acquire, [10001, 10002]))
    assert sum("codex:shared" in r.get("lanes", []) for r in results) == 1
    assert len(sessions.holders("codex:shared", db_path=db)) == 1


@pytest.mark.anyio
async def test_refused_registration_cannot_consume(monkeypatch, tmp_path):
    monkeypatch.setenv("HARDLINE_DB", str(tmp_path / "mb.db"))
    monkeypatch.setenv("HARDLINE_AGENT", "codex")
    monkeypatch.setenv("HARDLINE_AGENT_LABEL", "shared")
    monkeypatch.setattr(sessions, "instance_state", lambda *args: "alive")
    sessions.register(agent="codex", lane="codex:shared", pid=os.getpid() + 10000)
    message = mailbox.send("claude", "codex:shared", "only for the owner")
    server._announce_self()
    assert "contested" in server._last_registration_failure()
    batch = await server.inbox("codex")
    assert batch["messages"][0]["acked_at"] is None
    assert batch["remaining"] == 0
    assert "registration_warning" in batch
    assert (await server.ack(message["message_id"]))["ok"] is False


@pytest.mark.anyio
async def test_missing_grant_cannot_be_consumed_from_local_identity(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HARDLINE_DB", str(tmp_path / "mb.db"))
    monkeypatch.setenv("HARDLINE_AGENT", "codex")
    adapters.claim_lane("shared")
    server._announce_self()
    monkeypatch.setattr(server, "_last_heartbeat", [time.monotonic()])
    sessions.drop_lane("codex:shared")
    message = mailbox.send("claude", "codex:shared", "ungranted")
    assert (await server.ack(message["message_id"]))["ok"] is False
    assert (await server.inbox("codex"))["messages"][0]["acked_at"] is None


def test_automatic_registration_respects_unregistered_work(monkeypatch, tmp_path):
    from hardline_mcp import jobs

    db = tmp_path / "mb.db"
    jobs.create(
        agent="claude", requester="codex:shared", label=None, request={}, db_path=db
    )
    result = sessions.register(
        agent="codex", lane="codex:shared", pid=os.getpid() + 10000, db_path=db
    )
    assert result["lanes"] == []
    assert result["contested"] == ["codex:shared"]


def test_refused_retained_lane_rolls_back_entire_claim(monkeypatch, tmp_path):
    db = tmp_path / "mb.db"
    monkeypatch.setattr(sessions, "instance_state", lambda *args: "alive")
    sessions.register(
        agent="codex", lane="codex:other", pid=os.getpid() + 10000, db_path=db
    )
    result = sessions.claim(
        agent="codex", label="new", lanes=["codex:free", "codex:other"], db_path=db
    )
    assert result["ok"] is False
    assert sessions.holders("codex:free", db_path=db) == []
    assert sessions.holders("codex:new", db_path=db) == []
