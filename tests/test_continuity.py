"""Pending claims: waiting for a name instead of taking it (#39).

A conversation that Claude Code moves to a background session continues in a
new process, while the old process keeps running and keeps its lanes. Nothing
can tell that from a copy that is still being read, so the name cannot be
TAKEN - but it can be WAITED for, and the wait ends on the one piece of
positive evidence available: the holder's process exiting.

The holder here is a real child process that the test kills, so "the holder
exited" is the OS answering, not a mock agreeing.
"""

import subprocess
import sys

import pytest

from hardline_mcp import adapters, mailbox, sessions


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def codex_session(monkeypatch, tmp_path):
    db = tmp_path / "mb.db"
    monkeypatch.setenv("HARDLINE_DB", str(db))
    monkeypatch.setenv("HARDLINE_AGENT", "codex")
    return db


@pytest.fixture
def holder(codex_session):
    """A live foreign process registered as the holder of codex:construction."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
    )
    sessions.register(
        agent="codex", lane="codex:construction", pid=proc.pid, db_path=codex_session
    )
    assert sessions.holders("codex:construction", db_path=codex_session)
    yield proc
    proc.kill()
    proc.wait()


def _exit(proc):
    proc.kill()
    proc.wait()


def _unread(db, lane):
    with mailbox._connect(db) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM messages WHERE recipient = ? AND acked_at IS NULL",
            (lane,),
        ).fetchone()[0]


@pytest.mark.anyio
async def test_a_waiting_claim_is_granted_with_its_backlog_once_the_holder_exits(
    codex_session, holder
):
    from hardline_mcp import server

    mailbox.send("claude", "codex:construction", "sent while moving", db_path=codex_session)

    waiting = await server.register_session(label="construction", wait=True)
    assert waiting["ok"] is False, "a pending claim is not ownership"
    assert waiting["status"] == "pending"
    assert waiting["lane"] == "codex:construction"
    assert "construction" not in adapters.held_lanes()
    listing = await server.list_agents()
    assert listing["you"]["pending_claims"] == ["codex:construction"]

    # While the holder lives, nothing here may consume its mail.
    await server.inbox(agent="codex")
    assert _unread(codex_session, "codex:construction") == 1

    _exit(holder)
    assert server._fulfil_pending() == ["codex:construction"]
    assert "construction" in adapters.held_lanes()
    assert adapters.pending_claims() == {}

    got = await server.inbox(agent="codex")
    assert [m["body"] for m in got["messages"]] == ["sent while moving"]
    assert _unread(codex_session, "codex:construction") == 0


@pytest.mark.anyio
async def test_a_waiting_claim_is_never_granted_while_the_holder_lives(
    codex_session, holder
):
    from hardline_mcp import server

    await server.register_session(label="construction", wait=True)
    assert server._fulfil_pending() == []
    assert "construction" not in adapters.held_lanes()
    assert adapters.pending_claims() == {"construction": "codex"}
    assert [h["pid"] for h in sessions.holders("codex:construction")] == [holder.pid]


@pytest.mark.anyio
async def test_without_wait_a_held_name_is_refused_and_not_awaited(
    codex_session, holder
):
    from hardline_mcp import server

    refused = await server.register_session(label="construction")
    assert refused["ok"] is False
    assert "status" not in refused
    assert adapters.pending_claims() == {}


@pytest.mark.anyio
async def test_release_cancels_a_waiting_claim(codex_session, holder):
    from hardline_mcp import server

    await server.register_session(label="construction", wait=True)
    released = await server.release_session(label="construction")
    assert released == {
        "ok": True,
        "cancelled": "codex:construction",
        "note": "This session is no longer waiting for that name.",
    }

    _exit(holder)
    assert server._fulfil_pending() == []
    assert "construction" not in adapters.held_lanes()


@pytest.mark.anyio
async def test_the_heartbeat_fulfils_a_waiting_claim(codex_session, holder):
    """Fulfilment has to happen without anyone asking again.

    A session that moved has no reason to call register_session a second time;
    the paths it already runs must notice the holder is gone.
    """
    from hardline_mcp import server

    await server.register_session(label="construction", wait=True)
    _exit(holder)
    server._last_heartbeat.clear()
    server._heartbeat()
    assert "construction" in adapters.held_lanes()


@pytest.mark.anyio
async def test_a_waiting_claim_counts_toward_the_name_cap(
    codex_session, holder, monkeypatch
):
    """Each awaited name becomes a bind parameter on every poll once granted."""
    from hardline_mcp import server

    monkeypatch.setattr(adapters, "MAX_CLAIMED_LANES", 1)
    assert (await server.register_session(label="construction", wait=True))[
        "status"
    ] == "pending"
    refused = await server.register_session(label="another")
    assert refused["ok"] is False
    assert "awaits" in refused["error"]


def test_the_standing_rule_reaches_every_connected_model():
    """Sessions in other repositories never read these docs; the host delivers this."""
    from hardline_mcp import server

    assert "wait=true" in server.mcp._mcp_server.instructions
