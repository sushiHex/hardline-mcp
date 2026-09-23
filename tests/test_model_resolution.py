"""Codex family names ("astra") resolve against Codex's own catalog.

``_run_cmd`` is replaced wholesale: one fake answers both the catalog lookup
and the ``codex exec`` it precedes, routed by argv, so no real process runs.
"""

import json
from pathlib import Path

import pytest

from hardline_mcp import adapters


def _model(slug, visibility="list", upgrade=None):
    return {"slug": slug, "visibility": visibility, "upgrade": upgrade}


# Shaped after the refreshed catalog from codex-cli 0.155.1 (2026-09-23).
CATALOG = [
    _model("gpt-6-astra"),
    _model("gpt-6-sol"),
    _model("gpt-6-luna"),
    _model("gpt-reserve", visibility="hide"),
    _model("gpt-5.6-sol"),
    _model("gpt-5.6-terra"),
    _model("gpt-5.6-luna"),
    _model("gpt-5.5", upgrade={"model": "gpt-5.6-sol"}),
    _model("codex-auto-review", visibility="hide"),
]

_EXEC_OK = "\n".join(
    json.dumps(event)
    for event in (
        {"type": "thread.started", "thread_id": "t-1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {}},
    )
)


class FakeCodex:
    """Routes each spawn by argv; ``catalogs`` are served in turn, last repeats."""

    def __init__(self):
        self.catalogs = [CATALOG]
        self.catalog_result = None
        self.exec_result = {"ok": True, "reply": _EXEC_OK}
        self.calls = []

    def run(self, argv, **kwargs):
        call = {"argv": argv, **kwargs}
        self.calls.append(call)
        if argv[1:3] == ["debug", "models"]:
            env = kwargs.get("env") or {}
            call["home_existed"] = Path(env.get("CODEX_HOME", "")).is_dir()
            if self.catalog_result is not None:
                return self.catalog_result
            catalog = self.catalogs[min(len(self.catalog_calls) - 1, len(self.catalogs) - 1)]
            return {"ok": True, "reply": json.dumps({"models": catalog})}
        return dict(self.exec_result)

    @property
    def catalog_calls(self):
        return [c for c in self.calls if c["argv"][1:3] == ["debug", "models"]]

    @property
    def exec_calls(self):
        return [c for c in self.calls if "exec" in c["argv"]]

    def model_arg(self):
        argv = self.exec_calls[-1]["argv"]
        return argv[argv.index("--model") + 1] if "--model" in argv else None


@pytest.fixture
def codex(monkeypatch):
    fake = FakeCodex()
    monkeypatch.setenv("HARDLINE_CODEX_CMD", "codex-under-test")
    monkeypatch.setattr(adapters, "_run_cmd", fake.run)
    return fake


@pytest.fixture
def chatgpt_home(monkeypatch, tmp_path):
    home = tmp_path / "user-codex-home"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt"}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def test_family_resolves_to_its_newest_generation(codex):
    out = adapters.ask_codex("review", model="sol")

    assert out["ok"] is True
    assert codex.model_arg() == "gpt-6-sol"
    assert out["requested_model"] == "gpt-6-sol"
    assert out["model_resolution"] == {
        "requested": "sol",
        "resolved": "gpt-6-sol",
        "considered": ["gpt-6-sol", "gpt-5.6-sol"],
        "source": "codex debug models",
    }


def test_catalog_is_the_refreshed_one_from_the_launched_codex(codex, tmp_path):
    # --bundled is the binary's shipped list: measured stale and account-blind.
    adapters.ask_codex("review", model="astra", workdir=str(tmp_path))

    (lookup,) = codex.catalog_calls
    assert lookup["argv"] == ["codex-under-test", "debug", "models"]
    assert lookup.get("cwd") == str(tmp_path.resolve())
    assert lookup.get("timeout_s") == adapters._CODEX_CATALOG_TIMEOUT_S
    assert codex.model_arg() == "gpt-6-astra"


def test_family_is_the_final_segment_not_a_substring(codex):
    codex.catalogs = [CATALOG + [_model("gpt-7-solar")]]

    adapters.ask_codex("review", model="sol")

    assert codex.model_arg() == "gpt-6-sol"


@pytest.mark.parametrize(
    "other",
    [
        "gpt-7-astra-mini",  # a variant is another model
        "gpt-6-astra-2026-01-01",  # a dated snapshot must not rank as generation 2026
        "gpt-6o-astra-2025-01-01",
        "astra-preview",  # no generation at all
        "vendor-astra-2024",
    ],
)
def test_only_prefix_generation_family_slugs_qualify(codex, other):
    codex.catalogs = [[_model("gpt-6-astra"), _model(other)]]

    out = adapters.ask_codex("review", model="astra")

    assert codex.model_arg() == "gpt-6-astra"
    assert out["model_resolution"]["considered"] == ["gpt-6-astra"]


def test_generations_compare_numerically(codex):
    codex.catalogs = [[_model("gpt-5.9-luna"), _model("gpt-5.10-luna")]]

    adapters.ask_codex("review", model="luna")

    assert codex.model_arg() == "gpt-5.10-luna"


@pytest.mark.parametrize("visibility", ["hide", None, "none", "internal"])
def test_only_listed_models_are_offered(codex, visibility):
    newer = _model("gpt-7-astra", visibility=visibility)
    if visibility is None:
        del newer["visibility"]
    codex.catalogs = [CATALOG + [newer]]

    adapters.ask_codex("review", model="astra")

    assert codex.model_arg() == "gpt-6-astra"


@pytest.mark.parametrize("upgrade", [{"model": "gpt-6-sol"}, {}, False, ""])
def test_any_upgrade_marks_a_model_retiring(codex, upgrade):
    codex.catalogs = [CATALOG + [_model("gpt-7-sol", upgrade=upgrade)]]

    out = adapters.ask_codex("review", model="sol")

    assert codex.model_arg() == "gpt-6-sol"
    assert "gpt-7-sol" not in out["model_resolution"]["considered"]


@pytest.mark.parametrize(
    "family,catalog,reason",
    [
        ("astra", [_model("gpt-6-astra"), _model("gpt-6.0-astra")], "ambiguous"),
        ("astra", [_model("gpt-6-astra"), _model("o-7-astra")], "spans prefixes"),
        ("gpt", CATALOG, "no current listed"),  # a prefix, not a family
        ("nova", [_model("gpt-5.5-nova", upgrade={"model": "gpt-6-sol"})], "no current listed"),
    ],
)
def test_an_unresolvable_family_passes_through_literally(codex, family, catalog, reason):
    codex.catalogs = [catalog]

    out = adapters.ask_codex("review", model=family)

    # Exactly the pre-resolution behaviour: Codex judges the literal.
    assert codex.model_arg() == family
    assert out["model_resolution"]["resolved"] is None
    assert reason in out["model_resolution"]["reason"]


def test_unknown_family_names_the_known_families(codex):
    out = adapters.ask_codex("review", model="nebula")

    assert codex.model_arg() == "nebula"
    reason = out["model_resolution"]["reason"]
    assert "'astra'" in reason and "'terra'" in reason
    assert "reserve" not in reason


def test_unavailable_catalog_passes_through_literally(codex):
    codex.catalog_result = {"ok": False, "error": "exit 1: not signed in"}

    out = adapters.ask_codex("review", model="astra")

    assert codex.model_arg() == "astra"
    assert out["model_resolution"]["resolved"] is None
    assert "not signed in" in out["model_resolution"]["reason"]


@pytest.mark.parametrize("reply", ["not json", "[]", json.dumps({"models": "x"})])
def test_unreadable_catalog_passes_through_literally(codex, reply):
    codex.catalog_result = {"ok": True, "reply": reply}

    out = adapters.ask_codex("review", model="astra")

    assert codex.model_arg() == "astra"
    assert "catalog" in out["model_resolution"]["reason"]


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-5.6-sol", "o3"])
def test_identifiers_pass_through_without_a_lookup(codex, model):
    codex.catalogs = [CATALOG + [_model("o3")]]

    out = adapters.ask_codex("review", model=model)

    assert codex.model_arg() == model
    assert "model_resolution" not in out
    # Only a letters-only name costs a lookup; "o3" carries a digit.
    assert codex.catalog_calls == []


def test_a_letters_only_identifier_the_catalog_lists_is_kept(codex):
    codex.catalogs = [CATALOG + [_model("nova"), _model("gpt-6-nova")]]

    out = adapters.ask_codex("review", model="nova")

    assert codex.model_arg() == "nova"
    assert "model_resolution" not in out


def test_resolve_model_false_skips_the_lookup(codex):
    out = adapters.ask_codex("review", model="astra", resolve_model=False)

    assert codex.catalog_calls == []
    assert codex.model_arg() == "astra"
    assert "model_resolution" not in out


def test_family_names_are_case_insensitive(codex):
    out = adapters.ask_codex("review", model="Astra")

    assert codex.model_arg() == "gpt-6-astra"
    assert out["model_resolution"]["requested"] == "Astra"


def test_resolution_is_reported_when_codex_rejects_the_model(codex):
    codex.exec_result = {"ok": False, "error": "exit 1: model not available"}

    out = adapters.ask_codex("review", model="astra")

    assert out["ok"] is False
    assert out.get("model_resolution", {}).get("resolved") == "gpt-6-astra"


def test_lookup_strips_what_every_spawned_codex_loses(codex, monkeypatch):
    for name in adapters._AGENT_CHILD_STRIPPED_ENV:
        monkeypatch.setenv(name, "inherited")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    adapters.resolve_codex_model("astra")

    env = codex.catalog_calls[0].get("env", {})
    assert not adapters._AGENT_CHILD_STRIPPED_ENV & set(env)
    # Default mode runs with the operator's own provider settings, as exec does.
    assert env.get("OPENAI_API_KEY") == "sk-test"


def test_advisory_lookup_runs_in_an_isolated_home(codex, monkeypatch, chatgpt_home):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("HARDLINE_ALLOW_WRITE", "1")

    model, resolution = adapters.resolve_codex_model("astra", mode="advisory")

    assert model == "gpt-6-astra" and resolution["resolved"] == "gpt-6-astra"
    (lookup,) = codex.catalog_calls
    env = lookup.get("env", {})
    home = Path(env.get("CODEX_HOME", ""))
    # User config can supply its own catalog; advisory never loads it.
    assert home != chatgpt_home and home.name == "codex-home"
    assert lookup["home_existed"] is True
    assert lookup.get("cwd") == str(home.parent / "workspace")
    assert not home.exists()  # removed once the lookup is done
    assert "OPENAI_API_KEY" not in env and "HARDLINE_ALLOW_WRITE" not in env


def test_advisory_lookup_without_chatgpt_auth_passes_through(codex, monkeypatch, tmp_path):
    home = tmp_path / "api-home"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"auth_mode": "apikey"}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))

    model, resolution = adapters.resolve_codex_model("astra", mode="advisory")

    assert model == "astra"
    assert "ChatGPT" in resolution["reason"]
    assert codex.catalog_calls == []


def test_omitted_model_needs_no_lookup(codex):
    adapters.ask_codex("review", effort="high")

    assert codex.catalog_calls == []
    assert "--model" not in codex.exec_calls[0]["argv"]


def test_is_codex_family_matches_the_lookup_trigger():
    assert adapters.is_codex_family("astra") and adapters.is_codex_family("Sol")
    assert not any(
        adapters.is_codex_family(m) for m in (None, "", "gpt-6-astra", "o3", "gpt-5.5")
    )
