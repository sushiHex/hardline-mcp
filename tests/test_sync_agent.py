"""A sync ask_* stops its worker when the caller cancels, and keeps the caller's
idle window open while it waits (#89)."""

import subprocess
import sys
import threading
import time

import anyio
import pytest

from hardline_mcp import procid, server


@pytest.fixture
def sleeper():
    """A real child standing in for a spawned agent."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    yield proc
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=30)


class _Worker:
    """An ask_fn that records one spawn, then blocks until released."""

    def __init__(self, pid=None):
        self.pid, self.release, self.spawned = pid, threading.Event(), threading.Event()
        self.claimed, self.kwargs = None, {}

    def __call__(self, prompt, **kwargs):
        self.kwargs = kwargs
        if self.pid is not None and kwargs.get("on_spawn"):
            self.claimed = kwargs["on_spawn"](self.pid)
        self.spawned.set()
        self.release.wait(60)
        return {"ok": True, "reply": "late"}


async def _cancel_once_spawned(worker, fn=None):
    async with anyio.create_task_group() as tg:
        tg.start_soon(server._sync_agent, None, fn or worker, "p")
        await anyio.to_thread.run_sync(worker.spawned.wait, 30)
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_cancelled_call_kills_the_child_it_spawned(sleeper):
    worker = _Worker(sleeper.pid)
    started = time.monotonic()
    try:
        await _cancel_once_spawned(worker)
        assert time.monotonic() - started < 20, "the cancel waited for the worker"
        deadline = time.monotonic() + 30
        while sleeper.poll() is None and time.monotonic() < deadline:
            time.sleep(0.2)
        assert sleeper.poll() is not None, "the worker's child outlived the cancel"
    finally:
        worker.release.set()


@pytest.mark.anyio
async def test_a_spawn_after_the_cancel_is_refused():
    """Popen comes before on_spawn: a child spawned after the cancel must be
    refused, so _run_cmd kills the one it holds."""
    worker = _Worker()
    try:
        await _cancel_once_spawned(worker)
        assert worker.kwargs["on_spawn"](procid.current_identity()[0]) is False
    finally:
        worker.release.set()


@pytest.mark.anyio
async def test_the_guarded_claude_path_is_told_once_cancelled(monkeypatch):
    """The reserve guard re-asks ``still_wanted`` after its dispatch lock."""
    worker = _Worker()
    monkeypatch.setattr(server, "_ask_claude_with_reserve_guard", worker)
    try:
        await _cancel_once_spawned(worker, fn=worker)
        still_wanted = worker.kwargs.get("still_wanted") or (lambda: None)
        assert still_wanted() is False
    finally:
        worker.release.set()


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["ask_claude", "ask_codex"])
async def test_ask_claude_runs_through_the_cancellable_helper(monkeypatch, tool):
    seen = {}
    monkeypatch.setattr(
        server.adapters, tool, lambda prompt, **kw: seen.update(kw) or {"ok": True, "reply": "r"}
    )
    await getattr(server, tool)(prompt="x")
    assert callable(seen.get("on_spawn")), f"{tool} bypassed the cancellable helper"


class _Ctx:
    def __init__(self, fail=False):
        self.reports, self.fail = [], fail

    async def report_progress(self, progress, total=None, message=None):
        self.reports.append(progress)
        if self.fail:
            raise RuntimeError("stream gone")


def _slow(prompt, **kwargs):
    time.sleep(1.0)
    return {"ok": True, "reply": "done"}


@pytest.mark.anyio
@pytest.mark.parametrize("fail", [False, True])
async def test_a_waiting_call_reports_progress(monkeypatch, fail):
    monkeypatch.setattr(server, "_SYNC_PROGRESS_S", 0.05)
    ctx = _Ctx(fail=fail)
    try:
        result = await server._sync_agent(ctx, _slow, "p")
    except Exception as exc:  # noqa: BLE001 - a failed keep-alive must fail by assertion
        result = exc
    assert result == {"ok": True, "reply": "done"}
    assert len(ctx.reports) >= 2, "no keep-alive while the worker ran"
