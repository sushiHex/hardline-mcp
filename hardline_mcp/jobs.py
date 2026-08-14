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

# Why a kill did not happen. These are sentinels rather than prose because the
# caller must DISTINGUISH them: "the child was already gone" and "the pid now
# belongs to someone else" both mean our child is dead and the cancel is
# clean, while a genuine failure means the row says cancelled and a real
# process may still be running - the one case worth warning about.
ALREADY_GONE = "process is gone; nothing to kill"
IDENTITY_MISMATCH = (
    "refusing to kill: pid was reused by a different process "
    "(identity token does not match the one recorded at spawn)"
)
_CHILD_ALREADY_DEAD = frozenset({ALREADY_GONE, IDENTITY_MISMATCH})


_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WAIT_TIMEOUT = 0x00000102
_kernel32_cache: list = []


def _kernel32():
    """kernel32 with EXPLICIT argtypes/restype, or None off Windows.

    Declaring the signatures is not tidiness. ctypes defaults a function's
    result to C ``int``, so on 64-bit Windows the HANDLE from OpenProcess is
    truncated: the handle is then invalid, every probe built on it fails, and
    the failures are silent and wrong in the dangerous direction - a live
    owner reads as dead, and process_key returns None, which downgrades
    cancellation to killing a bare pid with no identity check at all.
    """
    if os.name != "nt":
        return None
    if _kernel32_cache:
        return _kernel32_cache[0]
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k.OpenProcess.restype = wintypes.HANDLE
    k.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.CloseHandle.argtypes = (wintypes.HANDLE,)
    k.CloseHandle.restype = wintypes.BOOL
    k.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    k.GetProcessTimes.restype = wintypes.BOOL
    _kernel32_cache.append(k)
    return k


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
        # WaitForSingleObject, NOT GetExitCodeProcess. 259 is STILL_ACTIVE, but
        # it is also a perfectly legal exit code, so a process that exited with
        # 259 is indistinguishable from a running one. Waiting with a zero
        # timeout has no such ambiguity: WAIT_TIMEOUT means still running,
        # WAIT_OBJECT_0 means the handle is signalled, i.e. exited.
        kernel32 = _kernel32()
        if kernel32 is None:
            return False
        handle = kernel32.OpenProcess(
            _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
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
    # kill(pid, 0) succeeds for a ZOMBIE, which is dead but unreaped. Treating
    # one as alive would keep every job it owns pinned as active indefinitely.
    state = _proc_state_linux(pid)
    return state != "Z"


def _proc_state_linux(pid: int) -> Optional[str]:
    """Process state letter from /proc, or None where /proc is unavailable."""
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
    except (OSError, ValueError):
        return None
    # comm can contain spaces and parentheses; state is the field after the
    # LAST ')', so split there rather than on the first space.
    tail = data.rpartition(")")[2].split()
    return tail[0] if tail else None


def process_key(pid: int) -> Optional[str]:
    """A token identifying a process INSTANCE, not just its slot.

    Returns the process creation time. A pid is reused after the process
    exits, so a recorded pid alone can name something else entirely by the
    time a cancel arrives — and killing on a stale pid means killing an
    innocent process tree. Pairing the pid with its start time makes that
    detectable.

    ``None`` where the platform will not say; callers must decide what to do
    with an unverifiable identity rather than assume it matches.
    """
    if not pid or pid <= 0:
        return None
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = _kernel32()
            if kernel32 is None:
                return None
            handle = kernel32.OpenProcess(
                _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return None
            try:
                creation = wintypes.FILETIME()
                exit_t = wintypes.FILETIME()
                kernel_t = wintypes.FILETIME()
                user_t = wintypes.FILETIME()
                ok = kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_t),
                    ctypes.byref(kernel_t),
                    ctypes.byref(user_t),
                )
                if not ok:
                    return None
                return f"{creation.dwHighDateTime}:{creation.dwLowDateTime}"
            finally:
                kernel32.CloseHandle(handle)
        # Linux: field 22 of /proc/<pid>/stat is starttime in clock ticks.
        try:
            with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as fh:
                data = fh.read()
        except (OSError, ValueError):
            return None
        fields = data.rpartition(")")[2].split()
        # fields[0] is state, so starttime (field 22 overall) is index 19 here.
        return fields[19] if len(fields) > 19 else None
    except Exception:  # noqa: BLE001 - identity is best effort, never fatal
        return None


def _row_to_dict(row) -> dict:
    job = {
        "job_id": row["job_id"],
        "agent": row["agent"],
        "requester": row["requester"],
        "label": row["label"],
        "state": row["state"],
        "owner_pid": row["owner_pid"],
        "child_pid": row["child_pid"],
        "child_key": row["child_key"] if "child_key" in row.keys() else None,
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
) -> bool:
    """Claim a queued job. Returns False if it was NOT claimable.

    The return value is the point. This is the queued -> running transition,
    and it is conditional on the job still being queued: if a cancel landed
    first the UPDATE matches nothing. Ignoring that let a cancelled job spawn
    its subprocess anyway - the row said cancelled while the expensive work
    carried on, which is worse than not supporting cancel at all.
    """
    db_path = _resolve_db(db_path)
    with closing(_connect(db_path)) as conn:
        with conn:
            cur = conn.execute(
                "UPDATE jobs SET state = ?, started_at = COALESCE(started_at, ?),"
                " child_pid = COALESCE(?, child_pid), owner_pid = ?"
                " WHERE job_id = ? AND state = ?",
                (RUNNING, _iso(now_fn()), child_pid, os.getpid(), job_id, QUEUED),
            )
        return cur.rowcount > 0


def set_child_pid(
    job_id: str,
    child_pid: int,
    *,
    started_key: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> bool:
    """Record the spawned child so a cancel from ANOTHER process can reach it.

    Restricted to a job that is still ``running``: writing a pid onto a row
    that has since been cancelled would hand a killer the identity of a
    process it was never entitled to signal.
    """
    db_path = _resolve_db(db_path)
    with closing(_connect(db_path)) as conn:
        with conn:
            cur = conn.execute(
                "UPDATE jobs SET child_pid = ?, child_key = ?"
                " WHERE job_id = ? AND state = ?",
                (child_pid, started_key, job_id, RUNNING),
            )
        return cur.rowcount > 0


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

    This is a compare-and-swap, not a blind write. ``state != CANCELLED``
    alone was far too wide: it also permitted completed -> failed, failed ->
    completed, and even queued -> completed by a caller that never claimed the
    job at all. A result may only be recorded from ``running`` (the normal
    path) or from ``lost``, and only by the process that owns the row.

    ``lost`` is superseded because it is a heuristic derived from "the owner's
    pid is not alive", and a terminal result from that same owner is direct
    evidence it was wrong - a slow reap or a probe racing the finish should
    not permanently discard a result that genuinely existed. Restricting it to
    the OWNING pid is what keeps that from becoming a general resurrection:
    a genuinely lost job's owner is dead and cannot call this.
    """
    db_path = _resolve_db(db_path)
    ok = bool(result and result.get("ok"))
    state = COMPLETED if ok else FAILED
    error = None if ok else (result or {}).get("error")
    with closing(_connect(db_path)) as conn:
        with conn:
            conn.execute(
                "UPDATE jobs SET state = ?, result = ?, error = ?, finished_at = ?,"
                # A finished child's pid must not stay on the row: it is the
                # stale identity a later cancel could kill something else with.
                " child_pid = NULL, child_key = NULL"
                " WHERE job_id = ? AND state IN (?, ?) AND owner_pid = ?",
                (
                    state,
                    json.dumps(result, default=str) if result is not None else None,
                    error,
                    _iso(now_fn()),
                    job_id,
                    RUNNING,
                    LOST,
                    os.getpid(),
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
        cur = conn.execute(
            "UPDATE jobs SET state = ?, error = ?, finished_at = COALESCE(finished_at, ?)"
            " WHERE job_id = ? AND state IN (?, ?)",
            (
                LOST,
                _OWNER_DIED,
                _iso(now_fn()),
                job["job_id"],
                QUEUED,
                RUNNING,
            ),
        )
    if cur.rowcount == 0:
        # We lost the race: the owner committed a real terminal state between
        # our liveness check and this write. Returning our in-memory verdict
        # anyway would report `lost` for a job the store records as completed
        # - the caller and the database disagreeing about the same job. Re-read
        # and believe the row.
        fresh = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job["job_id"],)
        ).fetchone()
        return _row_to_dict(fresh) if fresh is not None else job
    job["state"] = LOST
    job["error"] = _OWNER_DIED
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


_OWNER_DIED = "owner process exited before recording a terminal state"


def _sweep_lost(conn, now_fn: Callable[[], datetime]) -> None:
    """Resolve orphaned jobs before anyone FILTERS on state.

    Lazy resolution on read is right for a single job, but it cannot be the
    only mechanism for a query: ``state='lost'`` is applied by SQL, so an
    orphaned row still recorded as ``running`` was excluded before the
    liveness check could reclassify it — ``list_jobs(state="lost")`` could not
    find the very jobs it exists to surface, and ``active_only`` returned rows
    that were about to be reclassified. Resolve first, then filter.

    Driven by distinct OWNERS, not by rows. Liveness is a property of the
    owning process, so probing per row asked the OS the same question once per
    job: with several hundred active rows across ~26 servers, a single listing
    meant tens of thousands of identical probes and a separate committed
    transaction per dead row. There are only ever a handful of distinct
    owners, so this is a handful of probes and one UPDATE per dead one.

    It also removes the need to bound the scan at all, and with it the
    starvation that bound caused: any row cap has an order, and whichever end
    it favours, the rows at the other end can be starved indefinitely by
    enough long-running jobs at the favoured end.
    """
    marks = ", ".join("?" for _ in ACTIVE_STATES)
    active = tuple(sorted(ACTIVE_STATES))
    owners = [
        row[0]
        for row in conn.execute(
            f"SELECT DISTINCT owner_pid FROM jobs WHERE state IN ({marks})", active
        ).fetchall()
    ]
    dead = [pid for pid in owners if not pid_alive(pid)]
    if not dead:
        return
    with conn:
        for pid in dead:
            conn.execute(
                f"UPDATE jobs SET state = ?, error = ?,"
                f" finished_at = COALESCE(finished_at, ?)"
                f" WHERE owner_pid = ? AND state IN ({marks})",
                (LOST, _OWNER_DIED, _iso(now_fn()), pid, *active),
            )


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
    with closing(_connect(db_path)) as conn:
        _sweep_lost(conn, now_fn)
        rows = _query(
            conn,
            state=state,
            agent=agent,
            requester=requester,
            active_only=active_only,
            limit=limit,
        )
        return [_row_to_dict(r) for r in rows]


def _query(
    conn,
    *,
    state: Optional[str],
    agent: Optional[str],
    requester: Optional[str],
    active_only: bool,
    limit: int,
):
    """The filtered SELECT, shared so listing and listing_with_counts cannot
    drift apart. Assumes the caller has already swept."""
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
    return conn.execute(sql, tuple(params)).fetchall()


def kill_process_tree(
    pid: int, *, expect_key: Optional[str] = None
) -> tuple[bool, Optional[str]]:
    """Kill a child and everything it spawned.

    Killing only the recorded pid is not enough: ``claude``/``codex`` are
    launchers that spawn the real worker, so the parent dies and the work
    carries on invisibly. Windows needs taskkill /T for the tree; POSIX gets
    the process group when one exists, falling back to the pid.

    ``expect_key`` is the process-identity token recorded at spawn. It is
    checked immediately before signalling, because a pid is not an identity:
    the child may have exited long ago and its pid been reused, and taskkill
    /T on a reused pid would destroy an unrelated process tree. A mismatch is
    refused rather than risked; an unverifiable identity (no key recorded, or
    a platform that will not say) is allowed through, since refusing there
    would mean cancel never works at all.
    """
    if expect_key is not None:
        actual = process_key(pid)
        if actual is None:
            return False, ALREADY_GONE
        if actual != expect_key:
            return False, IDENTITY_MISMATCH
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

        # CLAIM FIRST, then kill. Reading the state, killing, and only then
        # writing left a window where the job completed normally in between:
        # the kill hit a process that was already finishing, the conditional
        # UPDATE matched nothing, and the caller was still told the job had
        # been cancelled. Winning the transition is what earns the right to
        # signal, so exactly one caller can do both.
        with conn:
            cur = conn.execute(
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
        if cur.rowcount == 0:
            current = conn.execute(
                "SELECT state FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            reached = current[0] if current else "unknown"
            return {
                "ok": False,
                "error": f"job reached {reached} before the cancel was applied",
                "state": reached,
            }

        killed, kill_error = (False, None)
        identity_verified = None
        if job["child_pid"]:
            identity_verified = job.get("child_key") is not None
            killed, kill_error = kill_process_tree(
                job["child_pid"], expect_key=job.get("child_key")
            )

    out = {
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
    out["identity_verified"] = identity_verified
    if identity_verified is False and killed:
        # A kill with no identity check is the original unsafe behaviour, and
        # it is reached exactly when the probe FAILED - a transient /proc read
        # error, a permissions problem, a race at spawn. Allowed, because
        # refusing would mean cancel never works where creation time is
        # unavailable, but never silently equivalent to a verified kill.
        out["warning"] = (
            f"killed pid {job['child_pid']} WITHOUT verifying process identity "
            "(no token was recorded at spawn); if that pid had been reused, an "
            "unrelated process tree was killed"
        )
    if job["child_pid"] and not killed and kill_error not in _CHILD_ALREADY_DEAD:
        # The row says cancelled but a real process may still be running, and
        # reporting that identically to a clean cancel would be a quiet lie.
        out["warning"] = (
            "job is recorded cancelled but its child could not be killed "
            f"({kill_error}); it may still be running as pid {job['child_pid']}"
        )
    return out


def listing_with_counts(
    *,
    state: Optional[str] = None,
    agent: Optional[str] = None,
    requester: Optional[str] = None,
    active_only: bool = False,
    limit: int = DEFAULT_JOB_LIMIT,
    db_path: Optional[Path] = None,
    now_fn: Callable[[], datetime] = _default_now,
) -> tuple[list[dict], dict]:
    """One sweep, then both the page and the summary.

    Calling ``listing()`` and ``counts()`` separately swept twice per request.
    Each sweep is a liveness probe per active row, and every dead one it finds
    commits its own transaction - so a single listing across ~26 concurrent
    server processes could mean tens of thousands of OS probes and a burst of
    serialized WAL writers contending with real mailbox reads.
    """
    db_path = _resolve_db(db_path)
    with closing(_connect(db_path)) as conn:
        _sweep_lost(conn, now_fn)
        rows = _query(
            conn,
            state=state,
            agent=agent,
            requester=requester,
            active_only=active_only,
            limit=limit,
        )
        summary = conn.execute(
            "SELECT state, COUNT(*) FROM jobs GROUP BY state"
        ).fetchall()
    return [_row_to_dict(r) for r in rows], {r[0]: r[1] for r in summary}


def counts(
    *,
    db_path: Optional[Path] = None,
    now_fn: Callable[[], datetime] = _default_now,
) -> dict:
    """How many jobs in each state — a one-call health read.

    Sweeps first for the same reason ``listing`` does: a summary that counts
    an orphaned job as ``running`` forever, while the listing beside it shows
    the same job as ``lost``, is worse than no summary.
    """
    db_path = _resolve_db(db_path)
    with closing(_connect(db_path)) as conn:
        _sweep_lost(conn, now_fn)
        rows = conn.execute("SELECT state, COUNT(*) FROM jobs GROUP BY state").fetchall()
    return {r[0]: r[1] for r in rows}


def python_executable() -> str:
    """Recorded in job requests so a stale interpreter is visible in the row."""
    return sys.executable
