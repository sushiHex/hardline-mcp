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
    jobs.finish(job_id, result={"ok": True, "reply": "r"}, db_path=db)
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
        jobs, "kill_process_tree", lambda pid: (killed.append(pid) or (True, None))
    )
    out = jobs.request_cancel(job_id, db_path=db)

    assert killed == [31337]
    assert out["child_killed"] is True
    assert jobs.get(job_id, db_path=db)["state"] == jobs.CANCELLED


def test_cancel_refuses_a_job_that_already_finished(tmp_path):
    db = tmp_path / "mb.db"
    job_id = _create(db)
    jobs.finish(job_id, result={"ok": True, "reply": "r"}, db_path=db)
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

    jobs.finish(made[0], result={"ok": True, "reply": "x"}, db_path=db)
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
    jobs.finish(a, result={"ok": True, "reply": "x"}, db_path=db)
    assert jobs.counts(db_path=db) == {jobs.QUEUED: 1, jobs.COMPLETED: 1}
    assert b  # silence the unused warning; its queued row is the other count


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
