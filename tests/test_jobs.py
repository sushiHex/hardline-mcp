"""Tests for hardline_mcp.jobs — durable identity for async dispatches.

Async dispatch was fire-and-forget and process-local: a restart lost the task
with no record it had existed, and the only lifecycle API was polling a mailbox
that cannot answer "is it still running?". These cover the record that fixes it.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest

from hardline_mcp import jobs, mailbox

_T0 = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)


def _clock(start):
    state = {"now": start}

    def now_fn():
        return state["now"]

    now_fn.tick = lambda s: state.__setitem__("now", state["now"] + timedelta(seconds=s))
    return now_fn


def _run_to_completion(db, job_id, result):
    """Claim the job, then finish it — the real lifecycle.

    finish() is a compare-and-swap from `running`, so calling it on a queued
    job is an invalid transition and now correctly does nothing. Several tests
    used to skip the claim and silently relied on a blind write.
    """
    assert jobs.mark_running(job_id, db_path=db) is True
    jobs.finish(job_id, result=result, db_path=db)


def _create(db, **kw):
    return jobs.create(
        agent=kw.get("agent", "codex"),
        requester=kw.get("requester", "claude:sess.1234abcd"),
        label=kw.get("label"),
        request=kw.get("request", {"prompt_chars": 12}),
        db_path=db,
        now_fn=kw.get("now_fn", _clock(_T0)),
    )


def test_a_new_job_is_queued_and_owned_by_this_process(tmp_path):
    db = tmp_path / "mb.db"
    job_id = _create(db, label="review-1")
    job = jobs.get(job_id, db_path=db)

    assert job_id.startswith("job_")
    assert job["state"] == jobs.QUEUED
    assert job["label"] == "review-1"
    assert job["owner_pid"] == os.getpid()
    assert job["request"] == {"prompt_chars": 12}
    assert job["result"] is None if "result" in job else True


def test_job_ids_are_unique_even_when_labels_repeat(tmp_path):
    db = tmp_path / "mb.db"
    ids = {_create(db, label="same") for _ in range(50)}
    assert len(ids) == 50


def test_lifecycle_records_timings_and_terminal_result(tmp_path):
    db = tmp_path / "mb.db"
    clock = _clock(_T0)
    job_id = _create(db, now_fn=clock)

    jobs.mark_running(job_id, child_pid=4242, db_path=db, now_fn=clock)
    running = jobs.get(job_id, db_path=db)
    assert running["state"] == jobs.RUNNING
    assert running["child_pid"] == 4242
    assert running["started_at"] is not None
    assert running["finished_at"] is None

    clock.tick(30)
    jobs.finish(job_id, result={"ok": True, "reply": "done"}, db_path=db, now_fn=clock)
    done = jobs.get(job_id, db_path=db)
    assert done["state"] == jobs.COMPLETED
    assert done["result"] == {"ok": True, "reply": "done"}
    assert done["finished_at"] is not None
    assert done["error"] is None


def test_a_failed_result_is_recorded_as_failed_with_its_error(tmp_path):
    db = tmp_path / "mb.db"
    job_id = _create(db)
    jobs.mark_running(job_id, db_path=db)
    jobs.finish(
        job_id,
        result={"ok": False, "error": "timeout after 900s", "timed_out": True},
        db_path=db,
    )
    job = jobs.get(job_id, db_path=db)
    assert job["state"] == jobs.FAILED
    assert job["error"] == "timeout after 900s"
    # The timeout evidence survives on the job, not only in a mailbox message.
    assert job["result"]["timed_out"] is True


def test_a_job_whose_owner_died_is_reported_lost_not_running(tmp_path):
    """The state that could not exist before: a restart lost the task with no
    record. ``lost`` is resolved on read because the process that would have
    written it is exactly the one that died."""
    db = tmp_path / "mb.db"
    job_id = _create(db)
    jobs.mark_running(job_id, db_path=db)

    # Re-point the row at a pid that cannot be running.
    with mailbox._connect(db) as conn:
        conn.execute(
            "UPDATE jobs SET owner_pid = ? WHERE job_id = ?", (0x7FFFFFFE, job_id)
        )
        conn.commit()

    job = jobs.get(job_id, db_path=db)
    assert job["state"] == jobs.LOST
    assert "exited" in job["error"]

    # And it is persisted, not recomputed forever.
    with mailbox._connect(db) as conn:
        stored = conn.execute(
            "SELECT state FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
    assert stored == jobs.LOST


def test_a_live_owner_is_never_declared_lost(tmp_path):
    db = tmp_path / "mb.db"
    job_id = _create(db)
    jobs.mark_running(job_id, db_path=db)  # owner_pid is THIS process
    assert jobs.get(job_id, db_path=db)["state"] == jobs.RUNNING


def test_pid_alive_agrees_with_reality(tmp_path):
    assert jobs.pid_alive(os.getpid()) is True
    assert jobs.pid_alive(0x7FFFFFFE) is False
    assert jobs.pid_alive(None) is False
    assert jobs.pid_alive(0) is False
    assert jobs.pid_alive(-1) is False


def test_finished_jobs_are_never_reclassified_as_lost(tmp_path):
    db = tmp_path / "mb.db"
    job_id = _create(db)
    _run_to_completion(db, job_id, {"ok": True, "reply": "r"})
    with mailbox._connect(db) as conn:
        conn.execute(
            "UPDATE jobs SET owner_pid = ? WHERE job_id = ?", (0x7FFFFFFE, job_id)
        )
        conn.commit()
    assert jobs.get(job_id, db_path=db)["state"] == jobs.COMPLETED


def test_cancel_marks_cancelled_and_reports_no_child_when_not_started(tmp_path):
    db = tmp_path / "mb.db"
    job_id = _create(db)
    out = jobs.request_cancel(job_id, db_path=db)
    assert out["ok"] is True
    assert out["state"] == jobs.CANCELLED
    assert out["child_killed"] is False
    assert "had not spawned" in out["note"]
    assert jobs.get(job_id, db_path=db)["state"] == jobs.CANCELLED


def test_cancel_kills_the_recorded_child_tree(tmp_path, monkeypatch):
    """Cancellation goes through the recorded child pid rather than an
    in-process handle, so a session can stop a job another session started."""
    db = tmp_path / "mb.db"
    job_id = _create(db)
    jobs.mark_running(job_id, child_pid=31337, db_path=db)

    killed = []
    monkeypatch.setattr(
        jobs,
        "kill_process_tree",
        lambda pid, expect_key=None: (killed.append((pid, expect_key)) or (True, None)),
    )
    out = jobs.request_cancel(job_id, db_path=db)

    assert killed == [(31337, None)]
    assert out["child_killed"] is True
    assert jobs.get(job_id, db_path=db)["state"] == jobs.CANCELLED


def test_claiming_a_cancelled_job_fails_so_it_never_starts(tmp_path):
    """The race that made cancel meaningless: mark_running is conditional on
    the job still being queued, and ignoring its result let a cancelled job
    spawn its subprocess anyway - the row said cancelled while the expensive
    work carried on."""
    db = tmp_path / "mb.db"
    job_id = _create(db)
    jobs.request_cancel(job_id, db_path=db)

    assert jobs.mark_running(job_id, db_path=db) is False
    assert jobs.get(job_id, db_path=db)["state"] == jobs.CANCELLED


def test_claiming_a_queued_job_succeeds_exactly_once(tmp_path):
    db = tmp_path / "mb.db"
    job_id = _create(db)
    assert jobs.mark_running(job_id, db_path=db) is True
    assert jobs.mark_running(job_id, db_path=db) is False  # already claimed


def test_a_pid_cannot_be_recorded_onto_a_cancelled_job(tmp_path):
    """Writing a child pid onto a row that has since been cancelled would
    hand a later killer the identity of a process it may not signal."""
    db = tmp_path / "mb.db"
    job_id = _create(db)
    jobs.request_cancel(job_id, db_path=db)
    assert jobs.set_child_pid(job_id, 31337, db_path=db) is False
    assert jobs.get(job_id, db_path=db)["child_pid"] is None


def test_cancel_loses_cleanly_when_the_job_finishes_mid_cancel(tmp_path, monkeypatch):
    """The race the claim-first ordering exists for.

    Cancel used to read the state, kill, and only then write. A job that
    completed IN THAT WINDOW got a process killed for nothing while the
    caller was still told it had been cancelled. Note the interleaving is
    injected rather than hoped for: finishing the job before calling cancel
    would be caught by the up-front terminal check and would never exercise
    the conditional claim at all - which is exactly how an earlier version of
    this test passed against the unfixed code.
    """
    db = tmp_path / "mb.db"
    job_id = _create(db)
    jobs.mark_running(job_id, child_pid=31337, db_path=db)

    killed = []
    monkeypatch.setattr(
        jobs,
        "kill_process_tree",
        lambda pid, expect_key=None: (killed.append(pid) or (True, None)),
    )

    real_resolve = jobs._resolve_lost

    def resolve_then_let_the_owner_win(conn, row, now_fn):
        job = real_resolve(conn, row, now_fn)
        jobs.finish(job_id, result={"ok": True, "reply": "beat you"}, db_path=db)
        return job

    monkeypatch.setattr(jobs, "_resolve_lost", resolve_then_let_the_owner_win)

    out = jobs.request_cancel(job_id, db_path=db)
    assert out["ok"] is False
    assert out["state"] == jobs.COMPLETED
    assert killed == []  # nothing signalled on a job we did not win
    assert jobs.get(job_id, db_path=db)["state"] == jobs.COMPLETED


def test_a_real_result_supersedes_a_heuristic_lost(tmp_path):
    """``lost`` is inferred from "the owner's pid is not alive"; a terminal
    result from the owner itself is direct evidence and outranks it. Refusing
    to overwrite meant a misclassified job discarded a real result."""
    db = tmp_path / "mb.db"
    job_id = _create(db)
    jobs.mark_running(job_id, db_path=db)
    # Misclassified while THIS process is still alive and finishing - owner_pid
    # stays ours, which is what makes the correction legitimate.
    with mailbox._connect(db) as conn:
        conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (jobs.LOST, job_id))
        conn.commit()

    jobs.finish(job_id, result={"ok": True, "reply": "it existed after all"}, db_path=db)
    job = jobs.get(job_id, db_path=db)
    assert job["state"] == jobs.COMPLETED
    assert job["result"]["reply"] == "it existed after all"


def test_a_foreign_process_cannot_resurrect_a_genuinely_lost_job(tmp_path):
    """Superseding `lost` is a self-correction, not a general resurrection.

    A genuinely lost job's owner is dead and cannot call finish(); allowing
    any caller to overwrite it would silently erase the provenance of a job
    that really was abandoned.
    """
    db = tmp_path / "mb.db"
    job_id = _create(db)
    jobs.mark_running(job_id, db_path=db)
    with mailbox._connect(db) as conn:
        conn.execute(
            "UPDATE jobs SET state = ?, owner_pid = ? WHERE job_id = ?",
            (jobs.LOST, 0x7FFFFFFE, job_id),  # owned by a process that is not us
        )
        conn.commit()

    jobs.finish(job_id, result={"ok": True, "reply": "not mine to write"}, db_path=db)
    assert jobs.get(job_id, db_path=db)["state"] == jobs.LOST


def test_finish_cannot_skip_the_claim(tmp_path):
    """queued -> completed without ever claiming the job was permitted by the
    old `state != cancelled` guard, so a caller that never ran anything could
    write a result."""
    db = tmp_path / "mb.db"
    job_id = _create(db)
    jobs.finish(job_id, result={"ok": True, "reply": "never ran"}, db_path=db)
    assert jobs.get(job_id, db_path=db)["state"] == jobs.QUEUED


def test_finish_cannot_rewrite_a_terminal_result(tmp_path):
    """completed -> failed and failed -> completed were both permitted."""
    db = tmp_path / "mb.db"
    job_id = _create(db)
    _run_to_completion(db, job_id, {"ok": True, "reply": "the real answer"})
    jobs.finish(job_id, result={"ok": False, "error": "stale worker"}, db_path=db)
    job = jobs.get(job_id, db_path=db)
    assert job["state"] == jobs.COMPLETED
    assert job["result"]["reply"] == "the real answer"


def test_finish_clears_the_child_pid_so_it_cannot_be_killed_later(tmp_path):
    """A finished child's pid is a stale identity; leaving it on the row is
    what a later cancel would aim at."""
    db = tmp_path / "mb.db"
    job_id = _create(db)
    jobs.mark_running(job_id, child_pid=31337, db_path=db)
    jobs.finish(job_id, result={"ok": True, "reply": "done"}, db_path=db)
    assert jobs.get(job_id, db_path=db)["child_pid"] is None


def test_kill_refuses_a_pid_whose_identity_no_longer_matches(tmp_path):
    """A pid is not an identity. If the child exited and its pid was reused,
    taskkill /T would destroy an unrelated process tree."""
    ok, err = jobs.kill_process_tree(os.getpid(), expect_key="definitely-not-mine")
    assert ok is False
    assert err == jobs.IDENTITY_MISMATCH

    # A pid that no longer exists is refused too, rather than signalled blind.
    ok, err = jobs.kill_process_tree(0x7FFFFFFE, expect_key="anything")
    assert ok is False
    assert err == jobs.ALREADY_GONE


def test_cancel_warns_when_the_child_survived_the_kill(tmp_path, monkeypatch):
    """A row that says cancelled while a real process keeps running must not
    be reported identically to a clean cancel."""
    db = tmp_path / "mb.db"
    job_id = _create(db)
    jobs.mark_running(job_id, child_pid=31337, db_path=db)
    monkeypatch.setattr(
        jobs, "kill_process_tree", lambda pid, expect_key=None: (False, "taskkill: boom")
    )

    out = jobs.request_cancel(job_id, db_path=db)
    assert out["ok"] is True and out["child_killed"] is False
    assert "may still be running" in out["warning"]
    assert "31337" in out["warning"]


def test_cancel_warns_when_it_killed_without_verifying_identity(tmp_path, monkeypatch):
    """A kill with no identity check is the original unsafe behaviour, reached
    exactly when the probe failed. Allowed, but never reported as if it were
    a verified kill."""
    db = tmp_path / "mb.db"
    job_id = _create(db)
    jobs.mark_running(job_id, child_pid=31337, db_path=db)  # no child_key
    monkeypatch.setattr(
        jobs, "kill_process_tree", lambda pid, expect_key=None: (True, None)
    )

    out = jobs.request_cancel(job_id, db_path=db)
    assert out["identity_verified"] is False
    assert "WITHOUT verifying" in out["warning"]


def test_cancel_reports_a_verified_kill_without_a_warning(tmp_path, monkeypatch):
    db = tmp_path / "mb.db"
    job_id = _create(db)
    jobs.mark_running(job_id, child_pid=31337, db_path=db)
    jobs.set_child_pid(job_id, 31337, started_key="tok", db_path=db)
    monkeypatch.setattr(
        jobs, "kill_process_tree", lambda pid, expect_key=None: (True, None)
    )

    out = jobs.request_cancel(job_id, db_path=db)
    assert out["identity_verified"] is True
    assert "warning" not in out


@pytest.mark.parametrize("reason", [jobs.ALREADY_GONE, jobs.IDENTITY_MISMATCH])
def test_cancel_does_not_warn_when_the_child_was_already_dead(
    tmp_path, monkeypatch, reason
):
    """Both refusals mean our child is gone, so the cancel is clean - warning
    there would train the reader to ignore the warning that matters."""
    db = tmp_path / "mb.db"
    job_id = _create(db)
    jobs.mark_running(job_id, child_pid=31337, db_path=db)
    monkeypatch.setattr(
        jobs, "kill_process_tree", lambda pid, expect_key=None: (False, reason)
    )
    assert "warning" not in jobs.request_cancel(job_id, db_path=db)


def test_process_key_identifies_an_instance_not_a_slot(tmp_path):
    mine = jobs.process_key(os.getpid())
    assert mine and jobs.process_key(os.getpid()) == mine  # stable
    assert jobs.process_key(0x7FFFFFFE) is None  # no such process
    assert jobs.process_key(0) is None


def test_listing_finds_an_orphan_even_when_filtering_on_lost(tmp_path):
    """SQL applies `state='lost'` BEFORE lazy resolution could reclassify the
    row, so the query could not find the very jobs it exists to surface."""
    db = tmp_path / "mb.db"
    job_id = _create(db)
    jobs.mark_running(job_id, db_path=db)
    with mailbox._connect(db) as conn:
        conn.execute(
            "UPDATE jobs SET owner_pid = ? WHERE job_id = ?", (0x7FFFFFFE, job_id)
        )
        conn.commit()

    # No job_status() call first - the filter itself must resolve it.
    found = jobs.listing(state=jobs.LOST, db_path=db)
    assert [j["job_id"] for j in found] == [job_id]
    assert jobs.listing(active_only=True, db_path=db) == []
    assert jobs.counts(db_path=db) == {jobs.LOST: 1}


def test_sweep_probes_once_per_owner_and_starves_nobody(tmp_path, monkeypatch):
    """Liveness is a property of the OWNER, not of each row.

    Probing per row asked the OS the same question once per job, which forced
    a bound on the scan - and any bound has an order, so whichever end it
    favoured, rows at the other end could be starved indefinitely by enough
    long-running jobs at the favoured end. Driving the sweep by distinct owner
    removes both the amplification and the bound.
    """
    db = tmp_path / "mb.db"
    dead_pid = 0x7FFFFFFE

    orphans = []
    for _ in range(40):
        job_id = _create(db)
        jobs.mark_running(job_id, db_path=db)
        orphans.append(job_id)
    with mailbox._connect(db) as conn:
        conn.execute(
            "UPDATE jobs SET owner_pid = ? WHERE job_id IN ({})".format(
                ",".join("?" for _ in orphans)
            ),
            (dead_pid, *orphans),
        )
        conn.commit()

    mine = [_create(db) for _ in range(5)]
    for job_id in mine:
        jobs.mark_running(job_id, db_path=db)

    probed = []
    real_alive = jobs.pid_alive
    monkeypatch.setattr(
        jobs, "pid_alive", lambda pid: (probed.append(pid), real_alive(pid))[1]
    )

    lost = jobs.listing(state=jobs.LOST, limit=200, db_path=db)
    # Snapshot before any further call, since each listing sweeps again.
    probed_by_one_listing = sorted(probed)

    # Every orphan resolved, none starved by a scan bound.
    assert {j["job_id"] for j in lost} == set(orphans)
    # ...our own jobs untouched...
    still_active = jobs.listing(active_only=True, limit=200, db_path=db)
    assert {j["job_id"] for j in still_active} == set(mine)
    # ...and 45 active rows cost two probes, one per distinct owner.
    assert probed_by_one_listing == sorted({dead_pid, os.getpid()})


def test_cancel_refuses_a_job_that_already_finished(tmp_path):
    db = tmp_path / "mb.db"
    job_id = _create(db)
    _run_to_completion(db, job_id, {"ok": True, "reply": "r"})
    out = jobs.request_cancel(job_id, db_path=db)
    assert out["ok"] is False and "already" in out["error"]


def test_cancel_is_not_overwritten_by_the_kill_it_caused(tmp_path):
    """Killing the child makes the run exit non-zero. That is the expected
    consequence of the cancel, not a new failure, so the state must stay
    cancelled while still recording what the killed run produced."""
    db = tmp_path / "mb.db"
    job_id = _create(db)
    jobs.mark_running(job_id, child_pid=None, db_path=db)
    jobs.request_cancel(job_id, db_path=db)

    jobs.finish(job_id, result={"ok": False, "error": "exit 1: killed"}, db_path=db)
    job = jobs.get(job_id, db_path=db)
    assert job["state"] == jobs.CANCELLED
    assert job["result"] == {"ok": False, "error": "exit 1: killed"}


def test_cancel_reports_an_unknown_job(tmp_path):
    assert jobs.request_cancel("job_nope", db_path=tmp_path / "mb.db")["ok"] is False


def test_listing_filters_and_is_newest_first(tmp_path):
    db = tmp_path / "mb.db"
    clock = _clock(_T0)
    made = []
    for i in range(5):
        made.append(_create(db, agent="codex" if i % 2 else "claude", now_fn=clock))
        clock.tick(60)

    newest_first = jobs.listing(db_path=db)
    assert [j["job_id"] for j in newest_first] == list(reversed(made))

    only_codex = jobs.listing(agent="codex", db_path=db)
    assert {j["agent"] for j in only_codex} == {"codex"}

    _run_to_completion(db, made[0], {"ok": True, "reply": "x"})
    assert len(jobs.listing(state=jobs.COMPLETED, db_path=db)) == 1
    assert made[0] not in {j["job_id"] for j in jobs.listing(active_only=True, db_path=db)}


def test_listing_is_bounded_like_every_other_read(tmp_path):
    db = tmp_path / "mb.db"
    for _ in range(jobs.MAX_JOB_LIMIT + 10):
        _create(db)
    assert len(jobs.listing(limit=10_000, db_path=db)) == jobs.MAX_JOB_LIMIT
    assert len(jobs.listing(limit=-1, db_path=db)) == 1
    assert len(jobs.listing(limit="junk", db_path=db)) == jobs.DEFAULT_JOB_LIMIT


def test_counts_summarize_states(tmp_path):
    db = tmp_path / "mb.db"
    a, b = _create(db), _create(db)
    _run_to_completion(db, a, {"ok": True, "reply": "x"})
    assert jobs.counts(db_path=db) == {jobs.QUEUED: 1, jobs.COMPLETED: 1}
    assert b  # silence the unused warning; its queued row is the other count


def test_a_jobs_table_without_child_key_is_migrated(tmp_path):
    """The real migration case, which the legacy-store test does NOT cover:
    that one has no jobs table at all, so _SCHEMA creates a fresh one already
    containing child_key and _add_missing_columns never runs."""
    import sqlite3

    db = tmp_path / "old-jobs.db"
    with sqlite3.connect(str(db)) as raw:
        raw.execute(
            "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, agent TEXT NOT NULL,"
            " requester TEXT NOT NULL, label TEXT, state TEXT NOT NULL,"
            " request TEXT NOT NULL, result TEXT, error TEXT,"
            " owner_pid INTEGER NOT NULL, child_pid INTEGER,"
            " created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT)"
        )
        raw.execute(
            "INSERT INTO jobs (job_id, agent, requester, state, request,"
            " owner_pid, created_at) VALUES"
            " ('job_old', 'codex', 'hermes', 'completed', '{}', 1, '2026-08-01')"
        )
        raw.commit()

    # Writing child_key would fail outright without the ALTER.
    job_id = _create(db)
    jobs.mark_running(job_id, db_path=db)
    assert jobs.set_child_pid(job_id, 4242, started_key="k", db_path=db) is True
    assert jobs.get(job_id, db_path=db)["child_key"] == "k"
    # The pre-existing row survives the migration.
    assert jobs.get("job_old", db_path=db)["state"] == jobs.COMPLETED


def test_migration_tolerates_a_concurrent_initializer(tmp_path, monkeypatch):
    """Check-then-ALTER is a race with many processes on one store: both can
    see the column missing, one adds it, the other gets "duplicate column".
    That is the desired end state reached by someone else - and it is neither
    "locked" nor "busy", so the retry loop would re-raise it."""
    import sqlite3

    db = tmp_path / "racy.db"
    _create(db)  # establish the schema

    class _ClaimsColumnIsMissing:
        """sqlite3.Connection is immutable, so wrap rather than patch."""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a, **kw):
            if sql.startswith("PRAGMA table_info"):
                # The pre-migration shape, which forces the ALTER to run
                # against a table that ALREADY has the column - exactly what
                # the losing process in the race sees.
                return [(0, "job_id"), (1, "agent")]
            return self._real.execute(sql, *a, **kw)

    with sqlite3.connect(str(db)) as conn:
        mailbox._add_missing_columns(_ClaimsColumnIsMissing(conn))  # must not raise


def test_schema_version_is_recorded_in_the_store(tmp_path):
    db = tmp_path / "mb.db"
    _create(db)
    with mailbox._connect(db) as conn:
        value = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert int(value) == mailbox.SCHEMA_VERSION


def test_jobs_table_is_added_to_an_existing_messages_only_store(tmp_path):
    """Existing installs have a messages-only database. The new tables must
    appear without a migration step or the first job write fails."""
    import sqlite3

    db = tmp_path / "legacy.db"
    with sqlite3.connect(str(db)) as raw:
        raw.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " sender TEXT NOT NULL, recipient TEXT NOT NULL, body TEXT NOT NULL,"
            " created_at TEXT NOT NULL, acked_at TEXT)"
        )
        raw.execute(
            "INSERT INTO messages (sender, recipient, body, created_at)"
            " VALUES ('claude', 'hermes', 'pre-existing', '2026-08-01T00:00:00Z')"
        )
        raw.commit()

    job_id = _create(db)  # must not raise
    assert jobs.get(job_id, db_path=db)["state"] == jobs.QUEUED
    # ...and the pre-existing mail is untouched.
    msgs, _ = mailbox.inbox("hermes", auto_ack=False, db_path=db)
    assert [m["body"] for m in msgs] == ["pre-existing"]
