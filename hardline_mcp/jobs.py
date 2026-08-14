"""Durable job records for async agent dispatches.

Async dispatch was fire-and-forget and process-local. A hardline-mcp restart
lost the task with no record it had ever existed, a timeout produced
``{"ok": false, "error": "timeout after 900s"}`` and nothing else, and the only
lifecycle API was polling a mailbox that could not answer "is it still
running?". Three agent sessions independently called this the single most
valuable thing to fix.

A job row is the durable identity that survives all of it: what was asked, who
asked, which process owns it, when each transition happened, and the terminal
result. It shares the mailbox's SQLite store so a dispatch and its delivery
cannot end up in two different files, and it reuses that module's connection
helpers so WAL setup, busy_timeout, and the cold-start race are handled in one
place rather than two.

States
------
``queued``    accepted, not yet started
``running``   child process spawned
``completed`` finished; ``result`` holds the adapter's reply
``failed``    finished with an error (including timeouts)
``cancelled`` cancelled by request
``lost``      owner process died before recording a terminal state

``lost`` is derived, not written by the thing that died - that is the whole
point. It is resolved on read by asking the OS whether the owning process is
still alive, so it costs nothing until somebody asks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .mailbox import _connect, _default_now, _iso, _resolve_db

QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
LOST = "lost"

TERMINAL_STATES = frozenset({COMPLETED, FAILED, CANCELLED, LOST})
ACTIVE_STATES = frozenset({QUEUED, RUNNING})

DEFAULT_JOB_LIMIT = 25
MAX_JOB_LIMIT = 200


def new_job_id() -> str:
    """Short, unambiguous, and not a sequence number.

    Labels are a human correlation aid the caller chooses and may reuse; a job
    identity must be unique whether or not the caller was careful.
    """
    return f"job_{uuid.uuid4().hex[:12]}"


def pid_alive(pid: Optional[int]) -> bool:
    """Is this process still running?

    Answers the ``lost`` question without a heartbeat thread or a periodic
    write. Several hardline processes share one store, so "owned by a pid that
    is not me" cannot mean dead - only the OS can say.

    PID reuse could in principle make a dead job look alive. On a single-user
    machine over the lifetime of one job that is negligible, and the failure
    direction is the safe one: a job wrongly considered alive is reported as
    still running rather than silently declared lost.
    """
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is not a liveness probe on Windows, so ask the API.
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but is not ours to signal - still alive.
        return True
    except OSError:
        return False
    return True


def _row_to_dict(row) -> dict:
    job = {
        "job_id": row["job_id"],
        "agent": row["agent"],
        "requester": row["requester"],
        "label": row["label"],
        "state": row["state"],
        "owner_pid": row["owner_pid"],
        "child_pid": row["child_pid"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error": row["error"],
    }
    try:
        job["request"] = json.loads(row["request"])
    except (TypeError, ValueError):
        job["request"] = None
    if row["result"] is not None:
        try:
            job["result"] = json.loads(row["result"])
        except (TypeError, ValueError):
            job["result"] = None
    return job


def create(
    *,
    agent: str,
    requester: str,
    label: Optional[str],
    request: dict,
    db_path: Optional[Path] = None,
    now_fn: Callable[[], datetime] = _default_now,
) -> str:
    """Record a job as ``queued`` and return its id."""
    db_path = _resolve_db(db_path)
    job_id = new_job_id()
    with closing(_connect(db_path)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO jobs (job_id, agent, requester, label, state, request,"
                " owner_pid, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    agent,
                    requester,
                    label,
                    QUEUED,
                    json.dumps(request, default=str),
                    os.getpid(),
                    _iso(now_fn()),
                ),
            )
    return job_id


def mark_running(
    job_id: str,
    *,
    child_pid: Optional[int] = None,
    db_path: Optional[Path] = None,
    now_fn: Callable[[], datetime] = _default_now,
) -> None:
    db_path = _resolve_db(db_path)
    with closing(_connect(db_path)) as conn:
        with conn:
            conn.execute(
                "UPDATE jobs SET state = ?, started_at = COALESCE(started_at, ?),"
                " child_pid = COALESCE(?, child_pid), owner_pid = ?"
                " WHERE job_id = ? AND state = ?",
                (RUNNING, _iso(now_fn()), child_pid, os.getpid(), job_id, QUEUED),
            )


def set_child_pid(
    job_id: str, child_pid: int, *, db_path: Optional[Path] = None
) -> None:
    """Record the spawned child so a cancel from ANOTHER process can reach it."""
    db_path = _resolve_db(db_path)
    with closing(_connect(db_path)) as conn:
        with conn:
            conn.execute(
                "UPDATE jobs SET child_pid = ? WHERE job_id = ?", (child_pid, job_id)
            )


def finish(
    job_id: str,
    *,
    result: Optional[dict],
    db_path: Optional[Path] = None,
    now_fn: Callable[[], datetime] = _default_now,
) -> None:
    """Record the terminal state, inferring it from the adapter's result.

    A cancelled job stays cancelled: the child was killed deliberately, so the
    non-zero exit that follows is the expected consequence, not a new failure.
    """
    db_path = _resolve_db(db_path)
    ok = bool(result and result.get("ok"))
    state = COMPLETED if ok else FAILED
    error = None if ok else (result or {}).get("error")
    with closing(_connect(db_path)) as conn:
        with conn:
            conn.execute(
                "UPDATE jobs SET state = ?, result = ?, error = ?, finished_at = ?"
                " WHERE job_id = ? AND state NOT IN (?, ?)",
                (
                    state,
                    json.dumps(result, default=str) if result is not None else None,
                    error,
                    _iso(now_fn()),
                    job_id,
                    CANCELLED,
                    LOST,
                ),
            )
            # A cancelled job still records what the killed run produced.
            conn.execute(
                "UPDATE jobs SET result = COALESCE(result, ?), finished_at ="
                " COALESCE(finished_at, ?) WHERE job_id = ? AND state = ?",
                (
                    json.dumps(result, default=str) if result is not None else None,
                    _iso(now_fn()),
                    job_id,
                    CANCELLED,
                ),
            )


def _resolve_lost(conn, row, now_fn: Callable[[], datetime]) -> dict:
    """Promote an active job whose owner is gone to ``lost``, persistently.

    Written on read rather than by a sweeper: the process that would have
    recorded it is precisely the one that died.
    """
    job = _row_to_dict(row)
    if job["state"] not in ACTIVE_STATES:
        return job
    if pid_alive(job["owner_pid"]):
        return job
    with conn:
        conn.execute(
            "UPDATE jobs SET state = ?, error = ?, finished_at = COALESCE(finished_at, ?)"
            " WHERE job_id = ? AND state IN (?, ?)",
            (
                LOST,
                "owner process exited before recording a terminal state",
                _iso(now_fn()),
                job["job_id"],
                QUEUED,
                RUNNING,
            ),
        )
    job["state"] = LOST
    job["error"] = "owner process exited before recording a terminal state"
    return job


def get(
    job_id: str,
    *,
    db_path: Optional[Path] = None,
    now_fn: Callable[[], datetime] = _default_now,
) -> Optional[dict]:
    db_path = _resolve_db(db_path)
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return _resolve_lost(conn, row, now_fn)


def listing(
    *,
    state: Optional[str] = None,
    agent: Optional[str] = None,
    requester: Optional[str] = None,
    active_only: bool = False,
    limit: int = DEFAULT_JOB_LIMIT,
    db_path: Optional[Path] = None,
    now_fn: Callable[[], datetime] = _default_now,
) -> list[dict]:
    """Jobs newest-first. Bounded like every other read in this package."""
    db_path = _resolve_db(db_path)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_JOB_LIMIT
    limit = max(1, min(limit, MAX_JOB_LIMIT))

    where: list[str] = []
    params: list = []
    if state:
        where.append("state = ?")
        params.append(state)
    if agent:
        where.append("agent = ?")
        params.append(agent)
    if requester:
        where.append("requester = ?")
        params.append(requester)
    if active_only:
        marks = ", ".join("?" for _ in ACTIVE_STATES)
        where.append(f"state IN ({marks})")
        params.extend(sorted(ACTIVE_STATES))

    sql = "SELECT * FROM jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    params.append(limit)

    with closing(_connect(db_path)) as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [_resolve_lost(conn, r, now_fn) for r in rows]


def kill_process_tree(pid: int) -> tuple[bool, Optional[str]]:
    """Kill a child and everything it spawned.

    Killing only the recorded pid is not enough: ``claude``/``codex`` are
    launchers that spawn the real worker, so the parent dies and the work
    carries on invisibly. Windows needs taskkill /T for the tree; POSIX gets
    the process group when one exists, falling back to the pid.
    """
    try:
        if os.name == "nt":
            proc = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=30,
            )
            if proc.returncode != 0:
                return False, (proc.stderr or proc.stdout or "").strip()
            return True, None
        import signal

        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            os.kill(pid, signal.SIGTERM)
        return True, None
    except Exception as exc:  # noqa: BLE001 - cancel must report, never raise
        return False, f"{type(exc).__name__}: {exc}"


def request_cancel(
    job_id: str,
    *,
    db_path: Optional[Path] = None,
    now_fn: Callable[[], datetime] = _default_now,
) -> dict:
    """Cancel a job, from any hardline process.

    Cancellation goes through the recorded ``child_pid`` rather than an
    in-process handle, so a session can cancel a job another session started -
    which is the case that matters, since the dispatcher is often not the one
    watching it run.
    """
    db_path = _resolve_db(db_path)
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": f"no job {job_id!r}"}
        job = _resolve_lost(conn, row, now_fn)
        if job["state"] in TERMINAL_STATES:
            return {
                "ok": False,
                "error": f"job is already {job['state']}",
                "state": job["state"],
            }

        killed, kill_error = (False, None)
        if job["child_pid"]:
            killed, kill_error = kill_process_tree(job["child_pid"])

        with conn:
            conn.execute(
                "UPDATE jobs SET state = ?, error = ?, finished_at = ?"
                " WHERE job_id = ? AND state IN (?, ?)",
                (
                    CANCELLED,
                    "cancelled by request",
                    _iso(now_fn()),
                    job_id,
                    QUEUED,
                    RUNNING,
                ),
            )
    return {
        "ok": True,
        "job_id": job_id,
        "state": CANCELLED,
        "child_killed": killed,
        "kill_error": kill_error,
        "note": (
            None
            if job["child_pid"]
            else "job had not spawned a child yet; marked cancelled before start"
        ),
    }


def counts(*, db_path: Optional[Path] = None) -> dict:
    """How many jobs in each state — a one-call health read."""
    db_path = _resolve_db(db_path)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute("SELECT state, COUNT(*) FROM jobs GROUP BY state").fetchall()
    return {r[0]: r[1] for r in rows}


def python_executable() -> str:
    """Recorded in job requests so a stale interpreter is visible in the row."""
    return sys.executable
