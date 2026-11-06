"""The toolhelp snapshot failure check in procid._windows_parent."""

import ctypes

from hardline_mcp import procid


class _Kernel32:
    """A kernel32 whose snapshot fails the way the real one does: HANDLE is
    unsigned, so INVALID_HANDLE_VALUE arrives as 2**64-1 (or 2**32-1)."""

    def __init__(self):
        self.calls = []

    def CreateToolhelp32Snapshot(self, flags, pid):
        return ctypes.c_void_p(-1).value

    def Process32First(self, snapshot, entry):
        self.calls.append("first")
        return 0

    def CloseHandle(self, handle):
        self.calls.append("close")
        return 1


def test_a_failed_snapshot_is_not_walked_or_closed(monkeypatch):
    fake = _Kernel32()
    monkeypatch.setattr(procid, "_kernel32", lambda: fake)
    assert procid._windows_parent(1234) is None
    assert fake.calls == [], "the pseudo-handle was used or closed"
