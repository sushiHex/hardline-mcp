"""Tests for comms_mcp.mailbox — real temp-dir SQLite (concurrency is the point)."""

from datetime import datetime, timedelta, timezone

import pytest

from comms_mcp import mailbox

_T0 = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def _clock(start):
    """A controllable now_fn: starts at `start`, advances via .tick(seconds)."""
    state = {"now": start}

    def now_fn():
        return state["now"]

    def tick(seconds):
        state["now"] = state["now"] + timedelta(seconds=seconds)

    now_fn.tick = tick
    return now_fn


def test_send_returns_id_and_timestamp(tmp_path):
    db = tmp_path / "mb.db"
    now_fn = _clock(_T0)
    r = mailbox.send("claude", "hermes", "hello", db_path=db, now_fn=now_fn)
    assert isinstance(r["message_id"], int)
    assert r["created_at"] == "2026-07-16T12:00:00Z"


def test_inbox_returns_unread_for_recipient_only(tmp_path):
    db = tmp_path / "mb.db"
    now_fn = _clock(_T0)
    mailbox.send("claude", "hermes", "for hermes", db_path=db, now_fn=now_fn)
    mailbox.send("claude", "codex", "for codex", db_path=db, now_fn=now_fn)

    hermes_inbox = mailbox.inbox("hermes", db_path=db)
    assert len(hermes_inbox) == 1
    assert hermes_inbox[0]["body"] == "for hermes"
    assert hermes_inbox[0]["sender"] == "claude"
    assert hermes_inbox[0]["recipient"] == "hermes"


def test_ack_removes_from_unread_inbox(tmp_path):
    db = tmp_path / "mb.db"
    now_fn = _clock(_T0)
    r = mailbox.send("hermes", "claude", "ping", db_path=db, now_fn=now_fn)
    assert len(mailbox.inbox("claude", db_path=db)) == 1

    ack = mailbox.ack(r["message_id"], db_path=db, now_fn=now_fn)
    assert ack["ok"] is True
    assert mailbox.inbox("claude", db_path=db) == []
    # still visible when unread_only=False
    assert len(mailbox.inbox("claude", unread_only=False, db_path=db)) == 1


def test_ack_unknown_id_returns_false(tmp_path):
    db = tmp_path / "mb.db"
    assert mailbox.ack(99999, db_path=db)["ok"] is False


def test_ack_is_idempotent(tmp_path):
    db = tmp_path / "mb.db"
    now_fn = _clock(_T0)
    r = mailbox.send("hermes", "claude", "ping", db_path=db, now_fn=now_fn)
    assert mailbox.ack(r["message_id"], db_path=db, now_fn=now_fn)["ok"] is True
    # second ack: already acked -> ok False (no row newly changed)
    assert mailbox.ack(r["message_id"], db_path=db, now_fn=now_fn)["ok"] is False


def test_history_all_agents_newest_first_with_limit(tmp_path):
    db = tmp_path / "mb.db"
    now_fn = _clock(_T0)
    for i in range(5):
        mailbox.send("claude", "hermes", f"m{i}", db_path=db, now_fn=now_fn)
        now_fn.tick(1)
    hist = mailbox.history(limit=3, db_path=db)
    assert len(hist) == 3
    assert [h["body"] for h in hist] == ["m4", "m3", "m2"]  # newest first


def test_history_filtered_by_agent_matches_sender_or_recipient(tmp_path):
    db = tmp_path / "mb.db"
    now_fn = _clock(_T0)
    mailbox.send("claude", "hermes", "a", db_path=db, now_fn=now_fn)
    mailbox.send("codex", "claude", "b", db_path=db, now_fn=now_fn)
    mailbox.send("hermes", "codex", "c", db_path=db, now_fn=now_fn)  # no claude

    claude_hist = mailbox.history(agent="claude", db_path=db)
    bodies = {h["body"] for h in claude_hist}
    assert bodies == {"a", "b"}  # claude as sender OR recipient, not "c"


def test_survives_reopen_same_db(tmp_path):
    db = tmp_path / "mb.db"
    now_fn = _clock(_T0)
    mailbox.send("claude", "hermes", "persist", db_path=db, now_fn=now_fn)
    # fresh calls reopen the connection — data must persist on disk
    assert len(mailbox.inbox("hermes", db_path=db)) == 1
