"""Tests for hardline_mcp.mailbox — real temp-dir SQLite (concurrency is the point)."""

from datetime import datetime, timedelta, timezone

import pytest

from hardline_mcp import mailbox

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

    hermes_inbox, remaining = mailbox.inbox("hermes", db_path=db)
    assert len(hermes_inbox) == 1
    assert remaining == 0
    assert hermes_inbox[0]["body"] == "for hermes"
    assert hermes_inbox[0]["sender"] == "claude"
    assert hermes_inbox[0]["recipient"] == "hermes"


def test_ack_removes_from_unread_inbox(tmp_path):
    db = tmp_path / "mb.db"
    now_fn = _clock(_T0)
    r = mailbox.send("hermes", "claude", "ping", db_path=db, now_fn=now_fn)
    # auto_ack=False: this test is about the EXPLICIT ack below, and a
    # consuming read here would ack the message first and make it pass for
    # the wrong reason.
    msgs, _ = mailbox.inbox("claude", auto_ack=False, db_path=db)
    assert len(msgs) == 1

    ack = mailbox.ack(r["message_id"], db_path=db, now_fn=now_fn)
    assert ack["ok"] is True
    assert mailbox.inbox("claude", db_path=db)[0] == []
    # still visible when unread_only=False
    seen, _ = mailbox.inbox("claude", unread_only=False, db_path=db)
    assert len(seen) == 1


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
    assert len(mailbox.inbox("hermes", db_path=db)[0]) == 1


def test_unicode_body_round_trips(tmp_path):
    # The adapter decodes agent output as UTF-8; the store must be just as
    # unicode-safe. Non-ASCII bodies must survive send -> SQLite -> inbox.
    db = tmp_path / "mb.db"
    body = "café ☕ 日本語 — rocket 🚀  line-sep"
    r = mailbox.send("claude", "hermes", body, db_path=db)
    got, _ = mailbox.inbox("hermes", db_path=db)
    assert got[0]["message_id"] == r["message_id"]
    assert got[0]["body"] == body


def test_inbox_limit_caps_batch_and_reports_remaining(tmp_path):
    db = tmp_path / "mb.db"
    now_fn = _clock(_T0)
    for i in range(10):
        mailbox.send("codex", "hermes", f"m{i}", db_path=db, now_fn=now_fn)
        now_fn.tick(1)

    msgs, remaining = mailbox.inbox("hermes", limit=4, auto_ack=False, db_path=db)
    assert [m["body"] for m in msgs] == ["m0", "m1", "m2", "m3"]  # oldest first
    # auto_ack=False consumed nothing, so all 10 are still unread
    assert remaining == 10


def test_repeated_polls_advance_through_the_backlog(tmp_path):
    """The regression that makes a bare ``limit`` worse than no limit.

    Oldest-first + a limit + nothing acking pins the caller to the same first
    batch forever, so it never sees a newer message. Consuming what was
    returned is what turns the limit into a drain.
    """
    db = tmp_path / "mb.db"
    now_fn = _clock(_T0)
    for i in range(12):
        mailbox.send("codex", "hermes", f"m{i}", db_path=db, now_fn=now_fn)
        now_fn.tick(1)

    first, rem1 = mailbox.inbox("hermes", limit=5, db_path=db, now_fn=now_fn)
    second, rem2 = mailbox.inbox("hermes", limit=5, db_path=db, now_fn=now_fn)
    third, rem3 = mailbox.inbox("hermes", limit=5, db_path=db, now_fn=now_fn)

    assert [m["body"] for m in first] == ["m0", "m1", "m2", "m3", "m4"]
    assert [m["body"] for m in second] == ["m5", "m6", "m7", "m8", "m9"]
    assert [m["body"] for m in third] == ["m10", "m11"]
    assert (rem1, rem2, rem3) == (7, 2, 0)

    # drained: a fourth poll is empty rather than replaying the backlog
    assert mailbox.inbox("hermes", limit=5, db_path=db, now_fn=now_fn) == ([], 0)


def test_auto_ack_consumes_exactly_what_it_returned(tmp_path):
    db = tmp_path / "mb.db"
    now_fn = _clock(_T0)
    for i in range(6):
        mailbox.send("codex", "hermes", f"m{i}", db_path=db, now_fn=now_fn)
        now_fn.tick(1)

    returned, _ = mailbox.inbox("hermes", limit=2, db_path=db, now_fn=now_fn)
    returned_ids = {m["message_id"] for m in returned}

    everything, _ = mailbox.inbox(
        "hermes", unread_only=False, limit=100, auto_ack=False, db_path=db
    )
    acked = {m["message_id"] for m in everything if m["acked_at"] is not None}
    assert acked == returned_ids  # not one row more


def test_auto_ack_does_not_touch_another_recipients_mail(tmp_path):
    """Guards the lazy implementation: a global ``UPDATE ... WHERE acked_at
    IS NULL`` with no recipient scope, which would let one agent's poll mark
    every other agent's mail read.

    Verified by mutation: this fails under a globally-scoped ack and passes
    under a recipient-scoped one, so the tighter "only the returned ids"
    guarantee is covered by test_auto_ack_consumes_exactly_what_it_returned,
    not here.
    """
    db = tmp_path / "mb.db"
    now_fn = _clock(_T0)
    mailbox.send("claude", "hermes", "for hermes", db_path=db, now_fn=now_fn)
    mailbox.send("claude", "codex", "for codex", db_path=db, now_fn=now_fn)
    mailbox.send("claude", "claude:other.9999zzzz", "for a lane", db_path=db, now_fn=now_fn)

    mailbox.inbox("hermes", db_path=db, now_fn=now_fn)

    codex_left, _ = mailbox.inbox("codex", auto_ack=False, db_path=db)
    lane_left, _ = mailbox.inbox("claude:other.9999zzzz", auto_ack=False, db_path=db)
    assert len(codex_left) == 1
    assert len(lane_left) == 1


def test_limit_is_clamped_so_the_bound_cannot_be_opted_out_of(tmp_path):
    db = tmp_path / "mb.db"
    now_fn = _clock(_T0)
    for i in range(mailbox.MAX_INBOX_LIMIT + 25):
        mailbox.send("codex", "hermes", f"m{i}", db_path=db, now_fn=now_fn)

    msgs, remaining = mailbox.inbox("hermes", limit=10_000, auto_ack=False, db_path=db)
    assert len(msgs) == mailbox.MAX_INBOX_LIMIT
    assert remaining == mailbox.MAX_INBOX_LIMIT + 25


@pytest.mark.parametrize("bad", [0, -5, "nonsense", None])
def test_nonsense_limit_falls_back_to_a_bounded_read(tmp_path, bad):
    db = tmp_path / "mb.db"
    for i in range(40):
        mailbox.send("codex", "hermes", f"m{i}", db_path=db)
    msgs, _ = mailbox.inbox("hermes", limit=bad, auto_ack=False, db_path=db)
    assert 1 <= len(msgs) <= mailbox.MAX_INBOX_LIMIT


def test_concurrent_writers_do_not_lose_or_duplicate(tmp_path):
    # The headline durability claim: WAL mode + per-call connections let many
    # agent subprocesses write the same mailbox at once without losing or
    # corrupting messages. Hammer one db from several threads and prove every
    # message landed exactly once (unique ids, exact count, no exceptions).
    import threading

    db = tmp_path / "mb.db"
    n_threads, per_thread = 8, 30
    errors: list[Exception] = []
    barrier = threading.Barrier(n_threads)  # maximize write overlap

    def worker(tid: int) -> None:
        try:
            barrier.wait()
            for i in range(per_thread):
                mailbox.send(f"agent{tid}", "hermes", f"t{tid}-m{i}", db_path=db)
        except Exception as e:  # noqa: BLE001 - surface any concurrency failure
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent writers raised: {errors}"
    rows = mailbox.history(limit=10_000, db_path=db)
    assert len(rows) == n_threads * per_thread  # nothing lost
    ids = [r["message_id"] for r in rows]
    assert len(set(ids)) == len(ids)  # nothing duplicated
    bodies = {r["body"] for r in rows}
    assert len(bodies) == n_threads * per_thread  # every distinct message present
