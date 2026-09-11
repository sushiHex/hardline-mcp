from concurrent.futures import wait
import gc
import threading
import weakref

import pytest

from hardline_mcp.dispatch import CancellableExecutor


def test_cancel_removes_prompt_and_notifies_waiters_before_worker_is_free():
    started, release = threading.Event(), threading.Event()

    class Work:
        def __call__(self):
            raise AssertionError("cancelled work must not execute")

    with CancellableExecutor(1) as pool:
        first = pool.submit(lambda: (started.set(), release.wait(10)))
        assert started.wait(5)
        try:
            work = Work()
            reference = weakref.ref(work)
            cancelled = pool.submit(work)
            del work
            assert cancelled.cancel()
            gc.collect()
            assert reference() is None
            assert wait([cancelled], timeout=0).done == {cancelled}
            replacement = pool.submit(lambda: "replacement")
        finally:
            release.set()
        assert replacement.result(timeout=5) == "replacement"
        first.result(timeout=5)


def test_task_failure_does_not_stop_queue_and_shutdown_rejects_new_work():
    pool = CancellableExecutor(1)

    def fail():
        raise ValueError("task failed")

    failed = pool.submit(fail)
    following = pool.submit(lambda: 42)
    with pytest.raises(ValueError, match="task failed"):
        failed.result(timeout=5)
    assert following.result(timeout=5) == 42
    pool.shutdown()
    with pytest.raises(RuntimeError, match="shutdown"):
        pool.submit(lambda: None)


def test_shutdown_cancels_waiting_work():
    started, release = threading.Event(), threading.Event()
    pool = CancellableExecutor(1)
    first = pool.submit(lambda: (started.set(), release.wait(10)))
    assert started.wait(5)
    queued = pool.submit(lambda: None)
    try:
        pool.shutdown(wait=False, cancel_futures=True)
        assert queued.cancelled()
        assert wait([queued], timeout=0).done == {queued}
        assert not first.cancelled()
    finally:
        release.set()
        pool.shutdown()
