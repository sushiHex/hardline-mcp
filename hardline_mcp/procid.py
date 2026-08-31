"""Process liveness and identity, for callers that must not kill or trust the
wrong process.

Two questions, both harder than they look on Windows:

``pid_alive``   is this process still running?
``process_key`` is it still the *same* process, or has the pid been reused?

They started life in ``jobs`` to resolve the ``lost`` state. ``sessions`` needs
exactly the same two answers to decide whether a registered lane still has a
holder, and "sessions depends on jobs" would describe a relationship that does
not exist. Both depend on process identity, which is neither.

The rule both callers inherit: **a pid is not an identity.** A pid is reused
once its process exits, so anything durable that records one must record the
creation-time token beside it and compare before acting.
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional

_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WAIT_TIMEOUT = 0x00000102
# Why an OpenProcess failed. Without these the two cases that matter - "no such
# process" and "exists, but not yours to open" - are one indistinguishable
# falsy handle.
_ERROR_INVALID_PARAMETER = 87
_ERROR_ACCESS_DENIED = 5
_kernel32_cache: list = []


def _kernel32():
    """kernel32 with EXPLICIT argtypes/restype, or None off Windows.

    Declaring the signatures is not tidiness. ctypes defaults a function's
    result to C ``int``, so on 64-bit Windows the HANDLE from OpenProcess is
    truncated: the handle is then invalid, every probe built on it fails, and
    the failures are silent and wrong in the dangerous direction - a live
    process reads as dead, and process_key returns None, which downgrades
    every identity check built on it to trusting a bare pid.
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
    k.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    k.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.Process32First.restype = wintypes.BOOL
    k.Process32Next.restype = wintypes.BOOL
    _kernel32_cache.append(k)
    return k


def pid_alive(pid: Optional[int]) -> bool:
    """Is this process still running?

    Answers the liveness question without a heartbeat thread or a periodic
    write. Several hardline processes share one store, so "owned by a pid that
    is not me" cannot mean dead - only the OS can say.

    PID reuse could in principle make a dead owner look alive. On a single-user
    machine that is negligible, and the failure direction is the safe one: a
    process wrongly considered alive is reported as still running rather than
    silently declared gone. Callers that will ACT on the answer (kill it, hand
    its lane to someone else) must pair this with ``process_key``.
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
    # one as alive would keep everything it owns pinned as active indefinitely.
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
    time it is acted on — and killing on a stale pid means killing an innocent
    process tree, while trusting one means handing a session's mail to a
    stranger.

    ``None`` where the platform will not say; callers must decide what to do
    with an unverifiable identity rather than assume it matches.

    Windows and Linux are covered. **macOS is not**: there is no ``/proc``, so
    every key is None there and identity degrades to the pid alone - a reused
    pid is undetectable, and a durable record can be honoured for the wrong
    process. The package does not otherwise restrict its platform, so this is a
    real gap rather than an impossible case; it needs ``proc_pidinfo`` via
    ctypes to close. Recorded here rather than silently degraded because the
    None branch in ``instance_alive`` looks deliberate at the call site and
    gives no hint that a whole platform takes it always.
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


ALIVE = "alive"
DEAD = "dead"
UNKNOWN = "unknown"


def instance_state(pid: Optional[int], expect_key: Optional[str]) -> str:
    """``ALIVE``, ``DEAD``, or ``UNKNOWN`` for a recorded (pid, identity) pair.

    Three answers, not two, because "I could not tell" is a fact and collapsing
    it into "dead" is how a live session gets its name given away. A probe can
    fail for reasons that have nothing to do with the process: on Windows,
    ``OpenProcess`` returns ACCESS_DENIED for a process running at a higher
    integrity level, which is indistinguishable from "no such process" unless
    the error code is read.

    Callers must decide what UNKNOWN means for THEM. Reporting a destination
    and deleting its record are not the same decision: the first can afford to
    be optimistic, the second cannot.

    A record with no key at all is not unknown - it was written by a platform
    that would not say, and is honoured on liveness alone, matching how
    ``kill_process_tree`` treats an unverifiable identity.
    """
    presence = _pid_state(pid)
    if presence != ALIVE:
        return presence
    if expect_key is None:
        return ALIVE
    actual = process_key(int(pid))
    if actual is None:
        # The pid is real but its identity will not read. Saying DEAD here is
        # what let a transient permission failure unregister a live session.
        return UNKNOWN
    return ALIVE if actual == expect_key else DEAD


def _pid_state(pid: Optional[int]) -> str:
    """Does this process exist? ``UNKNOWN`` when the OS declines to say.

    ``pid_alive`` answers the same question as a boolean and folds the
    declined case into False, which is right for a caller that only reports
    and wrong for one that deletes.
    """
    if not pid or pid <= 0:
        return DEAD
    if os.name != "nt":
        return ALIVE if pid_alive(pid) else DEAD

    import ctypes

    kernel32 = _kernel32()
    if kernel32 is None:
        return UNKNOWN
    handle = kernel32.OpenProcess(
        _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if handle:
        try:
            return (
                ALIVE
                if kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
                else DEAD
            )
        finally:
            kernel32.CloseHandle(handle)
    # The open failed. WHY it failed is the whole point: a pid that does not
    # exist and a pid we are not allowed to touch look identical without it.
    return open_failure_state(ctypes.get_last_error())


def open_failure_state(err: int) -> str:
    """What a failed ``OpenProcess`` says about the process, by error code.

    A separate function because it is the whole judgement and the only part
    that can be tested without a process that refuses to be opened: everything
    around it is a Win32 call. ``ERROR_INVALID_PARAMETER`` is how Windows says
    there is no such pid. Anything else - denied, or unrecognised - means the
    question went unanswered, and an unanswered question is not a death.
    """
    if err == _ERROR_INVALID_PARAMETER:
        return DEAD
    return UNKNOWN


def image_name(pid: int) -> Optional[str]:
    """The executable name of ``pid``, or None if it will not say."""
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
                _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return None
            try:
                size = wintypes.DWORD(1024)
                buf = ctypes.create_unicode_buffer(size.value)
                if not kernel32.QueryFullProcessImageNameW(
                    handle, 0, buf, ctypes.byref(size)
                ):
                    return None
                return os.path.basename(buf.value)
            finally:
                kernel32.CloseHandle(handle)
        with open(f"/proc/{pid}/comm", "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:  # noqa: BLE001 - a name is best effort, never fatal
        return None


def parent_pid_of(pid: int) -> Optional[int]:
    """The parent of ``pid``. Not this process - see ``os.getppid`` for that."""
    if not pid or pid <= 0:
        return None
    try:
        if os.name == "nt":
            return _windows_parent(pid)
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as fh:
            fields = fh.read().rpartition(")")[2].split()
        return int(fields[1]) if len(fields) > 1 else None
    except Exception:  # noqa: BLE001
        return None


def _windows_parent(pid: int) -> Optional[int]:
    """Parent pid via a toolhelp snapshot - the dependency-free way to ask."""
    import ctypes
    from ctypes import wintypes

    class _ENTRY(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    kernel32 = _kernel32()
    if kernel32 is None:
        return None
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot == -1:
        return None
    try:
        entry = _ENTRY()
        entry.dwSize = ctypes.sizeof(_ENTRY)
        if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
            return None
        while True:
            if entry.th32ProcessID == pid:
                return int(entry.th32ParentProcessID)
            if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                return None
    finally:
        kernel32.CloseHandle(snapshot)


def ancestry(pid: Optional[int] = None, depth: int = 4) -> list[str]:
    """Image names walking up from ``pid``, nearest ancestor first."""
    current = os.getppid() if pid is None else pid
    names: list[str] = []
    seen: set[int] = set()
    for _ in range(depth):
        if not current or current in seen:
            break
        seen.add(current)
        name = image_name(current)
        if not name:
            break
        names.append(name)
        current = parent_pid_of(current) or 0
    return names


def session_token(pid: int) -> Optional[str]:
    """A short, stable identifier for the process INSTANCE at ``pid``.

    Pairs the pid with its creation time before hashing, so a reused pid does
    not inherit the previous process's identifier. Eight hex characters, to
    match the shape of the session-id prefix Claude Code supplies.
    """
    if not pid or pid <= 0:
        return None
    token = process_key(pid)
    if token is None:
        return None
    return hashlib.sha256(f"{pid}:{token}".encode()).hexdigest()[:8]


def instance_alive(pid: Optional[int], expect_key: Optional[str]) -> bool:
    """Boolean form of ``instance_state``: anything but DEAD counts as alive.

    Optimistic by design, and the direction matters. A live process wrongly
    called dead loses its lane to somebody else; a dead one wrongly called
    live is reported as a destination until the next read. Only the first
    destroys anything.
    """
    return instance_state(pid, expect_key) != DEAD
