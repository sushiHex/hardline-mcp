"""Tests for hardline_mcp.adapters — subprocess is monkeypatched (no real spawns)."""

import subprocess

import pytest

from hardline_mcp import adapters


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
    assert "chat" in argv and "-Q" in argv and "what is your status" in argv
    # -Q must precede -q so -q consumes the prompt, not -Q
    assert argv.index("-Q") < argv.index("-q")


def test_ask_codex_shells_codex_exec(monkeypatch):
    # isolate from this machine's real codex install: no env, no discovery,
    # so it falls to the bare "codex" on PATH.
    monkeypatch.delenv("HARDLINE_CODEX_CMD", raising=False)
    monkeypatch.setattr(adapters, "_discover_codex", lambda: None)
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


def test_env_override_replaces_binary_but_keeps_subcommand(monkeypatch):
    """HARDLINE_*_CMD overrides only the executable path; the fixed subcommand
    (chat -q / exec / -p) must still be appended, and the prompt after it."""
    monkeypatch.setenv("HARDLINE_HERMES_CMD", "C:/x/hermes.exe")
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout="ok"))
    adapters.ask("hermes", "status?")
    assert calls[0]["cmd"] == ["C:/x/hermes.exe", "chat", "-Q", "-q", "status?"]


def test_deliver_uses_same_agent_dispatch(monkeypatch):
    """deliver(agent, notice) pushes a one-shot notification via the agent's
    native mechanism — same dispatch table as ask()."""
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout="delivered"))
    out = adapters.deliver("hermes", "you have 1 new message; call inbox")
    assert out["ok"] is True
    argv = calls[0]["cmd"]
    assert "you have 1 new message; call inbox" in argv


# --------------------------------------------------------------------------
# codex binary auto-discovery — the install dir is hash-named and rotates on
# every Codex update, so a hardcoded path rots. Discovery picks the newest.
# --------------------------------------------------------------------------

def test_codex_discovery_picks_newest_install(monkeypatch, tmp_path):
    import os
    base = tmp_path / "OpenAI" / "Codex" / "bin"
    old = base / "aaaa1111"
    new = base / "bbbb2222"
    for d in (old, new):
        d.mkdir(parents=True)
        (d / "codex.exe").write_text("x")
    # make `new` newer than `old`
    os.utime(old / "codex.exe", (1000, 1000))
    os.utime(new / "codex.exe", (2000, 2000))

    monkeypatch.setattr(adapters, "_codex_bin_root", lambda: base)
    found = adapters._discover_codex()
    assert found == str(new / "codex.exe")


def test_codex_discovery_returns_none_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(adapters, "_codex_bin_root", lambda: tmp_path / "nope")
    assert adapters._discover_codex() is None


def test_prefix_precedence_env_over_discovery_over_default(monkeypatch):
    # 1. env override wins
    monkeypatch.setenv("HARDLINE_CODEX_CMD", "C:/pinned/codex.exe")
    monkeypatch.setattr(adapters, "_discover_codex", lambda: "C:/found/codex.exe")
    assert adapters._prefix_for("codex")[0] == "C:/pinned/codex.exe"
    # 2. no env -> discovery
    monkeypatch.delenv("HARDLINE_CODEX_CMD", raising=False)
    assert adapters._prefix_for("codex")[0] == "C:/found/codex.exe"
    # 3. no env, discovery fails -> bare default (PATH)
    monkeypatch.setattr(adapters, "_discover_codex", lambda: None)
    assert adapters._prefix_for("codex")[0] == "codex"


def test_non_codex_agents_have_no_discovery(monkeypatch):
    # hermes/claude resolve to bare default when no env override; no discovery.
    monkeypatch.delenv("HARDLINE_HERMES_CMD", raising=False)
    assert adapters._prefix_for("hermes")[0] == "hermes"
