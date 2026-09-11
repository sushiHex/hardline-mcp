from concurrent.futures import Future, ThreadPoolExecutor
import threading

import pytest

from hardline_mcp import jobs, server


def dispatch(**options):
    return server._ask_async_impl(
        "codex",
        options.pop("ask", lambda *args, **kwargs: {"ok": True}),
        options.pop("prompt", "work"),
        "claude",
        **{
            "label": None,
            "model": None,
            "effort": "default",
            "mode": "default",
            "workdir": None,
            "write": False,
            **options,
        },
    )


def test_concurrent_admission_bounds_running_and_queued(monkeypatch, tmp_path):
    monkeypatch.setenv("HARDLINE_DB", str(tmp_path / "mb.db"))
    monkeypatch.setattr(server, "_ASYNC_MAX_PENDING", 3)
    monkeypatch.setattr(server, "_async_slots", threading.BoundedSemaphore(3))
    release, running = threading.Event(), threading.Semaphore(0)

    def ask(*args, **kwargs):
        running.release()
        assert release.wait(10)
        return {"ok": True}

    with ThreadPoolExecutor(2) as workers:
        monkeypatch.setattr(server, "_async_executor", workers)
        try:
            with ThreadPoolExecutor(10) as callers:
                receipts = list(callers.map(lambda _: dispatch(ask=ask), range(10)))
            assert sum(r.get("accepted", False) for r in receipts) == 3
            assert running.acquire(timeout=5) and running.acquire(timeout=5)
            assert not running.acquire(blocking=False)
            rows = jobs.listing()
            assert sorted(row["state"] for row in rows) == [
                "queued",
                "running",
                "running",
            ]
            refused = [r for r in receipts if not r["accepted"]]
            assert all(r["retryable"] and "job_id" not in r for r in refused)
        finally:
            release.set()
    assert all(row["state"] == "completed" for row in jobs.listing())


@pytest.mark.parametrize(
    "options",
    [
        {"effort": "imaginary"},
        {"mode": "imaginary"},
        {"model": "bad model"},
        {"workdir": "directory-that-does-not-exist"},
        {"write": True},
        {"prompt": "  "},
    ],
)
def test_invalid_request_is_rejected_before_admission(monkeypatch, tmp_path, options):
    monkeypatch.setenv("HARDLINE_DB", str(tmp_path / "mb.db"))
    monkeypatch.delenv("HARDLINE_ALLOW_WRITE", raising=False)
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(server, "_async_slots", slots)
    result = dispatch(**options)
    assert result["accepted"] is False
    assert "capacity" not in result["error"]
    assert jobs.listing() == []


def test_receipt_reports_queued_without_waiting_and_completion_releases_slot(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HARDLINE_DB", str(tmp_path / "mb.db"))
    monkeypatch.setattr(server, "_async_slots", threading.BoundedSemaphore(1))
    pending = []

    class Unwaitable(Future):
        def result(self, timeout=None):
            raise AssertionError("admission must not wait for execution")

    def defer(fn):
        future = Unwaitable()
        pending.append(lambda: future.set_result(fn()))
        return future

    monkeypatch.setattr(server._async_executor, "submit", defer)
    first = dispatch()
    assert first["accepted"] is True and first["state"] == "queued"
    assert first["dispatched"] is False
    assert dispatch()["accepted"] is False
    pending.pop(0)()
    assert jobs.get(first["job_id"])["state"] == "completed"
    assert dispatch()["accepted"] is True
    pending.pop(0)()


def test_submission_failure_releases_capacity_and_finishes_record(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HARDLINE_DB", str(tmp_path / "mb.db"))
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(server, "_async_slots", slots)

    def unavailable(*args):
        raise RuntimeError("executor stopped")

    monkeypatch.setattr(server._async_executor, "submit", unavailable)
    result = dispatch()
    assert result["accepted"] is False
    assert jobs.get(result["job_id"])["state"] == "failed"
    assert slots.acquire(blocking=False)
    slots.release()


@pytest.mark.anyio
@pytest.mark.parametrize("remote", [False, True])
async def test_queued_cancellation_admits_replacement_while_worker_is_busy(
    monkeypatch, tmp_path, remote
):
    from hardline_mcp.dispatch import CancellableExecutor

    monkeypatch.setenv("HARDLINE_DB", str(tmp_path / "mb.db"))
    monkeypatch.setattr(server, "_async_slots", threading.BoundedSemaphore(2))
    started, release = threading.Event(), threading.Event()
    executed = []

    def busy(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return {"ok": True}

    with CancellableExecutor(1) as pool:
        monkeypatch.setattr(server, "_async_executor", pool)
        try:
            dispatch(ask=busy)
            assert started.wait(5)
            queued = dispatch(
                ask=lambda *args, **kwargs: executed.append(True) or {"ok": True}
            )
            assert queued["state"] == jobs.QUEUED
            if remote:
                jobs.request_cancel(queued["job_id"])
            else:
                await server.job_cancel(queued["job_id"])
            replacement = dispatch()
            assert replacement["accepted"] is True
            assert replacement["state"] == jobs.QUEUED
            assert (
                jobs.get(queued["job_id"])["result"]["cancelled_before_start"] is True
            )
            assert executed == []
        finally:
            release.set()
    assert executed == []
