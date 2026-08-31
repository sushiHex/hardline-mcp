"""Tests for the session registry — real temp-dir SQLite, real process probes.

The registry's whole claim is that liveness is DERIVED, not stored, so the
tests that matter here are the ones where a recorded process is gone or is no
longer itself. A test that only ever registers live sessions would pass against
an implementation that hardcoded ``live = True``.
"""

import os
import threading
import time
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


def test_prune_only_deletes_the_instance_it_probed(tmp_path, monkeypatch):
    """The prune must be a compare-and-delete, not a delete by pid.

    Between deciding a pid is dead and issuing the DELETE, the OS can hand that
    pid to a new process which registers itself. Deleting on pid alone would
    remove the live newcomer on the strength of a liveness decision made about
    its predecessor — silently unregistering a session that just arrived.
    """
    db = tmp_path / "mb.db"
    sessions.register(agent="codex", lane="codex:a", pid=_DEAD_PID, db_path=db)

    def probe_then_race(row):
        # The pid is reused and re-registered while the pruner holds its
        # (now stale) view of the row.
        with mailbox._connect(db) as conn:
            with conn:
                conn.execute(
                    "UPDATE sessions SET process_key = ? WHERE pid = ?",
                    ("a-brand-new-instance", _DEAD_PID),
                )
        return False

    monkeypatch.setattr(sessions, "_is_live", probe_then_race)
    sessions.live(db_path=db)

    with mailbox._connect(db) as conn:
        keys = [r["process_key"] for r in conn.execute("SELECT process_key FROM sessions")]
    assert keys == ["a-brand-new-instance"], (
        "the row now describes a different process instance and must survive"
    )


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


def test_process_key_actually_identifies_this_process(tmp_path):
    """A real round-trip, not just a mismatch.

    The reuse test above writes a deliberately wrong token, so it also passes
    when ``process_key`` always returns None — the comparison rejects the fake
    either way. Registering with the token the platform really produces is what
    proves the mechanism works rather than merely refusing everything.
    """
    key = procid.process_key(os.getpid())
    if key is None:
        pytest.skip("this platform does not expose a process creation token")
    assert procid.process_key(os.getpid()) == key, "stable across calls"
    assert procid.instance_alive(os.getpid(), key) is True

    db = tmp_path / "mb.db"
    sessions.register(agent="claude", lane="claude:me", db_path=db)
    with mailbox._connect(db) as conn:
        stored = conn.execute("SELECT process_key FROM sessions").fetchone()[0]
    assert stored == key, "the real token must be what gets recorded"
    assert [s["lane"] for s in sessions.live(db_path=db)] == ["claude:me"]


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

    # Against the RAW table, not through live()/holders(): both filter the dead
    # row out anyway, so either would report success whether the takeover
    # cleared it or merely ignored it - leaving two rows for one lane.
    with mailbox._connect(db) as conn:
        pids = [
            r["pid"]
            for r in conn.execute(
                "SELECT pid FROM sessions WHERE lane = ?", ("codex:construction",)
            )
        ]
    assert pids == [os.getpid()], "the dead holder's row must be cleared, not skipped"


def test_two_live_sessions_cannot_both_claim_one_name(tmp_path, monkeypatch):
    """The race the BEGIN IMMEDIATE in ``claim`` exists for.

    Two sessions claiming the same label at once each read "nobody holds it"
    and each insert their OWN pid row, which collides with nothing — pid is the
    primary key and lane is not unique. Both would then hold the lane and
    consume each other's mail. Exactly one must win.
    """
    import threading

    db = tmp_path / "mb.db"
    other = os.getppid()
    if other in (0, os.getpid()) or not procid.pid_alive(other):
        pytest.skip("no second live pid available to race with")

    results: list[dict] = []
    lock = threading.Lock()
    start = threading.Barrier(2)

    # Force the interleaving instead of hoping for it. A bare barrier is not
    # enough: the winner usually finishes the whole claim before the loser's
    # SELECT runs, so the race never happens and the test passes against an
    # implementation with no transaction at all (verified - it did). Holding
    # the first writer between its check and its write is what makes the
    # unguarded version fail every time.
    real_upsert = sessions._upsert
    held = threading.Event()

    def slow_upsert(*args, **kwargs):
        if not held.is_set():
            held.set()
            time.sleep(0.5)
        return real_upsert(*args, **kwargs)

    monkeypatch.setattr(sessions, "_upsert", slow_upsert)

    def claim_as(pid):
        start.wait(timeout=5)
        outcome = sessions.claim(
            agent="codex", label="construction", pid=pid, db_path=db
        )
        with lock:
            results.append(outcome)

    threads = [
        threading.Thread(target=claim_as, args=(os.getpid(),)),
        threading.Thread(target=claim_as, args=(other,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    won = [r for r in results if r.get("ok")]
    assert len(won) == 1, f"exactly one claim must win, got {results!r}"
    assert len(sessions.holders("codex:construction", db_path=db)) == 1


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


def test_a_renamed_sessions_old_lane_still_reports_a_holder(monkeypatch, tmp_path):
    """The registry must record every lane a session holds, not just its name.

    This is the defect that made renaming dangerous. The process consumes mail
    for the old lane, but if the registry only stored the current name, the old
    one showed no holder — so it read as dead, ``send`` warned nobody could
    receive it, and another session could CLAIM it. Both would then hold the
    lane and drain each other's mail, nondeterministically, which is the exact
    failure lanes exist to prevent.
    """
    from hardline_mcp import server

    db = tmp_path / "mb.db"
    monkeypatch.setattr(mailbox, "_DEFAULT_PATH", db)
    monkeypatch.setenv("HARDLINE_AGENT", "codex")

    assert sessions.claim(agent="codex", label="first", db_path=db)["ok"] is True
    adapters.claim_lane("first")
    server._announce_self()

    adapters.claim_lane("second")
    server._announce_self()

    assert sessions.holders("codex:first", db_path=db), (
        "the old lane is still consumed by this process, so it still has a holder"
    )
    assert sessions.holders("codex:second", db_path=db)

    entries = sessions.live(db_path=db)
    assert len(entries) == 1, "one session, however many names"
    assert entries[0]["lane"] == "codex:second", "addressed by the newest name"
    assert entries[0]["lanes"] == ["codex:first", "codex:second"]


def test_another_session_cannot_claim_a_lane_a_renamed_process_still_holds(tmp_path):
    """The consequence of the above, stated as the attack it prevents."""
    db = tmp_path / "mb.db"
    other = os.getppid()
    if other in (0, os.getpid()) or not procid.pid_alive(other):
        pytest.skip("no second live pid available")

    sessions.register(
        agent="codex",
        lanes=["codex:first", "codex:second"],
        pid=other,
        db_path=db,
    )
    stolen = sessions.claim(agent="codex", label="first", db_path=db)
    assert stolen["ok"] is False
    assert "already held" in stolen["error"]


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
        owned=adapters.owned_recipients("claude"),
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
        sent["message_id"], owned=adapters.owned_recipients("claude"), db_path=db
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
    mine = ("claude:mine", "claude:also-mine")
    assert mailbox.ack(sent["message_id"], owned=mine, db_path=db) == {"ok": False}

    msgs, remaining = mailbox.inbox(
        ["claude:someone-else"], owned=mine, db_path=db
    )
    assert [m["body"] for m in msgs] == ["not yours"], "shown"
    assert msgs[0]["acked_at"] is None, "but never consumed"
    assert remaining == 0, "and not counted as work this caller can drain"


def test_a_process_with_no_lane_still_owns_no_lane(tmp_path):
    """Both halves of the rule, because they are enforced separately.

    Checking only ``ack`` exercises the SQL clause and leaves ``inbox``'s
    Python predicate and remaining-count untested — so a version that let a
    laneless caller consume through a reading poll would still pass.
    """
    db = tmp_path / "mb.db"
    sent = mailbox.send("codex", "claude:somebody", "lane mail", db_path=db)
    assert mailbox.ack(sent["message_id"], owned=(), db_path=db) == {"ok": False}
    assert mailbox.ack(sent["message_id"], owned=None, db_path=db) == {"ok": False}

    msgs, remaining = mailbox.inbox(["claude:somebody"], owned=(), db_path=db)
    assert [m["body"] for m in msgs] == ["lane mail"], "shown"
    assert msgs[0]["acked_at"] is None, "but never consumed"
    assert remaining == 0


@pytest.mark.parametrize(
    "lane",
    [
        "a:b",  # HARDLINE_AGENT_LABEL is operator-set and NOT validated, so a
        # recipient can carry more than one ':'
        "o'brien",  # a quote, to prove the clause is parameterized not interpolated
        "100%",  # not a LIKE pattern
        "_underscore",
    ],
)
def test_the_sql_clause_and_the_python_predicate_agree(tmp_path, lane):
    """The two halves of one rule, checked against each other on awkward input.

    ``_lane_ackable`` splits on the FIRST ':' in Python while the SQL uses
    ``substr(recipient, instr(recipient, ':') + 1)``. They have to agree for
    every lane, or a consuming read and an explicit ack disagree about who owns
    a message - which is the drift that keeping one rule in two forms is meant
    to prevent.
    """
    db = tmp_path / "mb.db"
    recipient = f"claude:{lane}"
    assert mailbox._lane_ackable(recipient, (recipient,)) is True
    assert mailbox._lane_ackable(recipient, ("claude:other",)) is False

    mine = mailbox.send("codex", recipient, "mine", db_path=db)
    assert mailbox.ack(mine["message_id"], owned=(recipient,), db_path=db)["ok"] is True

    theirs = mailbox.send("codex", recipient, "theirs", db_path=db)
    assert (
        mailbox.ack(theirs["message_id"], owned=("claude:other",), db_path=db)["ok"]
        is False
    )


def test_lane_ackable_accepts_a_bare_string_and_a_collection():
    """The normalizer's shape promise: one recipient or several, never a suffix."""
    assert mailbox._lane_ackable("claude:a", "claude:a") is True
    assert mailbox._lane_ackable("claude:a", ("claude:b", "claude:a")) is True
    assert mailbox._lane_ackable("claude:a", "claude:b") is False
    assert mailbox._lane_ackable("claude:a", ("claude:b", "claude:c")) is False
    assert mailbox._lane_ackable("claude", ()) is True


def test_owning_a_suffix_does_not_reach_into_another_agents_mail():
    """The cross-agent hole that human labels made reachable.

    Ownership used to compare the SUFFIX, so a session called ``construction``
    owned ``codex:construction`` and ``claude:construction`` alike. That was
    nearly unreachable while lanes were derived from session ids, which never
    collide — but two agents both named ``construction`` is the obvious case
    the moment sessions can name themselves.
    """
    assert mailbox._lane_ackable("claude:construction", ("codex:construction",)) is False
    assert mailbox._lane_ackable("codex:construction", ("codex:construction",)) is True


def test_ownership_survives_a_one_shot_iterator(tmp_path):
    """``inbox`` consults ownership once per message and again for ``remaining``.

    A generator would be exhausted after the first consultation: later messages
    would silently stop being ackable while ``remaining`` read zero, which is
    the "poll while non-zero" loop terminating with mail still undelivered.
    """
    db = tmp_path / "mb.db"
    for i in range(3):
        mailbox.send("codex", "claude:mine", f"m{i}", db_path=db)

    msgs, remaining = mailbox.inbox(
        ["claude:mine"], owned=iter(["claude:mine"]), db_path=db
    )
    assert [m["body"] for m in msgs] == ["m0", "m1", "m2"]
    assert all(m["acked_at"] for m in msgs), "every message must be consumed, not just the first"
    assert remaining == 0


# ── self_agent inference ─────────────────────────────────────────────────────


def test_an_anonymous_process_owns_no_qualified_recipients(monkeypatch):
    """Fail closed when the process cannot say which agent it serves.

    It still has a lane suffix — HARDLINE_AGENT_LABEL is set — but nothing says
    whether that names a codex session or a claude one. Qualifying it with a
    guess would hand this process whichever agent's mailbox the guess picked.
    """
    monkeypatch.setenv("HARDLINE_AGENT_LABEL", "construction")
    assert adapters.self_agent() is None
    assert adapters.held_lanes() == ("construction",)
    assert adapters.owned_recipients() == ()

    # Naming the agent explicitly is what makes it ownable — that is what
    # register_session and HARDLINE_AGENT are for.
    assert adapters.owned_recipients("codex") == ("codex:construction",)


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
async def test_an_async_result_dispatched_before_a_rename_still_arrives(
    monkeypatch, tmp_path, in_session
):
    """The motivating scenario, end to end, with a real deferred worker.

    The previous test writes the old recipient by hand, which proves the
    mailbox rule but not the thing that makes it necessary. Here the worker is
    captured at dispatch and run only AFTER the rename, so the recipient really
    is fixed at dispatch time — an implementation that recomputed the lane at
    delivery would pass the hand-written test and fail this one.
    """
    from concurrent.futures import TimeoutError as FuturesTimeout

    from hardline_mcp import server

    db = tmp_path / "mb.db"
    monkeypatch.setattr(mailbox, "_DEFAULT_PATH", db)

    deferred = []

    class _NotYet:
        """A future whose work has not run: the dispatch reports it running."""

        def result(self, timeout=None):
            raise FuturesTimeout()

    def defer(fn, *args, **kwargs):
        deferred.append(lambda: fn(*args, **kwargs))
        return _NotYet()

    monkeypatch.setattr(server._async_executor, "submit", defer)
    monkeypatch.setattr(
        server.adapters, "ask_codex", lambda prompt, **k: {"ok": True, "reply": "done"}
    )

    dispatched = await server.ask_codex_async(prompt="go", from_agent="claude")
    assert dispatched["lane"] == f"claude:{in_session}"

    claimed = await server.register_session(label="construction")
    assert claimed["ok"] is True

    for run in deferred:  # the worker finally delivers, under the OLD name
        run()

    got = await server.inbox(agent="claude")
    assert len(got["messages"]) == 1, (
        "a result dispatched before the rename must still reach the session"
    )
    assert got["messages"][0]["acked_at"], "and be consumable, not merely visible"


@pytest.mark.anyio
async def test_an_explicitly_declared_agent_is_remembered_for_ownership(
    monkeypatch, tmp_path
):
    """Passing ``agent=`` must make the session able to CONSUME, not just register.

    This is the documented path for Codex and Hermes: they cannot be inferred,
    so they name themselves at the call. If only the label is remembered, every
    later ``inbox``/``ack`` re-derives identity from the environment, finds
    nothing, and owns no lane-qualified mail - so the session registers, is
    advertised as live, receives mail, and can never take it.

    The end-to-end test misses this because its fixture sets HARDLINE_AGENT,
    which is exactly the case where passing ``agent=`` is unnecessary.
    """
    from hardline_mcp import server

    db = tmp_path / "mb.db"
    monkeypatch.setattr(mailbox, "_DEFAULT_PATH", db)
    assert adapters.self_agent() is None, "nothing in the env identifies this session"

    ok = await server.register_session(label="construction", agent="codex")
    assert ok["ok"] is True

    mailbox.send("claude", "codex:construction", "ping", db_path=db)
    got = await server.inbox(agent="codex")
    assert [m["body"] for m in got["messages"]] == ["ping"]
    assert got["messages"][0]["acked_at"], "consumable, not merely visible"

    listing = await server.list_agents()
    assert listing["you"]["addressable"] is True


@pytest.mark.anyio
async def test_a_process_that_knows_what_it_is_cannot_redeclare_itself(
    monkeypatch, tmp_path, in_session
):
    """A declaration may fill in an unknown identity, never contradict a known one.

    Remembering the declaration is what makes ``agent=`` work for Codex and
    Hermes — and it is also what makes getting it wrong permanent. A Claude
    session that passes ``agent="codex"`` would otherwise become Codex for
    every subsequent ownership check, able to claim an unheld Codex lane and
    drain the backlog waiting there for a real Codex session.
    """
    from hardline_mcp import server

    monkeypatch.setattr(mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")
    assert adapters.self_agent() == "claude"

    result = await server.register_session(label="construction", agent="codex")
    assert result["ok"] is False
    assert "codex" in result["error"] and "claude" in result["error"]

    assert adapters.self_agent() == "claude", "a refused declaration must not stick"
    assert adapters.owned_recipients() == (f"claude:{in_session}",)

    # Declaring what it already is remains fine — that is not a contradiction.
    assert (await server.register_session(label="ok", agent="claude"))["ok"] is True


@pytest.mark.anyio
async def test_a_rolled_back_claim_leaves_the_previous_label_intact(
    monkeypatch, codex_session
):
    """Rolling back the new lane must not leave the surviving one renamed.

    ``claim`` rewrites every retained lane, and passed ``label=None`` for the
    ones it was keeping — so a claim that is then rolled back deletes the new
    row and leaves the old one stripped of the name it was claimed under.
    ``list_agents`` would go on advertising the session with ``label: null``
    while it still holds and answers to that name.
    """
    from hardline_mcp import server

    assert (await server.register_session(label="first"))["ok"] is True

    monkeypatch.setattr(adapters, "claim_refusal", lambda label: None)
    monkeypatch.setattr(adapters, "claim_lane", lambda label: "lost the last slot")
    assert (await server.register_session(label="second"))["ok"] is False

    entry = sessions.live(db_path=codex_session)[0]
    assert entry["lane"] == "codex:first"
    assert entry["label"] == "first", "the surviving claim keeps the name it was made under"


@pytest.mark.anyio
async def test_concurrent_claims_keep_the_registry_and_the_process_agreed(
    monkeypatch, codex_session
):
    """The registry's current name and the one this process answers to are one fact.

    There is an await boundary between the durable claim and the local
    adoption, so two concurrent calls can commit in one order and adopt in the
    other: the registry would advertise one name while async results routed to
    the other.

    Asserted as non-interleaving rather than by racing for the symptom: the
    divergence only appears on a scheduling order the test cannot force, so a
    test that looked for it would pass most runs and prove nothing.
    """
    import anyio

    from hardline_mcp import server

    real_claim = sessions.claim
    order: list[str] = []
    lock = threading.Lock()

    def tracking_claim(**kwargs):
        with lock:
            order.append(f"enter:{kwargs['label']}")
        time.sleep(0.2)
        out = real_claim(**kwargs)
        with lock:
            order.append(f"exit:{kwargs['label']}")
        return out

    monkeypatch.setattr(sessions, "claim", tracking_claim)

    async def claim(name):
        await server.register_session(label=name)

    async with anyio.create_task_group() as tg:
        tg.start_soon(claim, "alpha")
        tg.start_soon(claim, "beta")

    first = order[0].split(":", 1)[1]
    assert order[1] == f"exit:{first}", (
        f"claims interleaved ({order}); the durable claim and the local adoption "
        "must be one critical section, or the registry and the process can end "
        "up naming different lanes as current"
    )
    entry = sessions.live(db_path=codex_session)[0]
    assert entry["lane"] == f"codex:{adapters.lane_suffix()}"


@pytest.mark.anyio
async def test_list_agents_marks_every_held_lane_live(monkeypatch, tmp_path):
    """A renamed session's OLD lane is still live, and must be reported so.

    The registry records every held lane precisely so an older name still shows
    a holder. Collapsing each session to its current name in the reporting
    layer throws that away again: the previous lane is marked dead and counted
    as stale mail, while ``holders()`` says the live process still consumes it.
    """
    from hardline_mcp import server

    db = tmp_path / "mb.db"
    monkeypatch.setattr(mailbox, "_DEFAULT_PATH", db)
    monkeypatch.setenv("HARDLINE_AGENT", "codex")

    await server.register_session(label="first")
    mailbox.send("claude", "codex:first", "for the old name", db_path=db)
    await server.register_session(label="second")

    listing = await server.list_agents()
    marked = {
        e["recipient"]: e.get("live")
        for e in listing["observed_recipients"]
        if ":" in e["recipient"]
    }
    assert marked.get("codex:first") is True, (
        "the old name still has a live holder, so it is not a dead destination"
    )
    assert "stale_lanes_note" not in listing


@pytest.mark.anyio
async def test_the_local_cap_is_checked_before_anything_durable(
    monkeypatch, codex_session
):
    """A claim that will be refused locally must not be attempted durably.

    Tested by watching for the durable call rather than for its leftovers,
    because a rollback would clean those up — so asserting only on the final
    rows cannot tell "never written" from "written then removed", and neither
    mechanism would be individually proven.
    """
    from hardline_mcp import server

    monkeypatch.setattr(adapters, "MAX_CLAIMED_LANES", 1)
    assert (await server.register_session(label="first"))["ok"] is True

    attempts = []
    real_claim = sessions.claim
    monkeypatch.setattr(
        sessions,
        "claim",
        lambda **kw: (attempts.append(kw), real_claim(**kw))[1],
    )

    refused = await server.register_session(label="second")
    assert refused["ok"] is False
    assert attempts == [], "the cap is knowable locally; do not write first and ask after"
    assert adapters.held_lanes() == ("first",)


@pytest.mark.anyio
async def test_a_late_local_refusal_rolls_back_the_registry_row(
    monkeypatch, codex_session
):
    """The narrow race the pre-check cannot close.

    Two concurrent register_session calls can both pass the pre-check and then
    one loses the last slot. Left alone, that leaves a registry row for a name
    this process never adopted and does not read: senders are told it has a
    holder, and no other session can claim it.
    """
    from hardline_mcp import server

    monkeypatch.setattr(adapters, "claim_refusal", lambda label: None)
    monkeypatch.setattr(adapters, "claim_lane", lambda label: "lost the last slot")

    refused = await server.register_session(label="second")
    assert refused["ok"] is False
    assert sessions.holders("codex:second", db_path=codex_session) == [], (
        "a name this process never adopted must not appear held"
    )


def test_two_claims_in_the_same_second_keep_their_order(tmp_path):
    """Which name is CURRENT cannot depend on alphabetical order.

    Stored timestamps have second precision, so two claims inside one second
    are indistinguishable by time; falling back to the lane text would make a
    rapid rename advertise whichever name sorts last. Here the second claim
    sorts EARLIER alphabetically, so a text tie-break reports the old name.
    """
    db = tmp_path / "mb.db"
    now_fn = _clock(_T0)  # frozen: both claims share a timestamp
    assert sessions.claim(agent="codex", label="zebra", db_path=db, now_fn=now_fn)["ok"]
    assert sessions.claim(
        agent="codex", label="alpha", lanes=["codex:zebra"], db_path=db, now_fn=now_fn
    )["ok"]

    entry = sessions.live(db_path=db)[0]
    assert entry["lanes"] == ["codex:zebra", "codex:alpha"], "claim order, not sort order"
    assert entry["lane"] == "codex:alpha", "the most recent claim is the current name"


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
    # ...and it actually took effect. Asserting only `ok` passes against an
    # implementation that returns success without registering or adopting.
    assert ok["lane"] == "codex:construction"
    assert adapters.lane_suffix() == "construction"
    assert sessions.holders("codex:construction", db_path=tmp_path / "mb.db")


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
    # Assert the warning too. Without this the test passes against an
    # implementation that warns for no lane at all, or only for some agents.
    assert "no live session holds" in result["warning"]
    assert "claude:gone.deadbeef" in result["warning"]
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
async def test_registration_recovers_if_the_row_goes_missing(codex_session):
    """Registration must be self-healing, not once-per-process.

    A row can vanish while its session is very much alive: a liveness probe
    that fails for an unrelated reason (an unopenable process), a store rebuilt
    underneath the running fleet, an operator clearing the table. If the
    process caches "I already registered" and never writes again, that session
    is invisible for the rest of its life — unaddressable, and reported to
    senders as a lane nobody holds.
    """
    from hardline_mcp import server

    await server.register_session(label="construction")
    sessions.unregister(db_path=codex_session)

    listing = await server.list_agents()
    assert [s["lane"] for s in listing["live_sessions"]] == ["codex:construction"]


def test_register_is_idempotent_under_concurrency(tmp_path):
    """Two threads registering the same pid must not raise.

    UPDATE-then-INSERT *looks* like an unguarded check-then-act - both threads
    find no row, both INSERT, and the loser hits the pid PRIMARY KEY. It is
    safe, and the reason is worth pinning: the UPDATE takes SQLite's single
    write lock even when it matches nothing, so the second thread blocks until
    the first commits and then sees the row its UPDATE was looking for.

    Pinned as a test because the safety comes from a lock this code never
    mentions. Anyone splitting these two statements across connections, or
    switching the UPDATE to a SELECT, removes it without touching a line that
    looks load-bearing — which is exactly what ``claim`` does one level up.
    """
    import threading

    db = tmp_path / "mb.db"
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def register_once():
        try:
            start.wait(timeout=5)
            sessions.register(agent="codex", lane="codex:c", db_path=db)
        except BaseException as exc:  # noqa: BLE001 - the point is to catch it
            errors.append(exc)

    threads = [threading.Thread(target=register_once) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent registration raised: {errors!r}"
    assert len(sessions.live(db_path=db)) == 1


@pytest.mark.anyio
async def test_list_agents_tells_an_unaddressable_session_how_to_register(
    codex_session,
):
    from hardline_mcp import server

    listing = await server.list_agents()
    assert listing["you"]["addressable"] is False
    assert "register_session" in listing["you"]["how_to_register"]
    # And it must not be IN the registry either. `addressable` is derived from
    # process-local state, so checking it alone passes even when every bare
    # session registers — which is how a one-shot `ask_codex` subprocess ends
    # up listed as a live session somebody could try to address.
    assert listing["live_sessions"] == [], (
        "a session with no lane is not a destination and must not be listed"
    )

    await server.register_session(label="construction")
    listing = await server.list_agents()
    assert listing["you"]["addressable"] is True
    assert listing["you"]["held_lanes"] == ["construction"]
    # Against the REGISTRY, not just process-local state. `addressable` is
    # computed from the same in-process anchor as `held_lanes`, so checking
    # only those two passes even when nothing was ever recorded and no other
    # session could actually reach this one.
    assert "codex:construction" in {s["lane"] for s in listing["live_sessions"]}
