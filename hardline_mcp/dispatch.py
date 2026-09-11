"""A removable work queue over a fixed number of standard executor workers."""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Executor, Future, ThreadPoolExecutor
import threading


class CancellableExecutor(Executor):
    """Cancellation releases queued callables immediately, including their prompts.

    Only worker loops enter ThreadPoolExecutor's queue. User work stays in a
    removable queue, so repeated cancel-and-replace cannot accumulate cancelled
    work items behind a long-running task. Admission is the caller's policy.
    """

    def __init__(self, max_workers: int):
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="hardline-async"
        )
        self._max_workers = max_workers
        self._workers = 0
        self._pending: OrderedDict[Future, tuple] = OrderedDict()
        self._lock = threading.Lock()
        self._closed = False

    def submit(self, fn, /, *args, **kwargs):
        future = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("cannot schedule new futures after shutdown")
            self._pending[future] = (fn, args, kwargs)
            future.add_done_callback(self._forget)
            if self._workers < self._max_workers:
                self._workers += 1
                try:
                    self._executor.submit(self._work)
                except BaseException:
                    self._workers -= 1
                    self._pending.pop(future)
                    raise
        return future

    def _forget(self, future):
        with self._lock:
            removed = self._pending.pop(future, None)
            if removed is not None and future.cancelled():
                # Match the Future contract for wait()/as_completed() even
                # though a worker will never dequeue this cancelled task.
                future.set_running_or_notify_cancel()

    def _work(self):
        while True:
            with self._lock:
                if not self._pending:
                    self._workers -= 1
                    return
                future, (fn, args, kwargs) = self._pending.popitem(last=False)
                if not future.set_running_or_notify_cancel():
                    continue
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)
            finally:
                del fn, args, kwargs  # do not retain the previous prompt while waiting

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self._lock:
            self._closed = True
            pending = list(self._pending) if cancel_futures else ()
        for future in pending:
            future.cancel()
        self._executor.shutdown(wait=wait)
