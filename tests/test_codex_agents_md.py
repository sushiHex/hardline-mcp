"""A Codex that cannot load AGENTS.md under the call's sandbox runs without it.

Seen 2026-10-23: Codex 0.164.0 refused every thread/start on Windows once the
permission profile denied any path - its AGENTS.md helper's MXC container got
no SYSTEMROOT (openai/codex#56264) - so every ask_codex failed in 2 s.
"""

import os

import pytest

from hardline_mcp import adapters
from test_adapters import _codex_reply

REFUSAL = (
    "exit 1: Reading additional input from stdin...\nError: thread/start: thread/start failed: "
    "error creating thread: failed to load AGENTS.md instructions for environment `local`: fs "
    "sandbox helper failed with status exit code: 1: MXC launcher: The supplied `process.env` "
    "cannot launch a Windows process container because it is missing the required variable(s): "
    "SYSTEMROOT. (code -32603)"
)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def codex(monkeypatch):
    """Scripted runs: each call pops the next result; the argv of each is kept."""
    monkeypatch.setattr(adapters, "_codex_docs_refused_by", {})
    monkeypatch.setattr(adapters, "_modified", lambda exe: 1)
    monkeypatch.setattr(adapters, "_codex_sandbox", lambda: adapters._CodexSandbox([], {}, profile=True))
    clock = Clock()
    monkeypatch.setattr(adapters.time, "monotonic", clock)
    runs, argvs = [], []

    def run(agent, argv, **kw):
        argvs.append(argv)
        return dict(runs.pop(0))

    monkeypatch.setattr(adapters, "_run_agent_cmd", run)
    return runs, argvs, clock


def _ok(text="fine"):
    return {"ok": True, "reply": _codex_reply(text)}


def _refused(**kw):
    return {"ok": False, "error": REFUSAL, "exit_code": 1, "_stdout": "", **kw}


def _no_docs(argv) -> bool:
    return any(a == "-c" and b == "project_doc_max_bytes=0" for a, b in zip(argv, argv[1:]))


def test_a_refused_thread_start_runs_again_without_agents_md(codex):
    runs, argvs, _ = codex
    runs += [_refused(), _ok()]
    out = adapters.ask_codex("review", resolve_model=False)
    assert out["ok"] is True and out["reply"] == "fine", out
    assert [_no_docs(a) for a in argvs] == [False, True]
    assert "AGENTS.md" in out.get("instructions_note", ""), "the caller is told what it lost"


def test_the_rerun_is_told_to_read_agents_md_itself(codex):
    """The reviewer, not only its caller: it can still read them."""
    runs, argvs, _ = codex
    runs += [_refused(), _ok()]
    adapters.ask_codex("review this", resolve_model=False)
    first, second = (a[a.index("--") + 1:] for a in argvs)
    assert first == ["review this"]
    assert second == [adapters._CODEX_NO_DOCS_INTAKE + "review this"]
    assert "read with your shell the AGENTS.md files" in second[0]


def test_the_refusing_binary_is_remembered_for_a_while(codex):
    runs, argvs, clock = codex
    runs += [_refused(), _ok(), _ok(), _ok()]
    adapters.ask_codex("one", resolve_model=False)
    out = adapters.ask_codex("two", resolve_model=False)
    assert [_no_docs(a) for a in argvs] == [False, True, True], "no doomed first attempt"
    assert "instructions_note" in out
    clock.now += adapters._CODEX_NO_DOCS_MEMORY_S
    out = adapters.ask_codex("three", resolve_model=False)
    assert _no_docs(argvs[-1]) is False, "a launcher may not show an update: try again"
    assert "instructions_note" not in out


def test_an_updated_binary_is_tried_with_agents_md_again(codex, monkeypatch):
    runs, argvs, _ = codex
    runs += [_refused(), _ok(), _ok()]
    adapters.ask_codex("one", resolve_model=False)
    monkeypatch.setattr(adapters, "_modified", lambda exe: 2)
    out = adapters.ask_codex("two", resolve_model=False)
    assert _no_docs(argvs[-1]) is False and "instructions_note" not in out


@pytest.mark.parametrize(
    "failure",
    [
        {"error": "exit 1: thread/start failed: model not found"},
        {"error": "exit 1: thread/start failed: failed to load AGENTS.md instructions: access denied"},
        {"error": REFUSAL, "_stdout": '{"type":"thread.started","thread_id":"t"}\n'},
    ],
    ids=["other-start-failure", "agents-md-other-cause", "after-a-thread-started"],
)
def test_any_other_failure_is_returned_not_retried(codex, failure):
    runs, argvs, _ = codex
    runs += [{"ok": False, "exit_code": 1, **failure}, _ok()]
    out = adapters.ask_codex("review", resolve_model=False)
    assert out["ok"] is False and len(argvs) == 1
    assert "instructions_note" not in out


def test_a_sandbox_without_a_profile_is_never_retried(codex, monkeypatch):
    """No denial, no helper: the refusal cannot be this defect."""
    runs, argvs, _ = codex
    monkeypatch.setattr(adapters, "_codex_sandbox", lambda: adapters._CodexSandbox([], {}))
    runs += [_refused(), _ok()]
    out = adapters.ask_codex("review", resolve_model=False)
    assert out["ok"] is False and len(argvs) == 1


def test_a_binary_that_cannot_be_read_is_still_retried(codex, monkeypatch):
    runs, argvs, _ = codex
    monkeypatch.setattr(adapters, "_modified", lambda exe: None)
    runs += [_refused(), _ok()]
    assert adapters.ask_codex("review", resolve_model=False)["ok"] is True


def test_modified_is_the_files_modification(tmp_path):
    exe = tmp_path / "codex.exe"
    exe.write_bytes(b"one")
    before = adapters._modified(str(exe))
    os.utime(exe, ns=(before + 10**9, before + 10**9))
    assert adapters._modified(str(exe)) == before + 10**9, "an update is a new binary"
    assert adapters._modified(str(tmp_path / "missing.exe")) is None
