"""A spawned Codex reaches no MCP server and no app connector (#42).

Measured before the fix: a default ask_codex child called hardline's own
tools, had node_repl's browser, and searched the web through the `apps`
connector - all unattended, all outside its --sandbox. These tests drive the
real ``_codex_features`` and ``_codex_mcp_servers`` against fake processes, so
the questions asked of Codex and the `codex exec` that follows are checked
together.
"""

import json

import pytest

from hardline_mcp import adapters
from hardline_mcp.adapters import _codex_features as REAL_FEATURES
from hardline_mcp.adapters import _codex_mcp_servers as REAL_LISTING

CONNECTORS = ["-c", "features.apps=false", "-c", "features.plugins=false"]
FEATURES_OUT = "apps   stable  true\nplugins  stable  true\nhooks  stable  true\n"


class _Proc:
    pid = 424242
    stdout = stderr = stdin = None  # closed by the reaper; nothing to close here

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
    """Real probes; fake processes. Mutate the class attributes to vary Codex."""
    calls = []

    class State:
        servers = json.dumps([{"name": "hardline"}, {"name": "node_repl"}])
        list_exit = 0
        features = FEATURES_OUT

    def popen(cmd, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        if cmd[1:3] == ["mcp", "list"]:
            return _Proc(State.servers, State.list_exit)
        if cmd[1:3] == ["features", "list"]:
            return _Proc(State.features, 0)
        return _Proc("reply", 0)

    adapters._codex_features_cache.clear()
    monkeypatch.setattr(adapters, "_codex_features", REAL_FEATURES)
    monkeypatch.setattr(adapters, "_codex_mcp_servers", REAL_LISTING)
    monkeypatch.setattr(adapters.subprocess, "Popen", popen)
    monkeypatch.setattr(adapters, "_kill_tree", lambda proc: None)
    State.calls = calls
    yield State
    adapters._codex_features_cache.clear()


def _execs(calls):
    return [c for c in calls if "exec" in c["cmd"]]


def _listing(calls):
    (call,) = [c for c in calls if c["cmd"][1:3] == ["mcp", "list"]]
    return call


def _before_prompt(argv):
    return argv[: argv.index("--")]


def _overrides(argv):
    return [b for a, b in zip(argv, argv[1:]) if a == "-c"]


def _disabled_servers(argv):
    tables = [o for o in _overrides(argv) if o.startswith("mcp_servers=")]
    return tables


@pytest.fixture
def advisory_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt"}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # the plain fast path
        {"model": "gpt-5.6-sol"},  # telemetry path
        {"workdir": "."},
        {"write": True, "workdir": "."},
        {"mode": "advisory"},
    ],
    ids=["plain", "telemetry", "workdir", "write", "advisory"],
)
def test_every_codex_child_gets_no_connectors_and_no_servers(
    codex, monkeypatch, advisory_home, kwargs
):
    monkeypatch.setenv("HARDLINE_ALLOW_WRITE", "1")
    adapters.ask_codex("review this", **kwargs)
    (call,) = _execs(codex.calls)
    argv = _before_prompt(call["cmd"])
    overrides = _overrides(argv)
    assert "features.apps=false" in overrides
    assert "features.plugins=false" in overrides
    assert _disabled_servers(argv) == [
        'mcp_servers={"hardline"={enabled=false},"node_repl"={enabled=false}}'
    ]


def test_servers_are_listed_exactly_where_and_how_the_child_will_run(
    codex, monkeypatch, tmp_path
):
    monkeypatch.setenv("HARDLINE_ALLOW_WRITE", "1")
    adapters.ask_codex("review", workdir=str(tmp_path))
    listing, (child,) = _listing(codex.calls), _execs(codex.calls)
    assert listing["cmd"][3:] == ["--json", *CONNECTORS], (
        "listed with plugins and apps off, or it names servers no layer defines"
    )
    assert listing["kwargs"]["cwd"] == child["kwargs"]["cwd"]
    assert listing["kwargs"]["env"] == child["kwargs"]["env"], (
        "a different environment (CODEX_HOME, say) could list a different config"
    )


def test_advisory_lists_in_its_isolated_home(codex, advisory_home):
    """--ignore-user-config still loads system, cloud and project layers."""
    adapters.ask_codex("review", mode="advisory")
    listing, (child,) = _listing(codex.calls), _execs(codex.calls)
    assert listing["kwargs"]["env"]["CODEX_HOME"] == child["kwargs"]["env"]["CODEX_HOME"]
    assert listing["kwargs"]["cwd"] == child["kwargs"]["cwd"]


def test_any_server_name_is_addressed_by_a_quoted_key(codex):
    codex.servers = json.dumps([{"name": "team.server"}, {"name": 'odd"name'}])
    adapters.ask_codex("review")
    (child,) = _execs(codex.calls)
    assert _disabled_servers(_before_prompt(child["cmd"])) == [
        'mcp_servers={"team.server"={enabled=false},"odd\\"name"={enabled=false}}'
    ]


def test_no_servers_means_no_table(codex):
    codex.servers = "[]"
    adapters.ask_codex("review")
    (child,) = _execs(codex.calls)
    assert _disabled_servers(_before_prompt(child["cmd"])) == []


@pytest.mark.parametrize(
    "listing", ["not json", "{}", '""', '[{"title": "x"}]', '[{"name": 7}]'],
    ids=["garbage", "object", "string", "no-name", "non-string-name"],
)
def test_an_unprovable_listing_refuses_the_call(codex, listing):
    codex.servers = listing
    out = adapters.ask_codex("review")
    assert _execs(codex.calls) == [], "no child is started on a guess"
    assert out.get("isolation") == "refused" and out.get("ok") is False


def test_a_failed_listing_refuses_the_call(codex):
    codex.list_exit = 1
    out = adapters.ask_codex("review", model="gpt-5.6-sol")
    assert _execs(codex.calls) == [], "no child is started on a guess"
    assert out.get("isolation") == "refused" and out.get("ok") is False


def test_a_codex_without_the_features_refuses(codex):
    """features.<unknown>=false is silently ignored, so a rename must be caught."""
    codex.features = "hooks  stable  true\nplugins  stable  true\n"
    out = adapters.ask_codex("review")
    assert _execs(codex.calls) == []
    assert out.get("ok") is False and "apps" in out.get("error", "")


def test_a_cancel_during_the_listing_stops_the_call(codex):
    out = adapters.ask_codex("review", on_spawn=lambda pid: False)
    assert out.get("cancelled") is True
    assert _execs(codex.calls) == []
