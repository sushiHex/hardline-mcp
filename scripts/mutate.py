"""Run named regression mutations in disposable source copies.

    python scripts/mutate.py [case ...]

A passing baseline and an assertion failure are required. Stale anchors,
empty/skipped selections, collection failures, and broken imports are errors.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


def _test(root: Path, selectors: list[str]) -> tuple[int, dict, str]:
    report = root / "mutation-results.xml"
    report.unlink(missing_ok=True)
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            f"--junitxml={report}",
            *selectors,
        ],
        cwd=root,
        env={
            **os.environ,
            "PYTHONPATH": str(root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_ADDOPTS": "",
            "HARDLINE_DB": str(root / "mutation.db"),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    cases = {}
    if report.exists():
        for case in ET.parse(report).iter("testcase"):
            key = (case.get("classname"), case.get("name"))
            cases[key] = [
                (child.tag, child.get("message", ""))
                for child in case
                if child.tag in {"failure", "error", "skipped"}
            ]
    return run.returncode, cases, run.stdout + run.stderr


def check(source: Path, case: dict) -> None:
    with tempfile.TemporaryDirectory(prefix="hardline-mutation-") as directory:
        root = Path(directory)
        for name in ("hardline_mcp", "tests", "scripts"):
            if (source / name).exists():
                shutil.copytree(
                    source / name,
                    root / name,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
        for name in ("pyproject.toml", "README.md", "LICENSE"):
            if (source / name).exists():
                shutil.copy2(source / name, root / name)
        path = (root / case["file"]).resolve()
        if root.resolve() not in path.parents:
            raise ValueError("mutation target must stay inside the source copy")
        original = path.read_text(encoding="utf-8")
        if not case["before"] or original.count(case["before"]) != 1:
            raise ValueError("stale or ambiguous mutation anchor")
        if case["before"] == case["after"]:
            raise ValueError("mutation must change the source")
        code, baseline, output = _test(root, case["tests"])
        if code != 0 or not baseline or any(baseline.values()):
            raise RuntimeError(
                f"baseline must execute and pass every selected test\n{output}"
            )
        path.write_text(
            original.replace(case["before"], case["after"]), encoding="utf-8"
        )
        code, mutated, output = _test(root, case["tests"])
        outcomes = [item for items in mutated.values() for item in items]
        if (
            code != 1
            or mutated.keys() != baseline.keys()
            or not outcomes
            or any(
                tag != "failure"
                or not message.startswith(
                    ("assert ", "AssertionError", "Failed: DID NOT RAISE")
                )
                for tag, message in outcomes
            )
        ):
            raise RuntimeError(f"mutation was not caught by an assertion\n{output}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", nargs="*")
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    cases = json.loads((source / "tests/mutations.json").read_text(encoding="utf-8"))
    selected = args.cases or list(cases)
    for name in selected:
        if name not in cases:
            parser.error(f"unknown mutation: {name}")
    failures = 0
    for name in selected:
        try:
            check(source, cases[name])
        except (ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            print(f"ERROR {name}: {exc}", flush=True)
            failures += 1
        else:
            print(f"CAUGHT {name}", flush=True)
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
