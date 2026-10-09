"""Names a conversation held before: told after a restart, never taken back for it.

A claimed name lives in its process, so a relaunched or reconnected
conversation comes back without it. Restoring it automatically was reviewed
and rejected (docs/session-continuity.md): these tests pin that a hint is
advice - it claims nothing, routes nothing, reads no mail - and that the
conversation is still told, where it looks and when mail arrives.
"""

import sqlite3
import subprocess
import sys

import pytest

from hardline_mcp import adapters, announce, channel, hints, mailbox, server, sessions

CONVERSATION = "1a2b3c4d-dead-beef-0000-000000000000"  # in_session's id
OLD = "claude:fonts.0ld0ld00"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def store(monkeypatch, tmp_path):
    db = tmp_path / "mb.db"
    monkeypatch.setenv("HARDLINE_DB", str(db))
    return db


# ── the record ──────────────────────────────────────────────────────────────


def test_a_name_is_hinted_to_the_conversation_that_held_it_only(store):
    hints.record(OLD, CONVERSATION)
    assert hints.held_before(CONVERSATION) == [OLD]
    assert hints.held_before("another-conversation") == []
    assert hints.held_before(None) == []


def test_the_next_grant_of_a_name_replaces_its_hint(store):
    """Whoever holds the name now, the earlier conversation's hint is stale."""
    hints.record(OLD, CONVERSATION)
    hints.record(OLD, "another-conversation")
    assert hints.held_before(CONVERSATION) == []
    assert hints.held_before("another-conversation") == [OLD]


def test_a_claimant_hardline_cannot_name_clears_the_hint(store):
    """A Codex or Hermes session has no conversation id, and holds it now."""
    hints.record(OLD, CONVERSATION)
    hints.record(OLD, None)
    assert hints.held_before(CONVERSATION) == []


def test_only_the_conversation_that_held_a_name_forgets_it(store):
    hints.record(OLD, CONVERSATION)
    assert hints.forget(OLD, "another-conversation") is False
    assert hints.held_before(CONVERSATION) == [OLD]
    assert hints.forget(OLD, CONVERSATION) is True
    assert hints.held_before(CONVERSATION) == []


def test_a_writer_forgets_only_the_hint_its_grant_wrote(store):
    hints.record(OLD, CONVERSATION, "window-b")
    assert hints.forget(OLD, CONVERSATION, "window-a") is False
    assert hints.held_before(CONVERSATION) == [OLD]
    assert hints.forget(OLD, CONVERSATION, "window-b") is True


def test_hints_are_bounded_newest_kept(store, monkeypatch):
    names = [f"claude:n{i}" for i in range(hints.PER_CONVERSATION + 3)]
    for name in names:
        hints.record(name, CONVERSATION)
    assert hints.held_before(CONVERSATION) == names[3:]
    monkeypatch.setattr(hints, "ROWS", 2)
    hints.record("claude:a", "c1")
    hints.record("claude:b", "c2")
    assert hints.held_before(CONVERSATION) == []
    assert hints.held_before("c1") == ["claude:a"]


def test_a_store_without_the_table_has_no_hints(store):
    """A store an older revision built: nothing written, nothing to tell."""
    hints.record(OLD, CONVERSATION)
    with sqlite3.connect(store) as conn:
        conn.execute("DROP TABLE lane_hints")
    assert hints.held_before(CONVERSATION) == []


# ── the server ──────────────────────────────────────────────────────────────


def _restart(lane):
    """What a relaunch or reconnect leaves: a new process, the old one gone."""
    adapters.reset_claimed_lanes()
    sessions.drop_lane(lane)


@pytest.mark.anyio
async def test_a_restarted_conversation_is_told_what_it_held_and_holds_none_of_it(
    store, in_session
):
    granted = await server.register_session(label="fonts.0ld0ld00")
    assert granted["ok"]
    assert hints.held_before(CONVERSATION) == [OLD], "keyed on the whole conversation id"
    _restart(OLD)
    mailbox.send("codex", "claude:elsewhere", "so no id equals a count", db_path=store)
    mailbox.send("codex", OLD, "for whoever answers to it", db_path=store)

    you = (await server.list_agents())["you"]
    assert you.get("previously_held") == [{"lane": OLD, "unread": 1}]
    assert "previously_held_note" in you
    assert OLD not in adapters.owned_recipients(), "a hint claims nothing"

    read = await server.inbox(agent="claude", auto_ack=True)
    assert read["count"] == 0, "and reads nothing"
    assert read.get("previously_held") == [{"lane": OLD, "unread": 1}]
    assert mailbox.inbox([OLD], db_path=store, auto_ack=False), "the mail still waits"


@pytest.mark.anyio
async def test_a_read_mentions_only_names_with_mail_waiting(store, in_session):
    await server.register_session(label="fonts.0ld0ld00")
    _restart(OLD)
    assert "previously_held" not in await server.inbox(agent="claude")
    assert (await server.list_agents())["you"].get("previously_held") == [{"lane": OLD, "unread": 0}]


@pytest.mark.anyio
async def test_a_name_held_or_awaited_again_is_no_longer_hinted(store, in_session):
    await server.register_session(label="fonts.0ld0ld00")
    assert "previously_held" not in (await server.list_agents())["you"]
    _restart(OLD)
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL
    )
    try:
        # A live holder, so list_agents' own fulfilment keeps the claim waiting.
        sessions.register(agent="claude", lane=OLD, pid=holder.pid, db_path=store)
        waiting = await server.register_session(label="fonts.0ld0ld00", wait=True)
        assert waiting.get("status") == "pending"
        assert "previously_held" not in (await server.list_agents())["you"]
    finally:
        holder.kill()
        holder.wait()


@pytest.mark.anyio
async def test_another_agents_name_is_never_suggested(store, in_session):
    """register_session here could only ask for this agent's name."""
    hints.record("codex:review", CONVERSATION)
    assert "previously_held" not in (await server.list_agents())["you"]
    assert (await server.release_session(label="review"))["ok"] is False


@pytest.mark.anyio
async def test_an_automatic_grant_clears_the_hint_for_its_name(store, in_session, monkeypatch):
    """A pinned lane taken at startup has moved on as surely as a claimed one."""
    monkeypatch.setenv("HARDLINE_AGENT_LABEL", "pinned")
    hints.record("claude:pinned", "another-conversation")
    assert server._announce_self() == "claude:pinned"
    assert hints.held_before("another-conversation") == []


@pytest.mark.anyio
async def test_a_release_never_forgets_a_newer_grants_hint(store, in_session):
    """Another window on this transcript can claim the name the moment the
    release commits; its hint is not this release's to remove."""
    await server.register_session(label="fonts.0ld0ld00")
    hints.record(OLD, CONVERSATION, "another-window")
    assert (await server.release_session(label="fonts.0ld0ld00"))["ok"]
    assert hints.held_before(CONVERSATION) == [OLD]


@pytest.mark.anyio
async def test_releasing_a_name_forgets_it(store, in_session):
    await server.register_session(label="fonts.0ld0ld00")
    released = await server.release_session(label="fonts.0ld0ld00")
    assert released["ok"] and "released" in released
    assert hints.held_before(CONVERSATION) == []


@pytest.mark.anyio
async def test_a_hint_not_wanted_is_forgotten_by_release(store, in_session):
    """The dismissal path: release refuses a name not held, except to forget it."""
    await server.register_session(label="fonts.0ld0ld00")
    _restart(OLD)
    hints.record(OLD, CONVERSATION, "an-earlier-process")  # whichever wrote it
    forgotten = await server.release_session(label="fonts.0ld0ld00")
    assert forgotten == {
        "ok": True,
        "forgotten": OLD,
        "note": "No longer listed as a name this conversation held before.",
    }
    assert hints.held_before(CONVERSATION) == []
    again = await server.release_session(label="fonts.0ld0ld00")
    assert again["ok"] is False


# ── the wake ────────────────────────────────────────────────────────────────


OWN = "claude:fonts.1a2b3c4d"


@pytest.fixture
def held(store, monkeypatch):
    """A real store; this process owns OWN and held OLD before. Set
    ``held.before`` and ``held.owned`` to move a name between the two."""

    class Held:
        before = [OLD]
        owned = (OWN,)

    monkeypatch.setattr(channel, "held_before", lambda: list(Held.before))
    monkeypatch.setattr(adapters, "owned_recipients", lambda agent=None: Held.owned)
    monkeypatch.setattr(announce.sessions, "granted", lambda owned: list(owned))
    monkeypatch.setattr(channel.sessions, "granted", lambda owned: list(owned))
    monkeypatch.setattr(announce.delivery, "record", lambda pid, key, **f: None)
    monkeypatch.setattr(announce.delivery, "prune", lambda: None)
    monkeypatch.setattr(announce.procid, "current_identity", lambda: (1, "key"))
    Held.send = staticmethod(
        lambda lane, body="x": mailbox.send("codex", lane, body, db_path=store)["message_id"]
    )
    return Held


@pytest.fixture
def wake(held):
    w = announce.Announcer(address="here")
    with w._lock:
        w.declare()
    return w


def test_counts_are_exact_however_much_is_waiting(held, monkeypatch):
    """One grouped read: a paged scan stopped short and lost the names past it."""
    monkeypatch.setattr(channel, "_SCAN", 2)
    monkeypatch.setattr(channel, "_PAGES", 1)
    ids = [held.send(OLD) for _ in range(5)]
    other = held.send("claude:other")
    assert channel.waiting((OLD, "claude:other", "claude:none")) == {
        OLD: (max(ids), 5),
        "claude:other": (other, 1),
    }


def test_mail_at_a_name_held_before_wakes_the_session_per_newer_mail(wake, held):
    held.send(OLD)
    _, nonce, ids = wake.poll()
    assert ids == set(), "nothing of it is this session's to announce"
    assert wake.accept_receipt(nonce)
    assert wake.poll() is None, "told once"
    held.send(OLD)
    assert wake.poll() is not None, "newer mail tells again"


def test_being_told_never_suppresses_the_notice_after_a_reclaim(wake, held):
    """Tracked apart from announced mail: once the name is held again, the
    same message is new mail to announce."""
    mail = held.send(OLD)
    _, nonce, _ = wake.poll()
    wake.accept_receipt(nonce)
    held.before, held.owned = [], (OWN, OLD)
    due = wake.poll()
    assert due is not None and due[2] == {mail}


def test_a_pointer_taken_back_is_told_again(wake, held):
    held.send(OLD)
    _, nonce, ids = wake.poll()
    wake.take_back(nonce, ids)
    assert wake.poll() is not None


def test_names_held_before_that_cannot_be_read_never_stop_the_mail_held(wake, held, monkeypatch):
    def broken(recipients):
        raise RuntimeError("hint store unreadable")

    monkeypatch.setattr(channel, "waiting", broken)
    mail = held.send(OWN, "yours")
    try:
        due = wake.poll()
        batch = channel.Pusher().next_batch()
    except RuntimeError as exc:
        raise AssertionError("a hint failure stopped the mail this session holds") from exc
    assert due is not None and due[2] == {mail}
    params, ids, _, _ = batch
    assert ids == [mail] and "yours" in params["content"]


def test_a_push_names_the_lane_once_and_never_shows_the_mail(held, monkeypatch):
    """Pushed lines carry senders and previews of mail this session holds;
    mail at a name it does not hold stays unread and unseen."""
    held.send(OLD, "not for this session's eyes")
    monkeypatch.setattr(channel, "bodies", lambda ids: pytest.fail("fetched a body"))
    pusher = channel.Pusher()
    params, _, _, _ = pusher.next_batch()
    assert params["content"] == channel.pointer(OLD)
    assert "register_session(label='fonts.0ld0ld00', wait=true)" in params["content"]
    pusher.notices.clear()
    assert pusher.next_batch() is None, "told once"
    held.before = []  # hidden while awaited, then the wait is cancelled
    assert pusher.next_batch() is None
    held.before = [OLD]
    assert pusher.next_batch() is None, "not told the same mail twice"


def test_a_push_after_a_reclaim_is_not_delayed_by_having_told(held):
    held.send(OLD, "waiting since the restart")
    pusher = channel.Pusher()
    pusher.next_batch()
    pusher.notices.clear()
    held.before, held.owned = [], (OWN, OLD)
    batch = pusher.next_batch()
    assert batch is not None and "waiting since the restart" in batch[0]["content"]
