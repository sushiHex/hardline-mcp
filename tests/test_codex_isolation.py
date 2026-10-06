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

    monkeypatch.setattr(adapters, "_codex_features", REAL_FEATURES)
    monkeypatch.setattr(adapters, "_codex_mcp_servers", REAL_LISTING)
    monkeypatch.setattr(adapters.subprocess, "Popen", popen)
    monkeypatch.setattr(adapters, "_kill_tree", lambda proc: None)
    State.calls = calls
    return State


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


def _features_probe(calls):
    (call,) = [c for c in calls if c["cmd"][1:3] == ["features", "list"]]
    return call


def test_codex_is_probed_exactly_where_and_how_the_child_will_run(
    codex, monkeypatch, tmp_path
):
    monkeypatch.setenv("HARDLINE_ALLOW_WRITE", "1")
    adapters.ask_codex("review", workdir=str(tmp_path))
    listing, probe = _listing(codex.calls), _features_probe(codex.calls)
    (child,) = _execs(codex.calls)
    assert listing["cmd"][3:] == ["--json", *CONNECTORS], (
        "listed with plugins and apps off, or it names servers no layer defines"
    )
    for question in (listing, probe):
        assert question["cmd"][0] == child["cmd"][0], "the probed executable is the one launched"
        assert question["kwargs"]["cwd"] == child["kwargs"]["cwd"]
        assert question["kwargs"]["env"] == child["kwargs"]["env"], (
            "a different environment (CODEX_HOME, say) could see a different config"
        )


def test_advisory_lists_in_its_isolated_home(codex, advisory_home):
    """--ignore-user-config still loads system, cloud and project layers."""
    adapters.ask_codex("review", mode="advisory")
    (child,) = _execs(codex.calls)
    for question in (_listing(codex.calls), _features_probe(codex.calls)):
        assert question["kwargs"]["env"]["CODEX_HOME"] == child["kwargs"]["env"]["CODEX_HOME"]
        assert question["kwargs"]["cwd"] == child["kwargs"]["cwd"]


def test_any_server_name_is_addressed_by_a_quoted_key(codex):
    names = ["team.server", 'odd"name', "back\\slash", "bell\x07", "del\x7f", "tab\tok", "emoji\U0001F600"]
    codex.servers = json.dumps([{"name": n} for n in names])
    adapters.ask_codex("review")
    (child,) = _execs(codex.calls)
    (table,) = _disabled_servers(_before_prompt(child["cmd"]))
    assert table == (
        'mcp_servers={"team.server"={enabled=false},"odd\\"name"={enabled=false},'
        '"back\\\\slash"={enabled=false},"bell\\u0007"={enabled=false},'
        '"del\\u007F"={enabled=false},"tab\tok"={enabled=false},'
        '"emoji\U0001F600"={enabled=false}}'
    ), "TOML basic strings: no surrogate-pair escapes, controls escaped, tab literal"
    try:
        import tomllib
    except ImportError:  # Python 3.10
        return
    parsed = tomllib.loads(table)["mcp_servers"]
    assert sorted(parsed) == sorted(names)


def test_a_name_toml_cannot_express_refuses(codex):
    codex.servers = json.dumps([{"name": "lone\ud800"}])
    out = adapters.ask_codex("review")
    assert _execs(codex.calls) == []
    assert out.get("isolation") == "refused"


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


@pytest.mark.parametrize(
    "features, missing",
    [
        ("hooks  stable  true\nplugins  stable  true\n", "apps"),
        ("apps  stable  true\nhooks  stable  true\n", "plugins"),
        ("apps  removed  false\nplugins  stable  true\n", "apps"),
        ("apps  under development  false\nplugins  removed  true\n", "plugins"),
    ],
    ids=["no-apps", "no-plugins", "apps-removed", "plugins-removed"],
)
def test_a_codex_without_the_features_refuses(codex, features, missing):
    """features.<unknown>=false is silently ignored, so a rename must be caught."""
    codex.features = features
    out = adapters.ask_codex("review")
    assert _execs(codex.calls) == []
    assert out.get("ok") is False and missing in out.get("error", "")


def test_the_executable_is_resolved_once_for_probes_and_launch(codex, monkeypatch):
    """A name or shim can resolve differently each time it is looked up."""
    lookups = iter(["codex-one", "codex-two", "codex-three", "codex-four"])
    monkeypatch.setattr(adapters, "_prefix_for", lambda agent: [next(lookups), "exec"])
    adapters.ask_codex("review")
    assert {c["cmd"][0] for c in codex.calls} == {"codex-one"}, (
        "the probed executable must be the launched one"
    )


def test_features_are_asked_fresh_on_every_call(codex):
    """No cache: an upgrade that drops a feature must refuse the very next call."""
    adapters.ask_codex("first")
    assert len(_execs(codex.calls)) == 1
    codex.features = "plugins  stable  true\n"
    out = adapters.ask_codex("second")
    assert len(_execs(codex.calls)) == 1, "the second call must not launch"
    assert out.get("isolation") == "refused"


def test_live_flags_never_unstub_unit_tests(monkeypatch):
    import conftest

    for flag in ("HARDLINE_LIVE_TESTS", "HARDLINE_LIVE_WATCH", "HARDLINE_TEST_SPAWN"):
        monkeypatch.setenv(flag, "1")
    assert not conftest._real_probes_for("tests.test_adapters")
    assert not conftest._real_probes_for("test_codex_isolation")
    assert conftest._real_probes_for("tests.test_live_agents")


@pytest.mark.parametrize("kwargs", [{}, {"model": "gpt-5.6-sol"}], ids=["plain", "telemetry"])
def test_an_invalid_timeout_fails_before_any_probe(codex, monkeypatch, kwargs):
    """docs/configuration.md: invalid timeouts fail before an agent is spawned."""
    monkeypatch.setenv("HARDLINE_CODEX_TIMEOUT_S", "not-a-number")
    out = adapters.ask_codex("review", **kwargs)
    assert codex.calls == [], "nothing - not even an isolation probe - may start"
    assert out.get("ok") is False and "HARDLINE_CODEX_TIMEOUT_S" in out.get("error", "")


def test_a_cancel_at_the_first_probe_stops_the_call(codex):
    out = adapters.ask_codex("review", on_spawn=lambda pid: False)
    assert out.get("cancelled") is True
    assert [c["cmd"][1:3] for c in codex.calls] == [["features", "list"]], (
        "nothing after a cancelled probe may start"
    )


def test_a_cancel_during_the_listing_stops_the_call(codex):
    claims = iter([True, False])  # the feature probe is claimed, the listing is not
    out = adapters.ask_codex("review", on_spawn=lambda pid: next(claims))
    assert out.get("cancelled") is True
    assert _execs(codex.calls) == []
