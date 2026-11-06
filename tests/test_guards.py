"""Guards that the rest of the suite passed straight through.

Each test pins one line the whole suite once survived the removal of; the
catalog case beside it (tests/mutations/) is the removal.
"""

from datetime import datetime, timezone

import pytest

from hardline_mcp import channel, procid, watch
from test_announce_batch import mail, wake  # noqa: F401 - fixture
from test_channel import LANE, store  # noqa: F401 - fixture
from test_watch import exercise, send, target  # noqa: F401 - fixture


def test_a_second_outage_after_recovery_gets_its_own_grace(target, monkeypatch):
    """Recovery forgets the first outage: the second one must not be judged
    against it and end the observer on its first failed read."""
    send(target, "codex")
    read = watch.read_pending
    state = {"offline": False}

    def sometimes(t):
        if state["offline"]:
            raise watch.Unavailable("offline")
        return read(t)

    monkeypatch.setattr(watch, "read_pending", sometimes)
    result, _, reports = exercise(
        target,
        duration=105,
        remind_after=500,
        tick=lambda t: state.update(offline=t in (1, 2, 100)),
    )
    assert result == 0, reports
    assert reports.count("observation recovered") == 2
    assert [r for r in reports if r.startswith("unavailable:")] == ["unavailable: offline"] * 2


def test_a_sweep_that_resumed_mid_backlog_does_not_forget_announced_mail(wake, monkeypatch):
    """Only a sweep from the front sees all unread mail, so only it may forget
    ids that are no longer there; one resumed past them has not looked."""
    monkeypatch.setattr(channel, "_SCAN", 2)
    monkeypatch.setattr(channel, "_PAGES", 1)
    first, second = mail(), mail()
    mail()
    assert wake.poll() is not None
    assert wake.announced == {first, second}
    assert wake.poll() is None, "the notice is still outstanding"
    assert wake.announced == {first, second}, "the end was reached, but not from the front"


def test_a_pusher_sweep_that_resumed_mid_backlog_keeps_its_reminder_schedule(store, monkeypatch):
    monkeypatch.setattr(channel, "_SCAN", 2)
    monkeypatch.setattr(channel, "_PAGES", 1)
    ids = [channel.mailbox.send("codex", LANE, f"m{i}")["message_id"] for i in range(3)]
    pusher = channel.Pusher()
    _, pushed, nonce, now = pusher.next_batch()
    pusher.sent(pushed, nonce, now)
    assert set(pusher.schedule) == set(ids[:2])
    pusher.next_batch()  # resumes after the second id and reaches the end
    assert set(pusher.schedule) >= set(ids[:2]), "reminders of mail not yet read are kept"


def test_an_older_fact_snapshot_never_overwrites_a_newer_one(monkeypatch):
    written = []
    monkeypatch.setattr(channel.delivery, "record", lambda pid, key, **facts: written.append(facts))
    assert procid.current_identity()[1] is not None
    pusher = channel.Pusher()
    stamp = datetime.now(timezone.utc)
    with pusher._lock:
        pusher.facts = {"declared_at": stamp}
        older = pusher._snapshot()
        pusher.facts = {"declared_at": stamp, "last_push_at": stamp}
        newer = pusher._snapshot()
    pusher._write(newer)
    pusher._write(older)
    assert written == [{"declared_at": stamp, "last_push_at": stamp}]
