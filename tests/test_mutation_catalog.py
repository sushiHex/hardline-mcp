"""The mutation catalog stays applicable as the code moves (CLAUDE.md).

``scripts/mutate.py`` refuses a stale anchor, but only when the catalog runs:
minutes, in one CI job, after a push. These are the same checks in
milliseconds, on every platform, with the suite.
"""

import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES = json.loads((ROOT / "tests" / "mutations.json").read_text(encoding="utf-8"))

_spec = importlib.util.spec_from_file_location("mutate", ROOT / "scripts" / "mutate.py")
mutate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mutate)


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


def test_every_anchor_appears_exactly_once():
    assert stale(CASES) == []


def test_every_case_names_tests_that_exist():
    assert unselected(CASES) == []


def test_the_checks_see_a_stale_anchor_and_a_renamed_test():
    case = {
        "file": "hardline_mcp/server.py",
        "before": "text the server never contained",
        "after": "",
        "tests": ["tests/test_server.py::test_renamed_long_ago[param]"],
    }
    assert stale({"x": case}) == ["x: stale or ambiguous mutation anchor"]
    assert unselected({"x": case}) == ["x: tests/test_server.py::test_renamed_long_ago[param]"]
