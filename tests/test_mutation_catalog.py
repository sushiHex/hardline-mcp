"""The mutation catalog stays applicable as the code moves (CLAUDE.md).

``scripts/mutate.py`` refuses a stale anchor, but only when the catalog runs:
minutes, in one CI job, after a push. These are the same checks in
milliseconds, on every platform, with the suite.
"""

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("mutate", ROOT / "scripts" / "mutate.py")
mutate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mutate)

CASES = mutate.load(ROOT)
KEYS = {"file", "before", "after", "tests"}


def stale(cases: dict) -> list[str]:
    """Cases ``mutate.py`` would refuse to apply, by its own rule."""
    found = []
    for name, case in cases.items():
        try:
            mutate._validate(ROOT, case)
        except (ValueError, OSError) as exc:
            found.append(f"{name}: {exc}")
    return found


def unselected(cases: dict) -> list[str]:
    """Selectors naming a test that no longer exists: a rename selects nothing."""
    found = []
    for name, case in cases.items():
        for selector in case["tests"]:
            path, _, test = selector.partition("::")
            function = test.split("[", 1)[0].split("::")[-1]
            try:
                source = (ROOT / path).read_text(encoding="utf-8")
            except OSError:
                source = ""
            if not re.search(rf"^\s*(async )?def {re.escape(function)}\(", source, re.M):
                found.append(f"{name}: {selector}")
    return found


def malformed(cases: dict) -> list[str]:
    """Cases without exactly the four keys, or with no test to run."""
    return [
        name
        for name, case in cases.items()
        if case.keys() != KEYS or not isinstance(case["tests"], list) or not case["tests"]
    ]


def test_every_case_has_the_four_keys_and_some_tests():
    assert malformed(CASES) == []


def test_case_names_are_safe_and_distinct_on_a_case_insensitive_disk():
    assert [n for n in CASES if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", n)] == []
    assert len({n.lower() for n in CASES}) == len(CASES)


def test_the_old_single_file_catalog_is_not_back():
    assert not (ROOT / "tests" / "mutations.json").exists()


def test_every_anchor_appears_exactly_once():
    # Every name in the message: pytest elides a long list's diff to its first.
    found = stale(CASES)
    assert found == [], "\n".join(found)


def test_every_case_names_tests_that_exist():
    found = unselected(CASES)
    assert found == [], "\n".join(found)


def test_the_checks_see_a_stale_anchor_and_a_renamed_test():
    case = {
        "file": "hardline_mcp/server.py",
        "before": "text the server never contained",
        "after": "",
        "tests": ["tests/test_server.py::test_renamed_long_ago[param]"],
    }
    assert stale({"x": case}) == ["x: stale or ambiguous mutation anchor"]
    assert unselected({"x": case}) == ["x: tests/test_server.py::test_renamed_long_ago[param]"]


def test_the_checks_see_a_misspelled_key_and_an_empty_selection():
    case = {"file": "f", "before": "b", "after": "a", "tests": ["t::x"]}
    assert malformed({"ok": case}) == []
    typo = {"file": "f", "before": "b", "after": "a", "test": ["t::x"]}
    assert malformed({"typo": typo}) == ["typo"]
    assert malformed({"empty": {**case, "tests": []}}) == ["empty"]
