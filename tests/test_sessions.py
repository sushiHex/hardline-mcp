"""Tests for the session registry — real temp-dir SQLite, real process probes.

The registry's whole claim is that liveness is DERIVED, not stored, so the
tests that matter here are the ones where a recorded process is gone or is no
longer itself. A test that only ever registers live sessions would pass against
an implementation that hardcoded ``live = True``.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest

from hardline_mcp import adapters, mailbox, procid, sessions

_T0 = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)

# A pid that is real enough to be well-formed and reliably absent. The existing
# jobs tests use the same value for the same reason.
_DEAD_PID = 0x7FFFFFFE


def _clock(start):
    state = {"now": start}

    def now_fn():
        return state["now"]

    def tick(seconds):
        state["now"] = state["now"] + timedelta(seconds=seconds)

    now_fn.tick = tick
    return now_fn


# ── liveness is derived, never stored ────────────────────────────────────────


def test_a_registered_session_whose_process_is_gone_is_not_live(tmp_path):
    db = tmp_path / "mb.db"
    sessions.register(agent="codex", lane="codex:ghost", pid=_DEAD_PID, db_path=db)
    # The row exists...
    with mailbox._connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    # ...but nothing reports it as a destination.
    assert sessions.live(db_path=db) == []
    assert sessions.holders("codex:ghost", db_path=db) == []


def test_reading_prunes_the_dead_row(tmp_path):
    db = tmp_path / "mb.db"
    sessions.register(agent="codex", lane="codex:ghost", pid=_DEAD_PID, db_path=db)
    sessions.live(db_path=db)
    with mailbox._connect(db) as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert remaining == 0, "a dead session must not accumulate as a stale destination"


def test_this_process_is_live(tmp_path):
    db = tmp_path / "mb.db"
    sessions.register(agent="claude", lane="claude:me", db_path=db)
    lanes = [s["lane"] for s in sessions.live(db_path=db)]
    assert lanes == ["claude:me"]


def test_a_reused_pid_does_not_inherit_the_previous_sessions_lane(tmp_path):
    """The identity check, not just the liveness check.

    Registering OUR pid with somebody else's creation-time token is exactly
    what a reused pid looks like: the process is alive, but it is not the one
    the row describes. Without the process_key comparison this reads as live
    and the new process silently inherits the old session's name and mail.
    """
    db = tmp_path / "mb.db"
    sessions.register(agent="codex", lane="codex:old", db_path=db)
    with mailbox._connect(db) as conn:
        with conn:
            conn.execute(
                "UPDATE sessions SET process_key = ? WHERE pid = ?",
                ("not-the-key-this-process-has", os.getpid()),
            )
    assert sessions.live(db_path=db) == []


def test_instance_alive_allows_an_unrecorded_identity(tmp_path):
    """A row written where the platform would not say must not read as dead.

    Mirrors kill_process_tree: an unverifiable identity is allowed through,
    because refusing there would mean the feature never works at all.
    """
    assert procid.instance_alive(os.getpid(), None) is True
    assert procid.instance_alive(_DEAD_PID, None) is False


# ── registration ─────────────────────────────────────────────────────────────


def test_register_is_idempotent_and_keeps_started_at(tmp_path):
    db = tmp_path / "mb.db"
    now_fn = _clock(_T0)
    sessions.register(agent="claude", lane="claude:a", db_path=db, now_fn=now_fn)
    now_fn.tick(600)
    sessions.register(agent="claude", lane="claude:a", db_path=db, now_fn=now_fn)

    rows = sessions.live(db_path=db)
    assert len(rows) == 1, "one row per process, not one per heartbeat"
    assert rows[0]["started_at"] == "2026-08-30T12:00:00Z", "started_at must not reset"
    assert rows[0]["last_seen"] == "2026-08-30T12:10:00Z", "last_seen must advance"


def test_unregister_removes_the_row(tmp_path):
    db = tmp_path / "mb.db"
    sessions.register(agent="claude", lane="claude:a", db_path=db)
    assert sessions.unregister(db_path=db) is True
    assert sessions.live(db_path=db) == []
    assert sessions.unregister(db_path=db) is False


def test_live_filters_by_agent(tmp_path):
    db = tmp_path / "mb.db"
    sessions.register(agent="claude", lane="claude:a", pid=os.getpid(), db_path=db)
    # A second live row needs a second live pid; the parent of this process is
    # a real, running process that is not us.
    other = os.getppid()
    if other in (0, os.getpid()) or not procid.pid_alive(other):
        pytest.skip("no second live pid available to register")
    sessions.register(agent="codex", lane="codex:b", pid=other, db_path=db)

    assert [s["lane"] for s in sessions.live(agent="claude", db_path=db)] == ["claude:a"]
    assert [s["lane"] for s in sessions.live(agent="codex", db_path=db)] == ["codex:b"]


# ── claiming a name ──────────────────────────────────────────────────────────


def test_claim_registers_the_qualified_lane(tmp_path):
    db = tmp_path / "mb.db"
    result = sessions.claim(agent="codex", label="construction", db_path=db)
    assert result["ok"] is True
    assert result["lane"] == "codex:construction"
    assert [s["lane"] for s in sessions.live(db_path=db)] == ["codex:construction"]


def test_claim_is_refused_when_a_live_session_holds_the_name(tmp_path):
    db = tmp_path / "mb.db"
    other = os.getppid()
    if other in (0, os.getpid()) or not procid.pid_alive(other):
        pytest.skip("no second live pid available to hold the lane")
    sessions.register(
        agent="codex", lane="codex:construction", pid=other, db_path=db
    )
    result = sessions.claim(agent="codex", label="construction", db_path=db)
    assert result["ok"] is False
    assert "already held" in result["error"]
    assert str(other) in result["error"], "the refusal must name who holds it"


def test_claim_takes_over_a_name_whose_holder_died(tmp_path):
    """A dead holder's claim means nothing.

    Refusing on its behalf would make a label unusable forever after the
    session that used it crashed - which, in a system whose defining failure is
    mail stranded on dead lanes, is the wrong direction to fail in.
    """
    db = tmp_path / "mb.db"
    sessions.register(
        agent="codex", lane="codex:construction", pid=_DEAD_PID, db_path=db
    )
    result = sessions.claim(agent="codex", label="construction", db_path=db)
    assert result["ok"] is True
    assert [s["pid"] for s in sessions.live(db_path=db)] == [os.getpid()]


def test_reclaiming_your_own_name_is_not_a_conflict(tmp_path):
    db = tmp_path / "mb.db"
    assert sessions.claim(agent="codex", label="c", db_path=db)["ok"] is True
    assert sessions.claim(agent="codex", label="c", db_path=db)["ok"] is True


# ── label validation ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "label",
    [
        "",
        "   ",
        "has space",
        "has:colon",  # ':' separates agent from lane
        ".leading-dot",
        "a" * 65,
        "codex",  # a roster name, so "codex:codex"
        "claude",
    ],
)
def test_bad_labels_are_rejected(label):
    assert adapters.validate_label(label) is not None


@pytest.mark.parametrize("label", ["construction", "a", "web-2", "main.1", "A_b"])
def test_good_labels_are_accepted(label):
    assert adapters.validate_label(label) is None


# ── held lanes: a rename must never strand mail ──────────────────────────────


def test_a_renamed_session_still_owns_its_previous_lane(monkeypatch, in_session):
    """The stranding regression, stated directly.

    An async result's recipient is fixed when the job is DISPATCHED, so mail
    can already be in flight to the old lane when a rename lands. If ownership
    moved instead of growing, that result would arrive addressed to a lane this
    session no longer holds - unconsumable, because only the holder may ack it.
    """
    assert adapters.held_lanes() == (in_session,)
    adapters.claim_lane("construction")

    assert adapters.lane_suffix() == "construction", "addressed by the new name"
    assert adapters.held_lanes() == (in_session, "construction"), (
        "but still owns the old one"
    )
    assert adapters.lane_for("claude") == "claude:construction"


def test_a_claim_overrides_the_env_label(monkeypatch):
    monkeypatch.setenv("HARDLINE_AGENT_LABEL", "from-env")
    assert adapters.lane_suffix() == "from-env"
    adapters.claim_lane("from-runtime")
    assert adapters.lane_suffix() == "from-runtime", "last writer of a name wins"
    assert adapters.held_lanes() == ("from-env", "from-runtime")


def test_mail_sent_to_the_old_lane_is_still_consumable_after_a_rename(
    tmp_path, in_session
):
    db = tmp_path / "mb.db"
    # Addressed while the session was still called `in_session`.
    mailbox.send("codex", f"claude:{in_session}", "your result", db_path=db)
    adapters.claim_lane("construction")

    msgs, remaining = mailbox.inbox(
        [f"claude:{in_session}"],
        lane_suffix=adapters.held_lanes(),
        db_path=db,
    )
    assert [m["body"] for m in msgs] == ["your result"]
    assert msgs[0]["acked_at"] is not None, "must be CONSUMED, not merely shown"
    assert remaining == 0


def test_ack_honours_every_held_lane(tmp_path, in_session):
    db = tmp_path / "mb.db"
    sent = mailbox.send("codex", f"claude:{in_session}", "old lane", db_path=db)
    adapters.claim_lane("construction")
    result = mailbox.ack(
        sent["message_id"], lane_suffix=adapters.held_lanes(), db_path=db
    )
    assert result["ok"] is True


def test_holding_several_lanes_still_excludes_other_sessions(tmp_path):
    """Widening ownership must not widen it to everyone.

    The failure mode being guarded is an implementation that, given several
    lanes, drops the condition entirely - which reads as "all my lanes work"
    on every happy-path test while quietly draining other sessions' mail.
    """
    db = tmp_path / "mb.db"
    sent = mailbox.send("codex", "claude:someone-else", "not yours", db_path=db)
    assert mailbox.ack(
        sent["message_id"], lane_suffix=("mine", "also-mine"), db_path=db
    ) == {"ok": False}

    msgs, remaining = mailbox.inbox(
        ["claude:someone-else"], lane_suffix=("mine", "also-mine"), db_path=db
    )
    assert [m["body"] for m in msgs] == ["not yours"], "shown"
    assert msgs[0]["acked_at"] is None, "but never consumed"
    assert remaining == 0, "and not counted as work this caller can drain"


def test_a_process_with_no_lane_still_owns_no_lane(tmp_path):
    db = tmp_path / "mb.db"
    sent = mailbox.send("codex", "claude:somebody", "lane mail", db_path=db)
    assert mailbox.ack(sent["message_id"], lane_suffix=(), db_path=db) == {"ok": False}
    assert mailbox.ack(sent["message_id"], lane_suffix=None, db_path=db) == {"ok": False}


def test_lane_ackable_accepts_a_bare_string_and_a_collection():
    """The normalizer's compatibility promise, which every existing caller relies on."""
    assert mailbox._lane_ackable("claude:a", "a") is True
    assert mailbox._lane_ackable("claude:a", ("b", "a")) is True
    assert mailbox._lane_ackable("claude:a", "b") is False
    assert mailbox._lane_ackable("claude:a", ("b", "c")) is False
    assert mailbox._lane_ackable("claude", ()) is True


# ── self_agent inference ─────────────────────────────────────────────────────


def test_self_agent_is_none_when_nothing_identifies_the_session(monkeypatch):
    """Codex and Hermes hand over nothing, and a guess would be a lie."""
    assert adapters.self_agent() is None


def test_self_agent_infers_a_claude_session(monkeypatch, in_session):
    assert adapters.self_agent() == "claude"


def test_hardline_agent_declares_identity_explicitly(monkeypatch):
    monkeypatch.setenv("HARDLINE_AGENT", "codex")
    assert adapters.self_agent() == "codex"


def test_an_unknown_hardline_agent_is_ignored_rather_than_trusted(monkeypatch):
    monkeypatch.setenv("HARDLINE_AGENT", "clod")
    assert adapters.self_agent() is None


# ── the server tools ─────────────────────────────────────────────────────────


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def codex_session(monkeypatch, tmp_path):
    """A Codex session: knows which agent it is, holds no lane of its own."""
    db = tmp_path / "mb.db"
    monkeypatch.setattr(mailbox, "_DEFAULT_PATH", db)
    monkeypatch.setenv("HARDLINE_AGENT", "codex")
    return db


@pytest.mark.anyio
async def test_register_session_makes_this_session_addressable(codex_session):
    """The end-to-end answer to 'send a message to the codex construction session'.

    Before the claim there is no such destination; after it, mail aimed at the
    lane reaches this session and only this session.
    """
    from hardline_mcp import server

    before = await server.send(
        from_agent="claude", to_agent="codex:construction", message="ping"
    )
    assert before["ok"] is True
    assert "no live session holds" in before["warning"]

    claimed = await server.register_session(label="construction")
    assert claimed["ok"] is True
    assert claimed["lane"] == "codex:construction"

    after = await server.send(
        from_agent="claude", to_agent="codex:construction", message="ping again"
    )
    assert "warning" not in after, "a live holder means no warning"

    got = await server.inbox(agent="codex")
    assert [m["body"] for m in got["messages"]] == ["ping", "ping again"]


@pytest.mark.anyio
async def test_inbox_still_collects_mail_addressed_before_a_rename(
    monkeypatch, tmp_path, in_session
):
    """Through the SERVER tool, not just the mailbox function.

    inbox() builds the list of recipients to read from, and reading only the
    CURRENT lane looks correct in every test where the session never had
    another one. A Claude session does: it starts with a lane derived from its
    session id, and an async result dispatched before a rename is addressed to
    that one. Found by mutation - the codex end-to-end test passed against an
    implementation that dropped every previous lane.
    """
    from hardline_mcp import server

    db = tmp_path / "mb.db"
    monkeypatch.setattr(mailbox, "_DEFAULT_PATH", db)

    mailbox.send("codex", f"claude:{in_session}", "dispatched earlier", db_path=db)
    claimed = await server.register_session(label="construction")
    assert claimed["ok"] is True
    mailbox.send("codex", "claude:construction", "dispatched after", db_path=db)

    got = await server.inbox(agent="claude")
    assert [m["body"] for m in got["messages"]] == [
        "dispatched earlier",
        "dispatched after",
    ]
    assert all(m["acked_at"] for m in got["messages"]), "both must be consumed"


@pytest.mark.anyio
async def test_register_session_rejects_a_bad_label(codex_session):
    from hardline_mcp import server

    result = await server.register_session(label="has:colon")
    assert result["ok"] is False
    assert "invalid label" in result["error"]
    assert adapters.lane_suffix() == "", "a rejected label must not be adopted"


@pytest.mark.anyio
async def test_register_session_needs_an_agent_it_cannot_infer(monkeypatch, tmp_path):
    from hardline_mcp import server

    monkeypatch.setattr(mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")
    result = await server.register_session(label="construction")
    assert result["ok"] is False
    assert "cannot tell which agent" in result["error"]

    ok = await server.register_session(label="construction", agent="codex")
    assert ok["ok"] is True


@pytest.mark.anyio
async def test_a_refused_claim_does_not_rename_this_process(codex_session):
    """Ordering, not just outcome.

    Adopting the lane before the registry accepted it would rename this process
    to a name it LOST the race for, and every result it dispatched afterwards
    would be addressed to a lane somebody else holds.
    """
    from hardline_mcp import server

    other = os.getppid()
    if other in (0, os.getpid()) or not procid.pid_alive(other):
        pytest.skip("no second live pid available to hold the lane")
    sessions.register(
        agent="codex", lane="codex:construction", pid=other, db_path=codex_session
    )

    result = await server.register_session(label="construction")
    assert result["ok"] is False
    assert adapters.lane_suffix() == "", "must not have adopted the contested name"
    assert adapters.held_lanes() == ()


@pytest.mark.anyio
async def test_send_does_not_warn_about_a_bare_recipient(codex_session):
    """Bare names are shared by design; warning about them would cry wolf on
    every ordinary cross-agent message."""
    from hardline_mcp import server

    result = await server.send(from_agent="claude", to_agent="codex", message="hi")
    assert result["ok"] is True
    assert "warning" not in result


@pytest.mark.anyio
async def test_send_still_persists_the_message_it_warns_about(codex_session):
    """A warning, not a rejection.

    A Claude lane is keyed on the session id and survives a /mcp reconnect, so
    a session briefly between hardline processes will come back to the same
    lane and collect its mail. Refusing would break that; the caller is simply
    told what it did.
    """
    from hardline_mcp import server

    result = await server.send(
        from_agent="claude", to_agent="claude:gone.deadbeef", message="held"
    )
    assert result["ok"] is True
    assert isinstance(result["message_id"], int)
    kept, _ = mailbox.inbox(
        "claude:gone.deadbeef", auto_ack=False, db_path=codex_session
    )
    assert [m["body"] for m in kept] == ["held"]


@pytest.mark.anyio
async def test_list_agents_separates_live_sessions_from_mailbox_history(
    codex_session,
):
    """The misreport this feature exists to end.

    A dead session's lane is indistinguishable from a live one in the mailbox,
    so `observed_recipients` presented 11 dead lanes as destinations. Liveness
    has to come from the registry, and the two must not be conflated.
    """
    from hardline_mcp import server

    mailbox.send("claude", "codex:long-gone", "stranded", db_path=codex_session)
    await server.register_session(label="construction")
    mailbox.send("claude", "codex:construction", "reachable", db_path=codex_session)

    listing = await server.list_agents()

    assert [s["lane"] for s in listing["live_sessions"]] == ["codex:construction"]
    marked = {
        e["recipient"]: e.get("live")
        for e in listing["observed_recipients"]
        if ":" in e["recipient"]
    }
    assert marked == {"codex:long-gone": False, "codex:construction": True}
    assert "1 lane(s)" in listing["stale_lanes_note"]


@pytest.mark.anyio
async def test_list_agents_tells_an_unaddressable_session_how_to_register(
    codex_session,
):
    from hardline_mcp import server

    listing = await server.list_agents()
    assert listing["you"]["addressable"] is False
    assert "register_session" in listing["you"]["how_to_register"]

    await server.register_session(label="construction")
    listing = await server.list_agents()
    assert listing["you"]["addressable"] is True
    assert listing["you"]["held_lanes"] == ["construction"]
