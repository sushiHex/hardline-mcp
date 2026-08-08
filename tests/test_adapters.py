"""Tests for hardline_mcp.adapters — subprocess is monkeypatched (no real spawns)."""

import json
import subprocess

import pytest

from hardline_mcp import adapters


@pytest.fixture
def allow_write(monkeypatch):
    """Opt this hardline-mcp process into write mode, the way an operator
    would. Tests of the gate *itself* set the variable explicitly instead —
    for them its value is the thing under test, not setup."""
    monkeypatch.setenv("HARDLINE_ALLOW_WRITE", "1")


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
    # Omitted model -> no --model flag at all; Codex's own configured default
    # applies, same posture ask_hermes already has toward Hermes's default.
    assert "--model" not in argv
    assert "--ephemeral" in argv
    assert argv[-2:] == ["--", "summarize"]


def test_deliver_to_codex_defers_to_codex_own_default(monkeypatch):
    monkeypatch.delenv("HARDLINE_CODEX_CMD", raising=False)
    monkeypatch.setattr(adapters, "_discover_codex", lambda: None)
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout="delivered"))

    out = adapters.deliver("codex", "--dangerously-bypass-approvals-and-sandbox")

    assert out["ok"] is True
    argv = calls[0]["cmd"]
    assert "--model" not in argv
    assert "--ephemeral" in argv
    assert argv[-2:] == ["--", "--dangerously-bypass-approvals-and-sandbox"]


def test_ask_claude_shells_claude_p(monkeypatch):
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout="claude reply"))
    out = adapters.ask("claude", "hello")
    assert out["ok"] is True
    assert out["reply"] == "claude reply"
    argv = calls[0]["cmd"]
    assert argv[0] == "claude" and "-p" in argv
    # Omitted model -> no --model flag at all; Claude Code's own configured
    # default applies (it previously pinned an explicit alias, added when a
    # stale global settings.json override was found governing unflagged
    # calls - that override has since been removed, so there is no longer a
    # reason for hardline to second-guess Claude Code's own default).
    assert "--model" not in argv
    # Prompt is separated so a prompt starting with "-" can't be read as a flag.
    assert argv[-2:] == ["--", "hello"]


def test_deliver_to_claude_also_defers_to_claude_own_default(monkeypatch):
    """The send(deliver=true) push-notice path uses deliver() == ask(), which
    must behave identically to a direct ask_claude() call - not some other
    unpinned claude -p dispatch."""
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout="delivered"))
    out = adapters.deliver("claude", "[hardline] new message #1 from hermes.")
    assert out["ok"] is True
    argv = calls[0]["cmd"]
    assert "--model" not in argv


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


def test_hermes_timeout_is_not_environment_configurable(monkeypatch):
    monkeypatch.setenv("HARDLINE_HERMES_TIMEOUT_S", "1200")
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout="reply"))

    out = adapters.ask("hermes", "hello")

    assert out["ok"] is True
    assert calls[0]["kwargs"]["timeout"] == 180


def _claude_stream(*events):
    return "\n".join(json.dumps(event) for event in events) + "\n"


def _codex_stream(*events):
    return "\n".join(json.dumps(event) for event in events) + "\n"


def test_ask_codex_routes_model_effort_and_reports_json_telemetry(monkeypatch):
    monkeypatch.delenv("HARDLINE_CODEX_CMD", raising=False)
    monkeypatch.setattr(adapters, "_discover_codex", lambda: None)
    stdout = _codex_stream(
        {"type": "thread.started", "thread_id": "thread-123"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"id": "item-1", "type": "agent_message", "text": "answer"},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 12,
                "cached_input_tokens": 3,
                "output_tokens": 4,
                "reasoning_output_tokens": 2,
            },
        },
    )
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout=stdout))

    out = adapters.ask_codex("review this", model="gpt-5.6-terra", effort="xhigh")

    assert out["ok"] is True
    assert out["reply"] == "answer"
    assert out["requested_model"] == "gpt-5.6-terra"
    assert out["actual_model"] is None
    assert out["requested_effort"] == "xhigh"
    assert out["effective_effort"] is None
    assert out["thread_id"] == "thread-123"
    assert out["usage"]["input_tokens"] == 12
    argv = calls[0]["cmd"]
    assert argv[argv.index("--model") + 1] == "gpt-5.6-terra"
    assert argv[argv.index("-c") + 1] == 'model_reasoning_effort="xhigh"'
    assert "--json" in argv
    assert "--ephemeral" in argv
    assert argv[-2:] == ["--", "review this"]


def test_ask_codex_allows_deep_multi_hour_reviews_by_default(monkeypatch):
    monkeypatch.delenv("HARDLINE_CODEX_TIMEOUT_S", raising=False)
    monkeypatch.setattr(adapters, "_discover_codex", lambda: None)
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout="reply"))

    out = adapters.ask("codex", "substantive review")

    assert out["ok"] is True
    assert calls[0]["kwargs"]["timeout"] == 14400


def test_ask_codex_uses_configurable_long_timeout(monkeypatch):
    monkeypatch.setenv("HARDLINE_CODEX_TIMEOUT_S", "1200")
    monkeypatch.setattr(adapters, "_discover_codex", lambda: None)
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout="reply"))

    out = adapters.ask("codex", "substantive review")

    assert out["ok"] is True
    assert calls[0]["kwargs"]["timeout"] == 1200


@pytest.mark.parametrize("value", ["forever", "", "0", "-1"])
def test_ask_codex_rejects_invalid_configured_timeout(monkeypatch, value):
    monkeypatch.setenv("HARDLINE_CODEX_TIMEOUT_S", value)
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout="must not run"))

    out = adapters.ask_codex("hello", model="gpt-5.6-sol")

    assert out["ok"] is False
    assert "HARDLINE_CODEX_TIMEOUT_S" in out["error"]
    assert calls == []


@pytest.mark.parametrize("effort", ["none", "minimal", "", "HIGH"])
def test_ask_codex_rejects_unsupported_effort(monkeypatch, effort):
    calls = _capture_run(monkeypatch)

    out = adapters.ask_codex("hello", model="gpt-5.6-sol", effort=effort)

    assert out["ok"] is False
    assert "effort" in out["error"].lower()
    assert calls == []


@pytest.mark.parametrize("model", ["", "--oss", "gpt 5.6 sol"])
def test_ask_codex_rejects_unsafe_model(monkeypatch, model):
    calls = _capture_run(monkeypatch)

    out = adapters.ask_codex("hello", model=model)

    assert out["ok"] is False
    assert "model" in out["error"].lower()
    assert calls == []


def test_ask_codex_advisory_isolates_context_and_api_overrides(monkeypatch, tmp_path):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "secret"}}),
        encoding="utf-8",
    )
    (codex_home / "AGENTS.md").write_text(
        "Prefix every response with HARDLINE_GLOBAL_SENTINEL.", encoding="utf-8"
    )
    neutral_root = tmp_path / "neutral-root"
    neutral_root.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    for name in adapters._CODEX_AUTH_OVERRIDE_ENV:
        monkeypatch.setenv(name, "must-not-leak")
    monkeypatch.setenv("OPENAI_FUTURE_PROVIDER_SECRET", "must-not-leak")
    monkeypatch.setattr(adapters.tempfile, "mkdtemp", lambda prefix: str(neutral_root))
    removed = []
    monkeypatch.setattr(
        adapters.shutil,
        "rmtree",
        lambda path, ignore_errors: removed.append((path, ignore_errors)),
    )
    stdout = _codex_stream(
        {"type": "thread.started", "thread_id": "thread-advisory"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "reviewed"},
        },
        {"type": "turn.completed", "usage": {"input_tokens": 10}},
    )
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout=stdout))

    out = adapters.ask_codex(
        "review", model="gpt-5.6-sol", effort="high", mode="advisory"
    )

    assert out["ok"] is True
    assert out["subscription_configured"] is True
    assert out["subscription_verified"] is None
    argv = calls[0]["cmd"]
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    assert "--skip-git-repo-check" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    isolated_cwd = neutral_root / "workspace"
    isolated_home = neutral_root / "codex-home"
    assert argv[argv.index("-C") + 1] == str(isolated_cwd)
    assert any("developer_instructions" in arg for arg in argv)
    assert calls[0]["kwargs"]["cwd"] == str(isolated_cwd)
    child_env = calls[0]["kwargs"]["env"]
    assert all(name not in child_env for name in adapters._CODEX_AUTH_OVERRIDE_ENV)
    assert "OPENAI_FUTURE_PROVIDER_SECRET" not in child_env
    assert child_env["CODEX_HOME"] == str(isolated_home)
    assert (isolated_home / "auth.json").exists()
    assert not (isolated_home / "AGENTS.md").exists()
    assert removed == [(str(neutral_root), True)]


def test_ask_codex_uses_explicit_workdir(monkeypatch, tmp_path):
    stdout = _codex_stream(
        {"type": "thread.started", "thread_id": "thread-workdir"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {}},
    )
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout=stdout))

    out = adapters.ask_codex("inspect", workdir=str(tmp_path))

    assert out["ok"] is True
    assert calls[0]["kwargs"]["cwd"] == str(tmp_path)
    argv = calls[0]["cmd"]
    assert argv[argv.index("-C") + 1] == str(tmp_path)


def test_ask_codex_resolves_relative_workdir_once(monkeypatch, tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(tmp_path)
    stdout = _codex_stream(
        {"type": "thread.started", "thread_id": "thread-relative"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {}},
    )
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout=stdout))

    out = adapters.ask_codex("inspect", workdir="child")

    assert out["ok"] is True
    expected = str(child.resolve())
    assert calls[0]["kwargs"]["cwd"] == expected
    argv = calls[0]["cmd"]
    assert argv[argv.index("-C") + 1] == expected


def test_ask_codex_write_disabled_by_default(monkeypatch, tmp_path):
    # write=True must be refused outright unless this process's environment
    # explicitly opts in - closes the gap where any hardline caller (e.g.
    # Hermes, driven by inbound Discord messages with no approval gate on MCP
    # tool calls) could otherwise reach unattended file writes with zero
    # operator opt-in.
    monkeypatch.delenv("HARDLINE_ALLOW_WRITE", raising=False)
    calls = _capture_run(monkeypatch)

    out = adapters.ask_codex("patch it", workdir=str(tmp_path), write=True)

    assert out["ok"] is False
    assert "HARDLINE_ALLOW_WRITE" in out["error"]
    assert calls == []


@pytest.mark.parametrize("value", ["0", "false", "False", "no", "No", ""])
def test_ask_codex_write_disabled_for_falsy_values(monkeypatch, tmp_path, value):
    monkeypatch.setenv("HARDLINE_ALLOW_WRITE", value)
    out = adapters.ask_codex("patch it", workdir=str(tmp_path), write=True)
    assert out["ok"] is False
    assert "HARDLINE_ALLOW_WRITE" in out["error"]


@pytest.mark.parametrize("value", ["1", " 1 ", "true", "True", "TRUE", "yes", "Yes"])
def test_ask_codex_write_enabled_for_truthy_synonyms(monkeypatch, tmp_path, value):
    # A typo'd-but-plausible value (e.g. HARDLINE_ALLOW_WRITE=true) must not
    # be a silent footgun that leaves write mode looking "on" to an operator
    # but actually off - accept the common spellings, case-insensitively.
    monkeypatch.setenv("HARDLINE_ALLOW_WRITE", value)
    stdout = _codex_stream(
        {"type": "thread.started", "thread_id": "thread-write"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "patched"},
        },
        {"type": "turn.completed", "usage": {}},
    )
    _capture_run(monkeypatch, _FakeCompleted(stdout=stdout))

    out = adapters.ask_codex("patch it", workdir=str(tmp_path), write=True)

    assert out["ok"] is True


@pytest.mark.parametrize("value", ["2", "enabled", "tru", "on"])
def test_ask_codex_write_rejects_unrecognized_value(monkeypatch, tmp_path, value):
    # Neither truthy nor falsy: must fail loud naming the bad value, not
    # silently behave as disabled with no indication anything was wrong.
    monkeypatch.setenv("HARDLINE_ALLOW_WRITE", value)
    calls = _capture_run(monkeypatch)

    out = adapters.ask_codex("patch it", workdir=str(tmp_path), write=True)

    assert out["ok"] is False
    assert "HARDLINE_ALLOW_WRITE" in out["error"]
    assert value in out["error"]
    assert calls == []


def test_ask_codex_write_requires_workdir(allow_write):
    out = adapters.ask_codex("patch it", write=True)
    assert out["ok"] is False
    assert "workdir" in out["error"].lower()


def test_ask_codex_write_rejects_advisory_mode(allow_write, tmp_path):
    out = adapters.ask_codex("patch it", write=True, mode="advisory")
    assert out["ok"] is False
    assert "advisory" in out["error"].lower()


def test_ask_codex_write_adds_workspace_write_sandbox(
    allow_write, monkeypatch, tmp_path
):
    stdout = _codex_stream(
        {"type": "thread.started", "thread_id": "thread-write"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "patched"},
        },
        {"type": "turn.completed", "usage": {}},
    )
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout=stdout))

    out = adapters.ask_codex("patch it", workdir=str(tmp_path), write=True)

    assert out["ok"] is True
    argv = calls[0]["cmd"]
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("-a") + 1] == "never"
    assert argv[argv.index("-C") + 1] == str(tmp_path)


def test_ask_codex_write_false_keeps_prior_argv_shape(monkeypatch):
    # Default (write=False) must stay byte-for-byte identical to before this
    # param existed - Hermes's existing bare ask_codex() calls depend on it.
    monkeypatch.delenv("HARDLINE_CODEX_CMD", raising=False)
    monkeypatch.setattr(adapters, "_discover_codex", lambda: None)
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout="codex reply"))

    out = adapters.ask_codex("summarize")

    assert out["ok"] is True
    argv = calls[0]["cmd"]
    assert "--sandbox" not in argv
    assert "-a" not in argv


def test_ask_codex_ignores_transient_error_before_completed_turn(monkeypatch):
    stdout = _codex_stream(
        {"type": "thread.started", "thread_id": "thread-retried"},
        {"type": "error", "message": "transient failure; retrying"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
        {"type": "turn.completed", "usage": {"output_tokens": 1}},
    )
    _capture_run(monkeypatch, _FakeCompleted(stdout=stdout))

    out = adapters.ask_codex("review", model="gpt-5.6-sol")

    assert out["ok"] is True
    assert out["reply"] == "answer"
    assert out["thread_id"] == "thread-retried"


def test_ask_codex_reports_structured_turn_failure(monkeypatch):
    stdout = _codex_stream(
        {"type": "thread.started", "thread_id": "thread-failed"},
        {"type": "turn.failed", "error": {"message": "usage limit reached"}},
    )
    _capture_run(
        monkeypatch,
        _FakeCompleted(stdout=stdout, stderr="codex failed", returncode=1),
    )

    out = adapters.ask_codex("review", model="gpt-5.6-sol")

    assert out["ok"] is False
    assert "usage limit reached" in out["error"]
    assert out["thread_id"] == "thread-failed"


def test_ask_codex_advisory_fails_before_spawn_without_chatgpt_auth(
    monkeypatch, tmp_path
):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "tokens": {"access_token": "secret"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    calls = _capture_run(monkeypatch)

    out = adapters.ask_codex("review", mode="advisory")

    assert out["ok"] is False
    assert out["subscription_configured"] is False
    assert out["subscription_verified"] is None
    assert "ChatGPT" in out["error"]
    assert calls == []


def test_ask_codex_advisory_cleans_up_when_auth_copy_fails(monkeypatch, tmp_path):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps({"auth_mode": "chatgpt"}), encoding="utf-8"
    )
    neutral_root = tmp_path / "neutral-root"
    neutral_root.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(adapters.tempfile, "mkdtemp", lambda prefix: str(neutral_root))
    monkeypatch.setattr(
        adapters.shutil,
        "copyfile",
        lambda *_: (_ for _ in ()).throw(PermissionError("copy denied")),
    )
    removed = []
    monkeypatch.setattr(
        adapters.shutil,
        "rmtree",
        lambda path, ignore_errors: removed.append((path, ignore_errors)),
    )
    calls = _capture_run(monkeypatch)

    out = adapters.ask_codex("review", mode="advisory")

    assert out["ok"] is False
    assert "isolated Codex advisory home" in out["error"]
    assert "copy denied" in out["error"]
    assert calls == []
    assert removed == [(str(neutral_root), True)]


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


def test_ask_claude_default_denies_edit_write(monkeypatch):
    # Parity with Codex: safe by default, matching test_ask_codex_write_false_keeps_prior_argv_shape.
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout="claude reply"))

    out = adapters.ask("claude", "summarize")

    assert out["ok"] is True
    argv = calls[0]["cmd"]
    assert argv[argv.index("--disallowedTools") + 1] == "Edit,Write,NotebookEdit"
    assert "--permission-mode" not in argv


def test_ask_claude_optioned_call_also_denies_edit_write_by_default(
    monkeypatch, tmp_path
):
    stdout = _claude_stream(
        {"type": "system", "subtype": "init", "model": "claude-fable-5"},
        {"type": "result", "subtype": "success", "result": "reviewed"},
    )
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout=stdout))

    out = adapters.ask_claude("review", model="fable", workdir=str(tmp_path))

    assert out["ok"] is True
    argv = calls[0]["cmd"]
    assert argv[argv.index("--disallowedTools") + 1] == "Edit,Write,NotebookEdit"


def test_ask_claude_write_disabled_by_default(monkeypatch, tmp_path):
    # Same gate as Codex's write path - a hardline registration that never
    # sets HARDLINE_ALLOW_WRITE (e.g. Hermes's) cannot reach bypassPermissions
    # regardless of what a caller (or a message that reached it) asks for.
    monkeypatch.delenv("HARDLINE_ALLOW_WRITE", raising=False)
    calls = _capture_run(monkeypatch)

    out = adapters.ask_claude("edit it", workdir=str(tmp_path), write=True)

    assert out["ok"] is False
    assert "HARDLINE_ALLOW_WRITE" in out["error"]
    assert calls == []


def test_ask_claude_write_requires_workdir(allow_write):
    out = adapters.ask_claude("edit it", write=True)
    assert out["ok"] is False
    assert "workdir" in out["error"].lower()


def test_ask_claude_write_rejects_advisory_mode(allow_write, tmp_path):
    out = adapters.ask_claude("edit it", write=True, mode="advisory")
    assert out["ok"] is False
    assert "advisory" in out["error"].lower()


def test_ask_claude_write_grants_full_tools_and_bypasses_permissions(
    allow_write, monkeypatch, tmp_path
):
    stdout = _claude_stream(
        {"type": "system", "subtype": "init", "model": "claude-fable-5"},
        {"type": "result", "subtype": "success", "result": "edited"},
    )
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout=stdout))

    out = adapters.ask_claude("edit it", workdir=str(tmp_path), write=True)

    assert out["ok"] is True
    argv = calls[0]["cmd"]
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--disallowedTools" not in argv
    assert (
        "-C" not in argv
    )  # claude has no -C flag; cwd is set via the subprocess kwarg
    assert calls[0]["kwargs"]["cwd"] == str(tmp_path)


def test_ask_claude_uses_explicit_workdir(monkeypatch, tmp_path):
    stdout = _claude_stream(
        {"type": "system", "subtype": "init", "model": "claude-fable-5"},
        {"type": "result", "subtype": "success", "result": "ok"},
    )
    calls = _capture_run(monkeypatch, _FakeCompleted(stdout=stdout))

    out = adapters.ask_claude("inspect", model="fable", workdir=str(tmp_path))

    assert out["ok"] is True
    assert calls[0]["kwargs"]["cwd"] == str(tmp_path)


def test_ask_claude_advisory_rejects_workdir(monkeypatch, tmp_path):
    out = adapters.ask_claude("review", mode="advisory", workdir=str(tmp_path))
    assert out["ok"] is False
    assert "workdir" in out["error"].lower()


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


# --------------------------------------------------------------------------
# Session lanes. Every Claude Code session spawns its own hardline process, so
# the process IS the session and can derive its own identity - no caller
# declares anything.
# --------------------------------------------------------------------------


def test_lane_is_empty_outside_a_session(monkeypatch):
    """Hermes and Codex set none of these, so they keep their plain
    identities and cross-agent messaging is untouched."""
    assert adapters.lane_suffix() == ""
    assert adapters.lane_for("hermes") == "hermes"


def test_lane_combines_project_and_session_id(in_session):
    # Readable enough to scan in `history`, unique enough that two sessions
    # in the SAME repo don't collide - which project name alone can't do.
    assert adapters.lane_suffix() == "fonts.1a2b3c4d"
    assert adapters.lane_for("claude") == "claude:fonts.1a2b3c4d"


def test_lane_is_stable_across_reconnect(monkeypatch, in_session):
    """A /mcp reconnect respawns this process but keeps the session, so the
    lane must not change - otherwise every in-flight result is orphaned. This
    is why the session id is keyed on rather than anything per-process."""
    first = adapters.lane_suffix()
    monkeypatch.setattr(adapters.os, "getpid", lambda: 999999)  # "new process"
    assert adapters.lane_suffix() == first


def test_explicit_label_overrides_derivation(monkeypatch, in_session):
    monkeypatch.setenv("HARDLINE_AGENT_LABEL", "review-bot")
    assert adapters.lane_for("claude") == "claude:review-bot"


def test_lane_falls_back_to_cwd_when_project_dir_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abcdefgh-0000-0000-0000-000000000000")
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    workdir = tmp_path / "someproject"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    assert adapters.lane_suffix() == "someproject.abcdefgh"


@pytest.mark.parametrize(
    ("name", "expected"),
    [("claude", "claude"), ("claude:fonts.1a2b3c4d", "claude"), ("hermes", "hermes")],
)
def test_base_agent_strips_the_lane(name, expected):
    assert adapters.base_agent(name) == expected
