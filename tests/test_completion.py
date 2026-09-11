from concurrent.futures import Future
import json
import os
import sqlite3

import pytest

from hardline_mcp import jobs, mailbox, server


def test_result_and_notice_commit_together(tmp_path):
    db = tmp_path / "mb.db"
    job_id = jobs.create(
        agent="codex", requester="claude", label="review", request={}, db_path=db
    )
    jobs.mark_running(job_id, db_path=db)
    result = {"ok": True, "reply": "complete answer " * 10000}
    with mailbox._connect(db) as conn:
        conn.execute(
            "CREATE TRIGGER reject_notice BEFORE INSERT ON messages "
            "BEGIN SELECT RAISE(ABORT, 'notice unavailable'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="notice unavailable"):
        jobs.finish(job_id, result=result, db_path=db)
    row = jobs.get(job_id, db_path=db)
    assert row["state"] == jobs.RUNNING
    assert row.get("result") is None
    assert mailbox.inbox("claude", db_path=db)[0] == []
    with mailbox._connect(db) as conn:
        conn.execute("DROP TRIGGER reject_notice")
    assert jobs.finish(job_id, result=result, db_path=db) is True
    assert jobs.finish(job_id, result={"ok": False}, db_path=db) is False
    notices = mailbox.inbox("claude", auto_ack=False, db_path=db)[0]
    assert len(notices) == 1
    assert len(notices[0]["body"]) < 1024
    notice = json.loads(notices[0]["body"])
    assert notice["job_id"] == job_id and notice["state"] == jobs.COMPLETED
    assert notice["result_with"] == f"job_result(job_id={job_id!r})"
    assert jobs.get(job_id, db_path=db)["result"] == result


def test_cancelled_completion_notifies_once(tmp_path):
    db = tmp_path / "mb.db"
    job_id = jobs.create(
        agent="codex", requester="claude", label=None, request={}, db_path=db
    )
    jobs.request_cancel(job_id, db_path=db)
    assert jobs.finish(job_id, result=None, db_path=db) is True
    assert jobs.finish(job_id, result=None, db_path=db) is False
    notices = mailbox.inbox("claude", auto_ack=False, db_path=db)[0]
    assert len(notices) == 1
    assert json.loads(notices[0]["body"])["state"] == jobs.CANCELLED


@pytest.mark.parametrize("cancelled", [False, True])
def test_worker_retains_dispatch_store(monkeypatch, tmp_path, cancelled):
    original, later = tmp_path / "dispatch.db", tmp_path / "later.db"
    monkeypatch.setenv("HARDLINE_DB", str(original))
    pending = []

    def defer(fn):
        future = Future()
        pending.append(lambda: future.set_result(fn()))
        return future

    monkeypatch.setattr(server._async_executor, "submit", defer)

    def ask(prompt, **kwargs):
        assert kwargs["on_spawn"](os.getpid())
        return {"ok": True, "reply": "done"}

    receipt = server._ask_async_impl(
        "codex",
        ask,
        "test",
        "claude",
        label=None,
        model=None,
        effort="default",
        mode="default",
        workdir=None,
        write=False,
    )
    if cancelled:
        jobs.request_cancel(receipt["job_id"], db_path=original)
    monkeypatch.setenv("HARDLINE_DB", str(later))
    pending[0]()
    row = jobs.get(receipt["job_id"], db_path=original)
    assert row["state"] == (jobs.CANCELLED if cancelled else jobs.COMPLETED)
    assert row["result"] is not None
    assert len(mailbox.inbox("claude", auto_ack=False, db_path=original)[0]) == 1
    assert not later.exists()
