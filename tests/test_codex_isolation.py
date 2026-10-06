"""A spawned Codex reaches no MCP server and no app connector (#42).

Measured before the fix: a default ask_codex child called hardline's own
tools, had node_repl's browser, and searched the web through the `apps`
connector - all unattended, all outside its --sandbox. These tests drive the
real ``_codex_mcp_servers`` against a fake process, so the `codex mcp list`
question and the `codex exec` answer are checked together.
"""

import json

import pytest

from hardline_mcp import adapters
from hardline_mcp.adapters import _codex_mcp_servers as REAL_LISTING

CONNECTORS = ["-c", "features.apps=false", "-c", "features.plugins=false"]


class _Proc:
    pid = 424242

    def __init__(self, stdout, returncode):
        self._out = stdout
        self.returncode = returncode

    def communicate(self, timeout=None):
        return self._out, ""

    def kill(self):
        pass

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def codex(monkeypatch):
    """Real listing; fake processes. ``codex.servers`` is what `mcp list` reports."""
    calls = []

    class State:
        servers = json.dumps([{"name": "hardline"}, {"name": "node_repl"}])
        list_exit = 0

    def popen(cmd, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        if cmd[1:3] == ["mcp", "list"]:
            return _Proc(State.servers, State.list_exit)
        return _Proc("reply", 0)

    monkeypatch.setattr(adapters, "_codex_mcp_servers", REAL_LISTING)
    monkeypatch.setattr(adapters.subprocess, "Popen", popen)
    monkeypatch.setattr(adapters, "_kill_tree", lambda proc: None)
    State.calls = calls
    return State


def _exec(calls):
    (call,) = [c for c in calls if "exec" in c["cmd"]]
    return call


def _before_prompt(argv):
    return argv[: argv.index("--")]


def _pairs(argv):
    return {(a, b) for a, b in zip(argv, argv[1:]) if a == "-c"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # the plain fast path
        {"model": "gpt-5.6-sol"},  # telemetry path
        {"workdir": "."},
        {"write": True, "workdir": "."},
    ],
    ids=["plain", "telemetry", "workdir", "write"],
)
def test_every_codex_child_gets_no_connectors_and_no_servers(codex, monkeypatch, kwargs):
    monkeypatch.setenv("HARDLINE_ALLOW_WRITE", "1")
    adapters.ask_codex("review this", **kwargs)
    argv = _before_prompt(_exec(codex.calls)["cmd"])
    pairs = _pairs(argv)
    assert ("-c", "features.apps=false") in pairs
    assert ("-c", "features.plugins=false") in pairs
    assert ("-c", "mcp_servers.hardline.enabled=false") in pairs
    assert ("-c", "mcp_servers.node_repl.enabled=false") in pairs
    assert "--ignore-user-config" not in argv, "the configured default model is kept"


def test_servers_are_listed_where_and_how_the_child_will_run(codex, monkeypatch, tmp_path):
    monkeypatch.setenv("HARDLINE_ALLOW_WRITE", "1")
    adapters.ask_codex("review", workdir=str(tmp_path))
    (listing,) = [c for c in codex.calls if c["cmd"][1:3] == ["mcp", "list"]]
    assert listing["cmd"][3:] == ["--json", *CONNECTORS], (
        "listed with plugins and apps off, or it names servers config never defines"
    )
    assert listing["kwargs"]["cwd"] == _exec(codex.calls)["kwargs"]["cwd"]
    env = listing["kwargs"]["env"]
    assert env is not None and "HARDLINE_ALLOW_WRITE" not in env, (
        "the listing must run in the child's stripped environment"
    )


def test_an_unlistable_config_fails_closed(codex):
    codex.list_exit = 1
    adapters.ask_codex("review", model="gpt-5.6-sol")
    argv = _before_prompt(_exec(codex.calls)["cmd"])
    assert "--ignore-user-config" in argv
    assert ("-c", "features.apps=false") in _pairs(argv)


def test_an_unaddressable_server_name_fails_closed(codex):
    """`-c mcp_servers.a.b.enabled=false` would address a different key."""
    codex.servers = json.dumps([{"name": "hardline"}, {"name": "team.server"}])
    adapters.ask_codex("review")
    argv = _before_prompt(_exec(codex.calls)["cmd"])
    assert "--ignore-user-config" in argv
    assert not any("team.server" in arg for arg in argv)


def test_unreadable_listing_fails_closed(codex):
    codex.servers = "not json"
    adapters.ask_codex("review")
    assert "--ignore-user-config" in _before_prompt(_exec(codex.calls)["cmd"])


def test_advisory_turns_connectors_off_without_listing(codex, monkeypatch, tmp_path):
    """Advisory already ignores user config; the connectors are account-bound."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt"}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    adapters.ask_codex("review", mode="advisory")
    assert not [c for c in codex.calls if c["cmd"][1:3] == ["mcp", "list"]]
    argv = _before_prompt(_exec(codex.calls)["cmd"])
    assert ("-c", "features.apps=false") in _pairs(argv)
    assert ("-c", "features.plugins=false") in _pairs(argv)
    assert argv.count("--ignore-user-config") == 1
