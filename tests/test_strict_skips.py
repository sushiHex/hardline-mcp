"""HARDLINE_STRICT_SKIPS, which CI sets: only a declared skip may pass."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_only_declared_skips_pass_under_strict_skips(tmp_path):
    (tmp_path / "conftest.py").write_text(
        "from tests.conftest import (\n"
        "    pytest_collectreport, pytest_runtest_makereport, pytest_sessionfinish)\n",
        encoding="utf-8",
    )
    (tmp_path / "test_sample.py").write_text(
        "import pytest\n"
        "@pytest.mark.skipif(True, reason='declared')\n"
        "def test_declared():\n    pass\n"
        "def test_decided_at_run_time():\n    pytest.skip('a probe stopped answering')\n"
        "@pytest.mark.xfail(strict=True)\n"
        "def test_expected_failure():\n    assert False\n",
        encoding="utf-8",
    )
    (tmp_path / "test_missing_extra.py").write_text(
        "import pytest\npytest.importorskip('no_such_extra_module')\n", encoding="utf-8"
    )
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfEs", str(tmp_path)],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONPATH": str(ROOT), "HARDLINE_STRICT_SKIPS": "1",
             "PYTEST_ADDOPTS": ""},
        timeout=120,
    )
    out = run.stdout + run.stderr
    assert run.returncode == 1, out  # the run fails, not just its report
    assert "skipped at run time under HARDLINE_STRICT_SKIPS: a probe stopped answering" in out, out
    assert "module skipped under HARDLINE_STRICT_SKIPS" in out, out
    summary = out.strip().splitlines()[-1]
    assert "1 failed" in summary and "1 skipped" in summary and "1 xfailed" in summary, out
    assert "1 error" in summary, out
