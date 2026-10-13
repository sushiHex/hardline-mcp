"""Run named regression mutations in disposable source copies.

    python scripts/mutate.py [case ...] [--jobs N]

Every anchor is checked first. One baseline run over every selected test must
execute and pass each case's selection; then each mutation runs its tests in a
worker's own copy of the tree, and must turn them into assertion failures.
Stale anchors, empty/skipped selections, collection failures, and broken
imports are errors.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

_CAUGHT = ("assert ", "AssertionError", "Failed: DID NOT RAISE")


def _test(root: Path, selectors: list[str], timeout: int = 120) -> tuple[int, dict, str]:
    report = root / "mutation-results.xml"
    report.unlink(missing_ok=True)
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            f"--rootdir={root}",  # junit class names follow the selectors' paths
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
        timeout=timeout,
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


def _selects(selector: str, key: tuple[str, str]) -> bool:
    """Whether junit ``key`` is a test that ``selector`` (``path::test`` or
    ``path::test[param]``) runs; a bare path selects nothing by design."""
    path, _, name = selector.partition("::")
    module = path.removesuffix(".py").replace("/", ".")
    return bool(name) and key[0] == module and (
        key[1] == name or ("[" not in name and key[1].startswith(name + "["))
    )


@contextlib.contextmanager
def _tree(source: Path):
    """A disposable copy of the source tree."""
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
        yield root


def _validate(source: Path, case: dict) -> None:
    path = (source / case["file"]).resolve()
    if source.resolve() not in path.parents:
        raise ValueError("mutation target must stay inside the source copy")
    if not case["before"] or path.read_text(encoding="utf-8").count(case["before"]) != 1:
        raise ValueError("stale or ambiguous mutation anchor")
    if case["before"] == case["after"]:
        raise ValueError("mutation must change the source")


def _baseline(root: Path, cases: dict[str, dict]) -> tuple[dict[str, set], dict[str, str]]:
    """One run over every selected test: per case, the tests its selection
    ran, or why it cannot be a baseline."""
    selectors = sorted({s for case in cases.values() for s in case["tests"]})
    _, results, output = _test(root, selectors, timeout=1800)
    keys, errors = {}, {}
    for name, case in cases.items():
        ran = {key for key in results for s in case["tests"] if _selects(s, key)}
        empty = [s for s in case["tests"] if not any(_selects(s, key) for key in ran)]
        broken = sorted(f"{c}::{t}" for c, t in ran if results[(c, t)])
        if empty or broken:
            errors[name] = (
                "baseline must execute and pass every selected test"
                f" (ran nothing: {empty}; did not pass: {broken})\n{output[-3000:]}"
            )
        else:
            keys[name] = ran
    return keys, errors


def _mutate(root: Path, case: dict, expected: set) -> None:
    path = root / case["file"]
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace(case["before"], case["after"]), encoding="utf-8")
    try:
        code, mutated, output = _test(root, case["tests"])
    finally:
        path.write_text(original, encoding="utf-8")
    outcomes = [item for items in mutated.values() for item in items]
    if (
        code != 1
        or mutated.keys() != expected
        or not outcomes
        or any(tag != "failure" or not message.startswith(_CAUGHT) for tag, message in outcomes)
    ):
        raise RuntimeError(f"mutation was not caught by an assertion\n{output}")


def check(source: Path, case: dict) -> None:
    """One case end to end, raising ValueError or RuntimeError if it fails."""
    _validate(source, case)
    with _tree(source) as root:
        keys, errors = _baseline(root, {"case": case})
        if errors:
            raise RuntimeError(errors["case"])
        _mutate(root, case, keys["case"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", nargs="*")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    catalog = json.loads((source / "tests/mutations.json").read_text(encoding="utf-8"))
    for name in args.cases:
        if name not in catalog:
            parser.error(f"unknown mutation: {name}")
    selected = {name: catalog[name] for name in args.cases or catalog}
    started = time.monotonic()
    failures = {}
    for name, case in selected.items():
        try:
            _validate(source, case)
        except ValueError as exc:
            failures[name] = str(exc)
    runnable = {n: c for n, c in selected.items() if n not in failures}
    jobs = max(1, min(args.jobs, len(runnable) or 1))
    with contextlib.ExitStack() as stack:
        trees = queue.Queue()
        for _ in range(jobs):
            trees.put(stack.enter_context(_tree(source)))
        root = trees.get()
        try:
            keys, errors = _baseline(root, runnable)
        except subprocess.TimeoutExpired as exc:
            keys, errors = {}, dict.fromkeys(runnable, f"baseline timed out: {exc}")
        trees.put(root)
        failures.update(errors)

        def run(name: str) -> None:
            tree = trees.get()
            try:
                _mutate(tree, runnable[name], keys[name])
            finally:
                trees.put(tree)

        with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
            futures = {pool.submit(run, name): name for name in keys}
            for future in concurrent.futures.as_completed(futures):
                name = futures[future]
                try:
                    future.result()
                except (RuntimeError, subprocess.TimeoutExpired) as exc:
                    failures[name] = str(exc)
                else:
                    print(f"CAUGHT {name}", flush=True)
    for name in sorted(failures):
        print(f"ERROR {name}: {failures[name]}", flush=True)
    print(
        f"{len(selected) - len(failures)} caught, {len(failures)} not, of {len(selected)}"
        f" in {time.monotonic() - started:.0f}s with {jobs} jobs",
        flush=True,
    )
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
