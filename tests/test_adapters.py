"""Tests for hardline_mcp.adapters — subprocess is monkeypatched (no real spawns)."""

import subprocess
import json

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
    assert calls[0]["kwargs"]["timeout"] == 180


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
    assert out["reply"] == "claude reply"
    argv = calls[0]["cmd"]
    assert argv[0] == "claude" and "-p" in argv
    # Default calls must pin an explicit model - never the bare command,
    # which would silently inherit the installed Claude CLI's own global
    # default (ambient, mutable state; see test_default_model_is_not_hardcoded_version).
    assert argv[argv.index("--model") + 1] == "sonnet"
    # Prompt is separated so a prompt starting with "-" can't be read as a flag.
    assert argv[-2:] == ["--", "hello"]


def test_deliver_to_claude_also_pins_default_model(monkeypatch):
    """The send(deliver=true) push-notice path uses deliver() == ask(), which
    must get the same model pin as a direct ask_claude() call - not the raw,
    unpinned claude -p dispatch other agents use."""
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout="delivered"))
    out = adapters.deliver("claude", "[hardline] new message #1 from hermes.")
    assert out["ok"] is True
    argv = calls[0]["cmd"]
    assert argv[argv.index("--model") + 1] == "sonnet"


def test_default_model_is_not_hardcoded_version():
    """`sonnet` is a tier alias Claude Code itself resolves, not a versioned
    id like "claude-sonnet-5" - so this constant never needs bumping when a
    new Sonnet ships, matching how explicit model="fable"/"opus" already work."""
    assert adapters._CLAUDE_DEFAULT_MODEL == "sonnet"
    assert not any(char.isdigit() for char in adapters._CLAUDE_DEFAULT_MODEL)


def test_ask_claude_explicit_sonnet_still_gets_full_telemetry(monkeypatch):
    """An *explicit* model="sonnet" is a different caller intent than
    omitting model - it must still take the stream-json/telemetry path,
    same as any other explicit model, not the lightweight default shortcut."""
    stdout = _claude_stream(
        {
            "type": "system",
            "subtype": "init",
            "model": "claude-sonnet-5",
            "apiKeySource": "none",
        },
        {
            "type": "assistant",
            "message": {"model": "claude-sonnet-5", "content": []},
        },
        {"type": "result", "subtype": "success", "result": "reviewed"},
    )
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout=stdout))

    out = adapters.ask_claude("hello", model="sonnet")

    assert out["ok"] is True
    assert out["actual_model"] == "claude-sonnet-5"
    assert out["requested_model"] == "sonnet"
    argv = calls[0]["cmd"]
    assert "--output-format" in argv and "stream-json" in argv


def test_ask_claude_uses_longer_default_timeout(monkeypatch):
    monkeypatch.delenv("HARDLINE_CLAUDE_TIMEOUT_S", raising=False)
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout="claude reply"))

    out = adapters.ask("claude", "perform a substantive review")

    assert out["ok"] is True
    assert calls[0]["kwargs"]["timeout"] == 900


def test_ask_claude_timeout_can_be_configured(monkeypatch):
    monkeypatch.setenv("HARDLINE_CLAUDE_TIMEOUT_S", "1200")
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout="claude reply"))

    out = adapters.ask("claude", "perform a very substantive review")

    assert out["ok"] is True
    assert calls[0]["kwargs"]["timeout"] == 1200


@pytest.mark.parametrize("value", ["forever", "", "   ", "0", "-1"])
@pytest.mark.parametrize("optioned", [False, True])
def test_ask_claude_rejects_invalid_configured_timeout(monkeypatch, value, optioned):
    monkeypatch.setenv("HARDLINE_CLAUDE_TIMEOUT_S", value)
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout="must not run"))

    if optioned:
        out = adapters.ask_claude("hello", model="fable")
    else:
        out = adapters.ask("claude", "hello")

    assert out["ok"] is False
    assert "HARDLINE_CLAUDE_TIMEOUT_S" in out["error"]
    assert calls == []


@pytest.mark.parametrize("agent", ["hermes", "codex"])
def test_non_claude_timeout_is_not_environment_configurable(monkeypatch, agent):
    monkeypatch.setenv(f"HARDLINE_{agent.upper()}_TIMEOUT_S", "1200")
    if agent == "codex":
        monkeypatch.setattr(adapters, "_discover_codex", lambda: None)
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout="reply"))

    out = adapters.ask(agent, "hello")

    assert out["ok"] is True
    assert calls[0]["kwargs"]["timeout"] == 180


def _claude_stream(*events):
    return "\n".join(json.dumps(event) for event in events) + "\n"


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_ask_claude_routes_model_and_effort(monkeypatch, effort):
    monkeypatch.delenv("HARDLINE_CLAUDE_TIMEOUT_S", raising=False)
    stdout = _claude_stream(
        {
            "type": "system",
            "subtype": "init",
            "model": "claude-fable-5",
            "apiKeySource": "none",
        },
        {
            "type": "assistant",
            "message": {
                "model": "claude-fable-5",
                "content": [{"type": "text", "text": "answer"}],
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "result": "answer",
            "usage": {"input_tokens": 2, "output_tokens": 1},
            "modelUsage": {"claude-fable-5": {"inputTokens": 2, "outputTokens": 1}},
        },
    )
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout=stdout))

    out = adapters.ask_claude("review this", model="fable", effort=effort)

    assert out["ok"] is True
    assert out["reply"] == "answer"
    assert out["requested_model"] == "fable"
    assert out["actual_model"] == "claude-fable-5"
    assert out["requested_effort"] == effort
    assert out["effective_effort"] is None  # Claude does not echo this value.
    assert out["api_key_source"] == "none"
    assert out["usage"]["input_tokens"] == 2
    argv = calls[0]["cmd"]
    assert argv[0] == "claude"
    assert argv[1] == "-p"
    assert argv[argv.index("--model") + 1] == "fable"
    assert argv[argv.index("--effort") + 1] == effort
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    assert argv[-1] == "review this"
    assert calls[0]["kwargs"]["timeout"] == 900


def test_ask_claude_default_effort_omits_flag(monkeypatch):
    stdout = _claude_stream(
        {"type": "system", "subtype": "init", "model": "claude-sonnet-5"},
        {"type": "result", "subtype": "success", "result": "ok"},
    )
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout=stdout))

    out = adapters.ask_claude("hello", model="sonnet", effort="default")

    assert out["ok"] is True
    assert "--effort" not in calls[0]["cmd"]
    assert out["requested_effort"] == "default"


@pytest.mark.parametrize("effort", ["none", "minimal", "ultra", "", "HIGH"])
def test_ask_claude_rejects_unsupported_effort(monkeypatch, effort):
    calls = _capture_run(monkeypatch)

    out = adapters.ask_claude("hello", model="fable", effort=effort)

    assert out["ok"] is False
    assert "effort" in out["error"].lower()
    assert calls == []


def test_ask_claude_reports_refusal_fallback(monkeypatch):
    stdout = _claude_stream(
        {"type": "system", "subtype": "init", "model": "claude-fable-5"},
        {
            "type": "system",
            "subtype": "model_refusal_fallback",
            "original_model": "claude-fable-5",
            "fallback_model": "claude-opus-4-8",
            "api_refusal_category": "cyber",
        },
        {"type": "assistant", "message": {"model": "claude-opus-4-8", "content": []}},
        {"type": "result", "subtype": "success", "result": "fallback answer"},
    )
    _capture_run(monkeypatch, _FakeCompleted(stdout=stdout))

    out = adapters.ask_claude("review", model="fable", effort="high")

    assert out["ok"] is True
    assert out["actual_model"] == "claude-opus-4-8"
    assert out["fallback"] == {
        "type": "model_refusal_fallback",
        "original_model": "claude-fable-5",
        "fallback_model": "claude-opus-4-8",
        "category": "cyber",
    }


def test_ask_claude_advisory_isolates_context_and_api_overrides(monkeypatch, tmp_path):
    for name in adapters._CLAUDE_AUTH_OVERRIDE_ENV:
        monkeypatch.setenv(name, "must-not-leak")
    stdout = _claude_stream(
        {
            "type": "system",
            "subtype": "init",
            "model": "claude-fable-5",
            "apiKeySource": "none",
        },
        {
            "type": "rate_limit_event",
            "rate_limit_info": {"isUsingOverage": False, "rateLimitType": "seven_day"},
        },
        {"type": "result", "subtype": "success", "result": "ok"},
    )
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout=stdout))
    monkeypatch.setattr(adapters.tempfile, "mkdtemp", lambda prefix: str(tmp_path))

    out = adapters.ask_claude("review", model="fable", effort="high", mode="advisory")

    assert out["ok"] is True
    assert out["subscription_verified"] is True
    argv = calls[0]["cmd"]
    assert "--safe-mode" in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert "--disable-slash-commands" in argv
    assert "--no-session-persistence" in argv
    assert "--system-prompt" in argv
    assert calls[0]["kwargs"]["cwd"] == str(tmp_path)
    child_env = calls[0]["kwargs"]["env"]
    assert all(name not in child_env for name in adapters._CLAUDE_AUTH_OVERRIDE_ENV)


@pytest.mark.parametrize(
    ("api_key_source", "rate_limit"),
    [
        ("environment", {"isUsingOverage": False}),
        ("none", {"isUsingOverage": True}),
        ("none", None),
    ],
)
def test_ask_claude_advisory_fails_closed_without_base_subscription_evidence(
    monkeypatch, tmp_path, api_key_source, rate_limit
):
    events = [
        {
            "type": "system",
            "subtype": "init",
            "model": "claude-fable-5",
            "apiKeySource": api_key_source,
        }
    ]
    if rate_limit is not None:
        events.append({"type": "rate_limit_event", "rate_limit_info": rate_limit})
    events.append({"type": "result", "subtype": "success", "result": "ok"})
    _capture_run(monkeypatch, _FakeCompleted(stdout=_claude_stream(*events)))
    monkeypatch.setattr(adapters.tempfile, "mkdtemp", lambda prefix: str(tmp_path))

    out = adapters.ask_claude("review", model="fable", mode="advisory")

    assert out["ok"] is False
    assert out["subscription_verified"] is False
    assert "subscription" in out["error"].lower()


def test_ask_claude_rejects_unknown_mode(monkeypatch):
    calls = _capture_run(monkeypatch)
    out = adapters.ask_claude("hello", mode="unsafe")
    assert out["ok"] is False
    assert "mode" in out["error"].lower()
    assert calls == []


def test_ask_claude_maps_advisory_tempdir_failure(monkeypatch):
    calls = _capture_run(monkeypatch)

    def fail_mkdtemp(*, prefix):
        raise OSError("no writable temp directory")

    monkeypatch.setattr(adapters.tempfile, "mkdtemp", fail_mkdtemp)

    out = adapters.ask_claude("hello", model="fable", mode="advisory")

    assert out["ok"] is False
    assert "temporary directory" in out["error"].lower()
    assert "no writable temp directory" in out["error"]
    assert calls == []


def test_ask_claude_rejects_flag_shaped_model(monkeypatch):
    calls = _capture_run(monkeypatch)
    out = adapters.ask_claude("hello", model="--dangerously-skip-permissions")
    assert out["ok"] is False
    assert "model" in out["error"].lower()
    assert calls == []


def test_ask_claude_separates_flag_shaped_prompt(monkeypatch):
    stdout = _claude_stream(
        {"type": "system", "subtype": "init", "model": "claude-fable-5"},
        {"type": "result", "subtype": "success", "result": "ok"},
    )
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout=stdout))

    out = adapters.ask_claude("--model opus", model="fable")

    assert out["ok"] is True
    assert calls[0]["cmd"][-2:] == ["--", "--model opus"]


def test_ask_claude_rejects_malformed_stream(monkeypatch):
    _capture_run(monkeypatch, _FakeCompleted(stdout="not-json\n"))
    out = adapters.ask_claude("hello", model="fable")
    assert out["ok"] is False
    assert "stream-json" in out["error"]


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
    assert (
        "not found" in out["error"].lower() or "not installed" in out["error"].lower()
    )


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


# --------------------------------------------------------------------------
# _run_cmd hardening: isolate stdin (this is a stdio MCP server — a spawned
# child must NOT inherit the JSON-RPC pipe) and decode robustly (an agent
# emitting non-ASCII must not crash the tool with UnicodeDecodeError).
# --------------------------------------------------------------------------


def test_run_cmd_isolates_stdin_and_decodes_utf8(monkeypatch):
    monkeypatch.delenv("HARDLINE_HERMES_CMD", raising=False)
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout="ok"))
    adapters.ask("hermes", "hi")
    kw = calls[0]["kwargs"]
    assert kw.get("stdin") == subprocess.DEVNULL
    assert kw.get("encoding") == "utf-8"
    assert kw.get("errors") == "replace"


def test_known_agents_is_the_fixed_roster():
    assert set(adapters.known_agents()) == {"claude", "hermes", "codex"}
