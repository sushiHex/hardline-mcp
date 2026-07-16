"""Tests for comms_mcp.adapters — subprocess is monkeypatched (no real spawns)."""

import subprocess

import pytest

from comms_mcp import adapters


class _FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _capture_run(monkeypatch, result=None, exc=None):
    """Patch adapters._run_cmd's subprocess.run; record the argv it was given."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        if exc is not None:
            raise exc
        return result if result is not None else _FakeCompleted(stdout="ok")

    monkeypatch.setattr(adapters.subprocess, "run", fake_run)
    return calls


def test_ask_hermes_shells_hermes_chat(monkeypatch):
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout="hermes says hi"))
    out = adapters.ask("hermes", "what is your status")
    assert out["ok"] is True
    assert out["reply"] == "hermes says hi"
    argv = calls[0]["cmd"]
    assert "hermes" in argv[0].lower() or argv[0] == "hermes"
    assert "chat" in argv and "what is your status" in argv


def test_ask_codex_shells_codex_exec(monkeypatch):
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout="codex reply"))
    out = adapters.ask("codex", "summarize")
    assert out["ok"] is True
    assert out["reply"] == "codex reply"
    argv = calls[0]["cmd"]
    assert argv[0] == "codex" and "exec" in argv


def test_ask_claude_shells_claude_p(monkeypatch):
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout="claude reply"))
    out = adapters.ask("claude", "hello")
    assert out["ok"] is True
    argv = calls[0]["cmd"]
    assert argv[0] == "claude" and "-p" in argv


def test_ask_unknown_agent_rejected(monkeypatch):
    calls = _capture_run(monkeypatch)
    out = adapters.ask("nobody", "hi")
    assert out["ok"] is False
    assert "unknown" in out["error"].lower()
    assert calls == []  # never spawned


def test_ask_nonzero_exit_is_not_ok(monkeypatch):
    _capture_run(monkeypatch, _FakeCompleted(stdout="", stderr="boom", returncode=1))
    out = adapters.ask("hermes", "x")
    assert out["ok"] is False
    assert "boom" in out["error"]


def test_ask_timeout_is_handled(monkeypatch):
    _capture_run(monkeypatch, exc=subprocess.TimeoutExpired(cmd="hermes", timeout=120))
    out = adapters.ask("hermes", "x")
    assert out["ok"] is False
    assert "timeout" in out["error"].lower()


def test_ask_missing_binary_is_handled(monkeypatch):
    _capture_run(monkeypatch, exc=FileNotFoundError("hermes not found"))
    out = adapters.ask("hermes", "x")
    assert out["ok"] is False
    assert "not found" in out["error"].lower() or "not installed" in out["error"].lower()


def test_deliver_uses_same_agent_dispatch(monkeypatch):
    """deliver(agent, notice) pushes a one-shot notification via the agent's
    native mechanism — same dispatch table as ask()."""
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout="delivered"))
    out = adapters.deliver("hermes", "you have 1 new message; call inbox")
    assert out["ok"] is True
    argv = calls[0]["cmd"]
    assert "you have 1 new message; call inbox" in argv
