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

import os
from typing import Optional

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


def instance_alive(pid: Optional[int], expect_key: Optional[str]) -> bool:
    """Is the process at ``pid`` alive AND still the instance ``expect_key`` named?

    The strict form of ``pid_alive``, for a durable record that will be trusted
    rather than merely reported. It exists because the two-step check has a
    subtlety worth writing down once instead of at each call site: an identity
    that WAS recorded and can no longer be read means gone, not "unverifiable".
    The only process whose key reads as None while it is genuinely alive is one
    we cannot open at all, and a record we cannot verify must not be honoured.

    A record with no key at all is a different case - it was written by a
    platform that would not say - and is allowed through on ``pid_alive``
    alone, matching how ``kill_process_tree`` treats an unverifiable identity.
    """
    if not pid_alive(pid):
        return False
    if expect_key is None:
        return True
    return process_key(int(pid)) == expect_key
