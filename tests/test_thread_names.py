"""A Codex thread's claimed names come back to its next server (bindings).

docs/session-continuity.md, "A Codex thread's names". The previous server is
played by a dead (pid, key) written into ``thread_names``; this process is the
successor. A live foreign process is a real child.
"""

import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from types import SimpleNamespace

import pytest

from hardline_mcp import adapters, bindings, codex_queue, jobs, mailbox, procid, server, sessions
from test_codex_queue import OTHER, THREAD, TURN, meta

GONE = 0x7FFFFFFE  # well-formed and reliably absent: a dead previous server


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def store(monkeypatch, tmp_path):
    db = tmp_path / "mb.db"
    monkeypatch.setenv("HARDLINE_DB", str(db))
    monkeypatch.setenv("HARDLINE_AGENT", "codex")
    mailbox.send("x", "nobody", "creates the store", db_path=db)
    return db


@pytest.fixture
def wake(store):
    """This server's Codex connection, pinned to THREAD."""
    w = codex_queue.CodexWake(queue=lambda thread, text: {"ok": True})
    w.observe({"_meta": meta()})
    assert w.pinned() == THREAD
    server._codex_wake[:] = [w]
    return w


@pytest.fixture
def live():
    """A live foreign process: (pid, key)."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    yield proc.pid, procid.process_key(proc.pid)
    proc.kill()
    proc.wait()


def ctx(thread=THREAD, source="user"):
    """A tool Context whose request carries Codex's ``_meta``."""
    data = meta(thread=thread, source=source)
    return SimpleNamespace(
        request_context=SimpleNamespace(meta=SimpleNamespace(model_dump=lambda **_: data))
    )


def bind(db, lane, seq, *, pid=GONE, key="gone", thread=THREAD):
    with closing(sqlite3.connect(db)) as raw, raw:
        bindings.ensure(raw)
        raw.execute(
            "INSERT OR REPLACE INTO thread_names (lane, thread, seq, pid, process_key, bound_at)"
            " VALUES (?, ?, ?, ?, ?, 't')",
            (lane, thread, seq, pid, key),
        )


def rows(db) -> dict:
    with closing(sqlite3.connect(db)) as raw:
        try:
            got = raw.execute("SELECT lane, thread, seq, pid, process_key FROM thread_names")
        except sqlite3.OperationalError:
            return {}
        return {r[0]: r[1:] for r in got}


def me():
    return procid.current_identity()


# ── binding on claim ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_a_claim_from_the_pinned_thread_is_bound_to_it(wake, store):
    got = await server.register_session(label="role", ctx=ctx())
    assert got.get("bound_to_thread") is True, got
    assert rows(store).get("codex:role", ())[:2] == (THREAD, 1), "bound, at its place"
    assert rows(store)["codex:role"][2:] == me()


@pytest.mark.anyio
@pytest.mark.parametrize("request_ctx", [ctx(source="subagent"), ctx(thread=OTHER), None])
async def test_a_claim_not_from_the_pinned_thread_unbinds_the_name(wake, store, request_ctx):
    bind(store, "codex:role", 1)
    got = await server.register_session(label="role", ctx=request_ctx)
    assert got.get("ok") is True and got.get("bound_to_thread") is False, got
    assert got.get("binding_note")
    assert "codex:role" not in rows(store)


@pytest.mark.anyio
async def test_a_claim_for_another_agent_is_never_bound(wake, store, monkeypatch):
    monkeypatch.delenv("HARDLINE_AGENT")
    got = await server.register_session(label="role", agent="claude", ctx=ctx())
    assert got.get("bound_to_thread") is False, got
    assert not any(lane.startswith("claude:") for lane in rows(store))


def test_an_automatic_grant_leaves_a_binding_alone(wake, store, monkeypatch):
    """Another thread's name re-acquired at startup keeps that thread's claim."""
    bind(store, "codex:pinned", 3, thread=OTHER)
    monkeypatch.setenv("HARDLINE_AGENT_LABEL", "pinned")
    server._announce_self("codex")
    assert rows(store).get("codex:pinned") == (OTHER, 3, GONE, "gone")


def test_bindings_survive_their_process_leaving_the_registry(store):
    bind(store, "codex:role", 1, pid=me()[0], key=me()[1])
    sessions.unregister(db_path=store)
    sessions.live(db_path=store)  # prunes the dead
    assert "codex:role" in rows(store)


# ── release and dismissal ────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_a_release_withdraws_the_pinned_threads_binding_whoever_wrote_it(wake, store):
    """The name was re-bound to this thread by another server; released here,
    it must not come back."""
    assert (await server.register_session(label="role", ctx=ctx()))["ok"]
    bind(store, "codex:role", 5)  # Q bound it to the same thread and exited
    assert (await server.release_session(label="role"))["ok"]
    assert "codex:role" not in rows(store)


def test_a_release_leaves_another_threads_binding_by_another_writer(store):
    sessions.register(agent="codex", lane="codex:role", db_path=store)
    bind(store, "codex:role", 1, thread=OTHER)
    sessions.drop_lane("codex:role", thread=THREAD, db_path=store)
    assert rows(store).get("codex:role") == (OTHER, 1, GONE, "gone")


@pytest.mark.anyio
async def test_a_release_that_fails_durably_keeps_the_name(wake, store, monkeypatch):
    assert (await server.register_session(label="role", ctx=ctx()))["ok"]

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(sessions, "drop_lane", broken)
    with pytest.raises(Exception):
        await server.release_session(label="role")
    assert "role" in adapters.claimed_lanes(), "the name went locally but not durably"


@pytest.mark.anyio
async def test_a_dismissal_of_an_unheld_name_unbinds_it_and_says_so(wake, store):
    bind(store, "codex:old", 1)
    got = await server.release_session(label="old")
    assert got.get("ok") is True and got.get("unbound") == "codex:old", got
    assert "forgotten" not in got
    assert "codex:old" not in rows(store)


@pytest.mark.anyio
async def test_cancelling_a_waiting_claim_withdraws_it_from_the_thread(wake, store, live):
    sessions.register(agent="codex", lane="codex:role", pid=live[0], db_path=store)
    bind(store, "codex:role", 1)
    assert adapters.await_lane("role", "codex", THREAD) is None
    got = await server.release_session(label="role")
    assert got.get("cancelled") == "codex:role" and got.get("unbound") == "codex:role", got
    assert "codex:role" not in rows(store)


@pytest.mark.anyio
async def test_a_failed_derived_release_keeps_the_claim(wake, store, monkeypatch):
    monkeypatch.setenv("HARDLINE_AGENT_LABEL", "pinned")
    assert (await server.register_session(label="pinned", ctx=ctx()))["ok"]

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(sessions, "release_derived", broken)
    with pytest.raises(Exception):
        await server.release_session(label="pinned")
    assert adapters.claimed_lanes() == ("pinned",)


@pytest.mark.anyio
async def test_releasing_a_claimed_derived_name_keeps_the_lane_at_its_place(
    wake, store, monkeypatch
):
    monkeypatch.setenv("HARDLINE_AGENT_LABEL", "pinned")
    for label in ("x", "pinned"):
        assert (await server.register_session(label=label, ctx=ctx()))["ok"]
    assert "codex:pinned" in rows(store)
    got = await server.release_session(label="pinned")
    assert got.get("ok") is True, got
    assert "codex:pinned" not in rows(store)
    assert adapters.claimed_lanes() == ("x",)
    entry = sessions.live(db_path=store)[0]
    assert entry["lanes"] == ["codex:pinned", "codex:x"] and entry["lane"] == "codex:x"


# ── restoration ──────────────────────────────────────────────────────────────


def test_the_threads_names_come_back_in_their_order(wake, store):
    bind(store, "codex:a", 1)
    bind(store, "codex:b", 2)
    server._restore_pending()
    assert adapters.claimed_lanes() == ("a", "b")
    assert adapters.lane_suffix() == "b"
    assert {lane: r[2:] for lane, r in rows(store).items()} == {
        "codex:a": me(), "codex:b": me(),
    }
    entry = sessions.live(db_path=store)[0]
    assert entry["lane"] == "codex:b"
    assert sorted(server._restoration_report()["restored"]) == ["codex:a", "codex:b"]


def test_another_threads_names_are_not_restored(wake, store):
    bind(store, "codex:a", 1, thread=OTHER)
    server._restore_pending()
    assert adapters.claimed_lanes() == ()


def test_a_name_whose_writer_lives_is_not_restored(wake, store, live):
    bind(store, "codex:a", 1, pid=live[0], key=live[1])
    server._restore_pending()
    assert adapters.claimed_lanes() == ()
    assert server._restoration["outcomes"]["codex:a"][0] == "blocked"


def test_a_name_held_by_a_live_process_is_not_restored(wake, store, live):
    bind(store, "codex:a", 1)
    sessions.register(agent="codex", lane="codex:a", pid=live[0], db_path=store)
    server._restore_pending()
    assert adapters.claimed_lanes() == ()
    assert rows(store)["codex:a"][2:] == (GONE, "gone"), "writer untouched"


def _candidate(lane, seq, pid=GONE, key="gone"):
    return {"lane": lane, "thread": THREAD, "seq": seq, "pid": pid, "process_key": key,
            "kind": "acquire", "new": True}


def _restore(candidates, store, free=8):
    return sessions.restore(
        THREAD, candidates, agent="codex", free=free,
        plan=lambda new, order: list(new), attribution=lambda t: t, db_path=store,
    )


def test_the_write_phase_rechecks_the_writer_itself(store, live):
    """Not only the survey: a writer that is alive at write time refuses."""
    bind(store, "codex:a", 1, pid=live[0], key=live[1])
    got = _restore([_candidate("codex:a", 1, *live)], store)
    assert got["accepted"] == [] and got["outcomes"]["codex:a"][0] == "blocked"


def test_the_write_phase_rechecks_the_ownership_rule(store, live):
    bind(store, "codex:a", 1)
    sessions.register(agent="codex", lane="codex:a", pid=live[0], db_path=store)
    got = _restore([_candidate("codex:a", 1)], store)
    assert got["accepted"] == [] and got["outcomes"]["codex:a"][0] == "blocked"


def test_a_stopped_wake_restores_nothing(wake, store):
    bind(store, "codex:a", 1)
    wake.observe({"_meta": meta(thread=OTHER)})  # a second top-level thread
    server._restore_pending()
    assert adapters.claimed_lanes() == ()
    assert server._restoration["policy"].startswith("stopped")


def test_a_stop_between_survey_and_write_commits_nothing(wake, store, monkeypatch):
    bind(store, "codex:a", 1)
    survey = sessions.survey

    def then_stop(*a, **k):
        found = survey(*a, **k)
        wake.stopped = True
        return found

    monkeypatch.setattr(sessions, "survey", then_stop)
    server._restore_pending()
    assert adapters.claimed_lanes() == ()
    assert rows(store)["codex:a"][2:] == (GONE, "gone")
    assert not sessions.holders("codex:a", db_path=store)


def test_a_changed_candidate_is_skipped_and_the_next_one_restored(wake, store, monkeypatch):
    """One slot: A changes before the write, so B gets it."""
    for i in range(adapters.MAX_CLAIMED_LANES - 1):
        adapters.claim_lane(f"filler{i}")
    bind(store, "codex:a", 2)
    bind(store, "codex:b", 1)
    survey = sessions.survey

    def then_change(*a, **k):
        found = survey(*a, **k)
        # Another dead server re-bound it meanwhile: same place, new writer,
        # which only the five-field re-read can see.
        with closing(sqlite3.connect(store)) as raw, raw:
            raw.execute(f"UPDATE thread_names SET pid = {GONE - 1} WHERE lane = 'codex:a'")
        return found

    monkeypatch.setattr(sessions, "survey", then_change)
    server._restore_pending()
    assert "b" in adapters.claimed_lanes() and "a" not in adapters.claimed_lanes()
    assert server._restoration["outcomes"]["codex:a"][0] == "changed"
    assert not sessions.holders("codex:a", db_path=store), "nothing of A was written"


def test_a_full_session_still_adopts_a_name_it_already_holds(wake, store):
    """A writer transfer adds no entry, so it costs no slot."""
    for i in range(adapters.MAX_CLAIMED_LANES - 1):
        adapters.claim_lane(f"filler{i}")
    adapters.claim_lane("a")
    bind(store, "codex:a", 1)
    bind(store, "codex:b", 2)
    server._restore_pending()
    assert rows(store)["codex:a"][2:] == me()
    assert "b" not in adapters.claimed_lanes()
    assert server._restoration["outcomes"]["codex:b"][0] == "cap"


def test_a_held_but_contested_name_is_not_adopted(wake, store, monkeypatch, live):
    """Held here is memory, not a grant: a live holder in the registry blocks."""
    monkeypatch.setenv("HARDLINE_AGENT_LABEL", "d")
    bind(store, "codex:d", 1)
    sessions.register(agent="codex", lane="codex:d", pid=live[0], db_path=store)
    server._restore_pending()
    assert adapters.claimed_lanes() == ()
    assert server._restoration["outcomes"]["codex:d"][0] == "blocked"
    found = sessions.survey(
        THREAD, "codex:", before=None, limit=8, listed=(), held=("codex:d",), pending=(),
        db_path=store,
    )
    assert found[0]["outcome"] == "blocked", "the survey adopted a contested lane"
    adopt = {**_candidate("codex:d", 1), "kind": "adopt"}
    assert _restore([adopt], store)["outcomes"]["codex:d"][0] == "blocked"


def test_adopting_a_held_name_takes_its_registry_grant_too(wake, store, monkeypatch):
    """Held here, with no registry row: restored means granted, or the wake
    (which reads granted lanes) would never announce its mail."""
    monkeypatch.setenv("HARDLINE_AGENT_LABEL", "d")
    bind(store, "codex:d", 1)
    server._restore_pending()
    assert adapters.claimed_lanes() == ("d",)
    assert [h["pid"] for h in sessions.holders("codex:d", db_path=store)] == [me()[0]]


def test_this_processs_unlisted_derived_binding_is_adopted(wake, store, monkeypatch):
    """Its own binding of its derived name, never adopted into the list (a
    failure after the commit): the next attempt adopts it."""
    monkeypatch.setenv("HARDLINE_AGENT_LABEL", "d")
    server._announce_self("codex")
    bind(store, "codex:d", 1, pid=me()[0], key=me()[1])
    server._restore_pending()
    assert adapters.claimed_lanes() == ("d",)


@pytest.mark.anyio
async def test_the_order_restored_is_the_order_the_next_server_restores(wake, store):
    """E claimed here, R restored below it: renumbered so the next server
    answers to E too, not to R's older, higher place."""
    assert (await server.register_session(label="e", ctx=ctx()))["ok"]
    bind(store, "codex:r", 5)
    server._restore_pending()
    assert adapters.claimed_lanes() == ("r", "e") and adapters.lane_suffix() == "e"
    _restart(store)
    server._restore_pending()
    assert adapters.claimed_lanes() == ("r", "e") and adapters.lane_suffix() == "e"


def test_a_candidate_refused_after_its_writes_is_rolled_back_alone(wake, store):
    """B's binding update is refused after B's registry row was written: B's
    savepoint undoes that row, and A, accepted before it, stays."""
    bind(store, "codex:a", 2)
    bind(store, "codex:b", 1)
    with closing(sqlite3.connect(store)) as raw, raw:
        raw.execute(
            "CREATE TRIGGER refuse_b BEFORE UPDATE ON thread_names WHEN OLD.lane = 'codex:b'"
            " BEGIN SELECT RAISE(IGNORE); END"
        )
    server._restore_pending()
    assert adapters.claimed_lanes() == ("a",)
    assert server._restoration["outcomes"]["codex:b"][0] == "changed"
    assert not sessions.holders("codex:b", db_path=store), "B's registry row survived"
    assert sessions.holders("codex:a", db_path=store)


def test_a_dispatch_racing_a_claim_never_goes_to_the_lane_the_claim_replaces(
    wake, store, monkeypatch
):
    """The claim is durable but not yet published when the dispatch chooses
    its recipient: the dispatch is refused, not sent to the lane being left."""
    import threading

    durable, go = threading.Event(), threading.Event()
    claim = sessions.claim

    def paused(**kw):
        got = claim(**kw)
        durable.set()
        assert go.wait(30)
        return got

    monkeypatch.setattr(sessions, "claim", paused)
    claimer = threading.Thread(
        target=server._register_session_impl, args=("r", None, False, THREAD)
    )
    claimer.start()
    try:
        assert durable.wait(30)
        raced = _admit()
    finally:
        go.set()
        claimer.join(30)
    assert raced.get("accepted") is False and raced.get("retryable") is True, raced
    assert [n["lane"] for n in raced.get("unheld_names", [])] == ["codex:r"]
    after = _admit()
    assert after.get("accepted") is True and after.get("lane") == "codex:r", after


class _Unreadable:
    """A prepared origin set that fails when published: a fault part way. Not
    a set subclass: frozenset() copies a set's table without iterating it."""

    def __init__(self, items):
        self.items = set(items)

    def __iter__(self):
        raise RuntimeError("injected publication fault")


def test_a_publication_that_fails_part_way_is_repaired_in_order(wake, store, monkeypatch):
    """A fault while publishing must leave the name repairable, not listed
    without its origin: then a newer restored name lands below it, not above."""
    prepare = adapters.prepare_restoration
    faults = [True]

    def faulty(labels, order):
        prepared = prepare(labels, order)
        if faults and faults.pop():
            prepared = prepared._replace(restored=_Unreadable(prepared.restored))
        return prepared

    monkeypatch.setattr(adapters, "prepare_restoration", faulty)
    bind(store, "codex:a", 1)
    server._restore_pending()
    assert server._restoration["failure"], "the fault was not reported"
    server._restore_pending()  # repaired
    assert adapters.claimed_lanes() == ("a",)
    bind(store, "codex:b", 5)
    server._restore_pending()
    assert adapters.claimed_lanes() == ("a", "b"), "a was read as an explicit claim"


def test_an_explicit_claim_clears_an_origin_a_failed_publication_left(wake, store):
    """Origins published, then the list replacement failed: the name is then
    claimed explicitly before any repair. It is explicit, so a newer restored
    name lands below it."""
    adapters._restored = frozenset({"a"})  # the state such a fault leaves
    assert adapters.claim_lane("a") is None
    bind(store, "codex:b", 5)
    server._restore_pending()
    assert adapters.claimed_lanes() == ("b", "a")


class _FaultyOrigins(frozenset):
    """Origins whose first cleanup fails: a fault inside ``claim_lane``."""

    faults = [True]

    def __sub__(self, other):
        if self.faults and self.faults.pop():
            raise MemoryError("injected origin-cleanup fault")
        return frozenset(self) - other


def test_a_claim_whose_origin_cleanup_fails_is_left_unclaimed_and_retried(wake, store):
    """Two faults: a publication left A's origin behind, then clearing it during
    an explicit claim of A failed. A must not be listed with that origin: the
    claim fails whole, a retry claims it explicitly, and B lands below it."""
    _FaultyOrigins.faults = [True]
    adapters._restored = _FaultyOrigins({"a"})
    with pytest.raises(MemoryError):
        adapters.claim_lane("a")
    assert adapters.claimed_lanes() == (), "listed with the stale origin"
    assert adapters.claim_lane("a") is None
    bind(store, "codex:b", 5)
    server._restore_pending()
    assert adapters.claimed_lanes() == ("b", "a")


def test_a_dead_launching_host_restores_nothing(wake, store, monkeypatch):
    bind(store, "codex:a", 1)
    monkeypatch.setattr(adapters, "host_identity", lambda: {"host_pid": GONE, "host_key": "gone"})
    server._restore_pending()
    assert adapters.claimed_lanes() == ()
    assert server._restoration["policy"] == "launching host exited"


def test_a_store_without_the_table_says_so(wake, store):
    with closing(sqlite3.connect(store)) as raw, raw:
        raw.execute("DROP TABLE IF EXISTS thread_names")
    server._restore_pending()
    assert server._restoration["policy"] == "no binding table"


class _WatchedLock:
    """``server._identity_lock``, recording how the claimer got it: ``blocked``
    (held by someone else, so it waits) or ``acquired`` (free at once)."""

    def __init__(self, inner, record):
        self.inner, self.record = inner, record

    def acquire(self, *a, **k):
        if threading.current_thread().name != "claimer":
            return self.inner.acquire(*a, **k)
        if self.inner.acquire(blocking=False):
            self.record("acquired")
            return True
        self.record("blocked")
        return self.inner.acquire(*a, **k)

    def release(self):
        self.inner.release()

    def locked(self):
        return self.inner.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


@pytest.mark.parametrize("path", ["explicit", "fulfilled"])
def test_a_claim_cannot_publish_between_a_dispatchs_recipient_and_its_job(
    wake, store, monkeypatch, path
):
    """The dispatch holds the identity lock from its recipient to its job. A
    claim reaching publication meanwhile is BLOCKED on that lock - not let
    through, not publishing without it - whatever the schedule: the dispatch
    waits for the claimer's first decisive step, which is one of those three."""
    in_job, decisive = threading.Event(), threading.Event()
    claimer_steps, events = [], []

    def record(step):
        claimer_steps.append(step)
        decisive.set()

    monkeypatch.setattr(server, "_identity_lock", _WatchedLock(server._identity_lock, record))
    create, claim_lane = jobs.create, adapters.claim_lane

    def creating(**kw):
        in_job.set()
        assert decisive.wait(30), "the claimer never reached the lock or publication"
        events.append(("job", kw["requester"]))
        return create(**kw)

    def claiming(label):
        record("published")
        events.append(("claim", label))
        return claim_lane(label)

    monkeypatch.setattr(jobs, "create", creating)
    monkeypatch.setattr(adapters, "claim_lane", claiming)
    dispatched = {}
    dispatcher = threading.Thread(target=lambda: dispatched.update(_admit()))
    dispatcher.start()
    assert in_job.wait(30)
    # Unattributed, so no binding: the dispatch is accepted and reaches its job.
    if path == "explicit":
        claimer = threading.Thread(
            target=server._register_session_impl, args=("r", None, False, None), name="claimer"
        )
    else:
        assert adapters.await_lane("r", "codex", None) is None
        claimer = threading.Thread(target=server._fulfil_pending, name="claimer")
    claimer.start()
    dispatcher.join(30)
    claimer.join(30)
    assert claimer_steps[:1] == ["blocked"], claimer_steps
    assert dispatched.get("accepted") is True, dispatched
    assert events == [("job", dispatched["lane"]), ("claim", "r")], events
    assert dispatched["lane"] != "codex:r"


def test_a_restoration_whose_local_half_was_lost_is_repaired(wake, store):
    """Committed, then the process forgot it (failure after commit): the next
    attempt finds this process's own row unheld and claims it again."""
    bind(store, "codex:a", 1)
    server._restore_pending()
    adapters.reset_claimed_lanes()
    server._restoration.update(outcomes={}, cursor=None, seen=set())
    server._restore_pending()
    assert adapters.claimed_lanes() == ("a",)


def test_more_than_a_page_of_names_comes_back_over_two_attempts(wake, store):
    for seq in range(1, server._RESTORE_PAGE + 2):
        bind(store, f"codex:n{seq}", seq)
    server._restore_pending()
    assert len(adapters.claimed_lanes()) == server._RESTORE_PAGE
    server._restore_pending()
    assert len(adapters.claimed_lanes()) == server._RESTORE_PAGE + 1
    assert adapters.lane_suffix() == f"n{server._RESTORE_PAGE + 1}", "newest is the address"


def test_restoration_declares_codex_when_nothing_else_did(wake, store, monkeypatch):
    """A Codex server not told its agent learns it from what it restored, so
    the names it holds are ones it reads."""
    monkeypatch.delenv("HARDLINE_AGENT")
    assert adapters.self_agent() is None
    bind(store, "codex:a", 1)
    server._restore_pending()
    assert adapters.self_agent() == "codex"
    assert "codex:a" in adapters.owned_recipients()


@pytest.mark.anyio
async def test_a_claim_is_placed_above_another_writers_names_on_the_thread(wake, store):
    bind(store, "codex:old", 2)
    assert (await server.register_session(label="role", ctx=ctx()))["ok"]
    assert rows(store)["codex:role"][1] == 3


def test_a_failing_stage_does_not_starve_restoration(wake, store, monkeypatch):
    bind(store, "codex:a", 1)

    def broken():
        raise RuntimeError("pending claims broke")

    monkeypatch.setattr(server, "_fulfil_pending", broken)
    try:
        server._regain_lanes()
    except RuntimeError:
        pass  # judged by what was restored, below
    assert adapters.claimed_lanes() == ("a",), "the failing stage starved restoration"


def test_the_first_poll_after_the_pin_restores(store):
    w = codex_queue.CodexWake(queue=lambda thread, text: {"ok": True})
    w._last_fulfil = object()  # as if fulfil had just run
    w.observe({"_meta": meta()})
    assert w._last_fulfil is None


def test_restoration_runs_on_no_tool_path():
    """Tools read what restoration published; only the wake's poll restores."""
    import inspect

    for tool in ("inbox", "list_agents", "register_session", "_admit_async", "_register_locked"):
        assert "_restore_pending" not in inspect.getsource(getattr(server, tool)), tool


# ── async admission ──────────────────────────────────────────────────────────


def _admit(from_agent="codex"):
    return server._admit_async(
        "codex", lambda prompt, **kw: {"ok": True, "reply": "r"}, "do it", from_agent,
        label=None, model=None, effort="default", mode="default", workdir=None, write=False,
    )


def test_a_dispatch_is_refused_while_the_threads_names_are_unheld(wake, store, live):
    bind(store, "codex:a", 1, pid=live[0], key=live[1])
    for _ in range(2):  # refused every time, and the slot is given back
        got = _admit()
        assert got.get("retryable") is True and got.get("accepted") is False, got
        assert [n["lane"] for n in got.get("unheld_names", [])] == ["codex:a"]
    with closing(sqlite3.connect(store)) as raw:
        assert raw.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0, "a job was created"


def test_a_thread_without_names_dispatches_with_a_note(wake, store):
    got = _admit()
    assert got.get("accepted") is True, got
    assert "continuity_note" in got


@pytest.mark.anyio
async def test_a_held_claim_dispatches_despite_an_unheld_name(wake, store, live):
    bind(store, "codex:a", 1, pid=live[0], key=live[1])
    assert (await server.register_session(label="mine", ctx=ctx()))["ok"]
    got = _admit()
    assert got.get("accepted") is True and got.get("lane") == "codex:mine", got
    assert "continuity_note" not in got


def test_an_unpinned_server_never_asks(store, monkeypatch):
    def unread(*a, **k):
        raise AssertionError("read the bindings without a pinned thread")

    monkeypatch.setattr(bindings, "unheld", unread)
    assert _admit().get("accepted") is True


def test_a_from_agent_that_is_not_this_session_is_refused(wake, store):
    got = _admit(from_agent="claude")
    assert got.get("accepted") is False and "not this session's identity" in got.get("error", "")


@pytest.mark.anyio
async def test_claims_and_jobs_publish_under_the_identity_lock(wake, store, monkeypatch):
    """A dispatch sees a claim whole or not at all."""
    claim_lane, create = adapters.claim_lane, jobs.create
    seen = []

    def claiming(label):
        seen.append(("claim", server._identity_lock.locked()))
        return claim_lane(label)

    def creating(**kw):
        seen.append(("job", server._identity_lock.locked()))
        return create(**kw)

    result_lane = server._result_lane

    def choosing(agent):
        seen.append(("recipient", server._identity_lock.locked()))
        return result_lane(agent)

    monkeypatch.setattr(adapters, "claim_lane", claiming)
    monkeypatch.setattr(jobs, "create", creating)
    monkeypatch.setattr(server, "_result_lane", choosing)
    assert (await server.register_session(label="mine", ctx=ctx()))["ok"]
    assert _admit().get("accepted") is True
    assert seen == [("claim", True), ("recipient", True), ("job", True)], seen


def test_a_fulfilled_claim_is_bound_as_its_request_was(wake, store, monkeypatch):
    """A waiting claim carries its request's thread to the moment it is granted."""
    claim_lane = adapters.claim_lane
    locked = []
    monkeypatch.setattr(
        adapters, "claim_lane",
        lambda label: locked.append(server._identity_lock.locked()) or claim_lane(label),
    )
    assert adapters.await_lane("role", "codex", THREAD) is None
    assert server._fulfil_pending() == ["codex:role"]
    assert rows(store).get("codex:role", ())[:1] == (THREAD,)
    assert locked == [True]


def _restart(store):
    """This server exits and the thread's next one starts: the binding's
    writer is dead, nothing of this process is registered or remembered."""
    with closing(sqlite3.connect(store)) as raw, raw:
        raw.execute(f"UPDATE thread_names SET pid = {GONE}, process_key = 'gone'")
    sessions.unregister(db_path=store)
    adapters.reset_claimed_lanes()
    server._restoration.update(outcomes={}, cursor=None, seen=set(), policy="no thread")


@pytest.mark.anyio
async def test_a_result_dispatched_after_a_restart_is_read_after_the_next(wake, store):
    """The acceptance test: P claims R and exits; S1 restores R, dispatches
    without claiming, the job completes; S1 exits; S2 restores R and reads the
    completion at R."""
    import time

    bind(store, "codex:r", 1)  # P claimed R on this thread, then exited
    server._restore_pending()  # S1
    assert adapters.lane_suffix() == "r"
    receipt = _admit()
    assert receipt.get("accepted") is True and receipt.get("lane") == "codex:r", receipt
    deadline = time.monotonic() + 30
    while (jobs.get(receipt["job_id"], db_path=store) or {}).get("state") != jobs.COMPLETED:
        assert time.monotonic() < deadline, "the job never finished"
        time.sleep(0.05)
    _restart(store)  # S1 exits; S2 starts
    server._restore_pending()  # S2
    assert adapters.lane_suffix() == "r"
    read = await server.inbox(agent="codex")
    bodies = [m.get("body", "") for m in read.get("messages", [])]
    assert any(receipt["job_id"] in body for body in bodies), read


@pytest.mark.anyio
async def test_a_job_whose_server_died_is_lost_not_announced_after_the_restart(
    wake, store, monkeypatch
):
    """The other variant: S1 exits before the job finishes. S2 gets the name
    back, the job reads lost, and no completion notice exists to read."""
    from concurrent.futures import Future

    bind(store, "codex:r", 1)  # P claimed R, then exited
    server._restore_pending()  # S1 restores R
    never_run = Future()  # S1 accepts the job and dies before it runs
    with monkeypatch.context() as s1:
        s1.setattr(server._async_executor, "submit", lambda fn: never_run)
        receipt = _admit()
    assert receipt.get("accepted") is True and receipt.get("lane") == "codex:r", receipt
    job = receipt["job_id"]
    with closing(sqlite3.connect(store)) as raw, raw:  # S1's death, as its job records it
        raw.execute("UPDATE jobs SET owner_pid = ?, owner_key = 'gone' WHERE job_id = ?", (GONE, job))
    _restart(store)
    never_run.set_result({"ok": False})  # the slot S1's process held
    server._restore_pending()  # S2
    assert adapters.lane_suffix() == "r"
    assert (await server.job_status(job_id=job)).get("job", {}).get("state") == jobs.LOST
    read = await server.inbox(agent="codex")
    assert not any(job in m.get("body", "") for m in read.get("messages", [])), read


# ── order, and the request's thread ──────────────────────────────────────────


def test_restored_names_go_in_by_durable_place_below_explicit_ones():
    claims, restored = adapters._with_restored(["E"], set(), ["R"], [("R", 3)])
    assert claims == ["R", "E"]
    # Round 5's trace: A restored, renumbered to 2; B, bound later at 4, after it.
    claims, restored = adapters._with_restored(["A"], {"A"}, ["B"], [("A", 2), ("B", 4)])
    assert claims == ["A", "B"]
    # An older one restored late goes below the newer.
    claims, _ = adapters._with_restored(["B"], {"B"}, ["A"], [("A", 1), ("B", 2)])
    assert claims == ["A", "B"]
    # Already listed: stays where it is, with its origin.
    claims, restored = adapters._with_restored(["X", "R"], set(), ["R"], [("R", 1)])
    assert claims == ["X", "R"] and "R" not in restored


@pytest.mark.parametrize(
    "data, expected",
    [
        (meta(), THREAD),
        (meta(source="subagent"), None),
        ({**meta(), "threadId": "not-a-uuid"}, None),
        ({**meta(), "x-codex-turn-metadata": {**meta()["x-codex-turn-metadata"], "thread_id": OTHER}}, None),
        ({}, None),
    ],
)
def test_a_request_is_attributed_only_to_its_own_top_level_thread(data, expected):
    assert codex_queue.thread_of(data) == expected


def desktop(thread=THREAD, session=None, **turn):
    """A call as Codex Desktop makes it (#114): no ``thread_source``, the
    session id beside the thread id. ``session`` stands for a child's root."""
    m = meta(thread=thread)
    m["sessionId"] = session or thread  # the outer id is the same session's
    t = m["x-codex-turn-metadata"]
    del t["thread_source"]
    t.update({"session_id": session or thread, **turn})
    return m


@pytest.mark.parametrize(
    "data, expected",
    [
        (desktop(), THREAD),
        (desktop(thread_source=None), THREAD),
        (desktop(forked_from_thread_id=OTHER), THREAD),  # a fork is its own root
        (desktop(session=OTHER), None),  # a child: its root's session
        (desktop(parent_thread_id=OTHER), None),
        (desktop(thread_source="subagent"), None),
        (desktop(thread_source=""), None),
        (desktop(session_id=None), None),
    ],
    ids=["desktop", "null-source", "fork", "child", "parent", "subagent", "empty-source", "no-session"],
)
def test_a_thread_without_a_source_is_top_level_only_as_its_own_root(data, expected):
    assert codex_queue.thread_of(data) == expected


def test_a_desktop_thread_pins_and_a_second_one_stops_the_wake():
    w = codex_queue.CodexWake(queue=lambda thread, text: {"ok": True})
    w.observe({"_meta": desktop(session=OTHER)})  # a child calls first
    w.observe({"_meta": desktop(parent_thread_id=OTHER)})
    assert w.pinned() is None
    w.observe({"_meta": desktop(turn_id=TURN)})
    assert w.pinned() == THREAD and w.attribution(THREAD) == THREAD
    assert w.turn == TURN  # the turn a notice is steered into
    w.observe({"_meta": desktop(thread=OTHER, session=THREAD)})  # its child
    w.observe({"_meta": desktop(thread=OTHER, parent_thread_id=THREAD)})
    assert w.stopped is False
    w.observe({"_meta": desktop(thread=OTHER)})
    assert w.stopped is True and w.attribution(THREAD) is None


def test_a_subagent_call_does_not_stop_the_wake(wake):
    wake.observe({"_meta": meta(thread=OTHER, source="subagent")})
    assert wake.stopped is False and wake.attribution(THREAD) == THREAD
