"""Behavioural tests that spawn a REAL `claude -p`.

The argv tests beside these prove list construction. The defect they cover was
behavioural - "read-only" denied the Edit/Write tools while Bash wrote the file
anyway - so argv assertions alone would still pass if the CLI changed how it
interprets a flag, if argument order made one ineffective, or if Bash found
another route. Construction and effect are different claims.

Skipped by default: these spawn a real authenticated CLI, cost quota, and take
tens of seconds. Opt in with HARDLINE_TEST_SPAWN=1.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from hardline_mcp import adapters

pytestmark = pytest.mark.skipif(
    os.environ.get("HARDLINE_TEST_SPAWN", "") not in {"1", "true", "yes"},
    reason="spawns a real claude CLI; set HARDLINE_TEST_SPAWN=1 to run",
)

_WRITE_PROMPT = (
    "Use the Bash tool to run exactly: echo hardline-probe > probe.txt\n"
    "Then reply with only the word DONE. Do not ask any questions."
)


def _claude_available() -> bool:
    try:
        return subprocess.run(
            [adapters._prefix_for("claude")[0], "--version"],
            capture_output=True, timeout=60, stdin=subprocess.DEVNULL,
        ).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def test_read_mode_does_not_write_through_bash(monkeypatch):
    """The regression itself: read mode wrote the file before this fix."""
    if not _claude_available():
        pytest.skip("claude CLI not runnable here")
    workdir = Path(tempfile.mkdtemp(prefix="hardline-spawn-ro-"))
    monkeypatch.chdir(workdir)

    out = adapters.ask_claude(_WRITE_PROMPT)

    assert out["ok"] is True, out
    assert not (workdir / "probe.txt").exists(), (
        "read mode wrote a file through Bash: " + (out.get("reply") or "")[:200]
    )


def test_codex_read_mode_does_not_write_in_a_trusted_project(monkeypatch, tmp_path):
    """Codex has a REAL sandbox, but hardline only asked for it on the advisory
    path - so the default path inherited the host's ~/.codex/config.toml. On a
    host with `[windows] sandbox = "elevated"` and the target project marked
    trust_level = "trusted", a default ask_codex call wrote the file.

    Needs a trusted project to be meaningful: in an untrusted directory codex
    refuses to run at all ("Not inside a trusted directory"), so the sandbox is
    never exercised. HARDLINE_TEST_TRUSTED_DIR names one.
    """
    trusted = os.environ.get("HARDLINE_TEST_TRUSTED_DIR", "")
    if not trusted or not Path(trusted).is_dir():
        pytest.skip("set HARDLINE_TEST_TRUSTED_DIR to a codex-trusted project")
    workdir = Path(trusted) / ".tmp" / "hardline-codex-probe"
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    try:
        out = adapters.ask_codex(_WRITE_PROMPT, workdir=str(workdir))
        assert not (workdir / "probe.txt").exists(), (
            "codex read mode wrote a file: " + str(out)[:200]
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_write_mode_still_writes(monkeypatch, tmp_path):
    """The other half - the fix must not have made write mode useless."""
    if not _claude_available():
        pytest.skip("claude CLI not runnable here")
    monkeypatch.setenv("HARDLINE_ALLOW_WRITE", "1")

    out = adapters.ask_claude(_WRITE_PROMPT, workdir=str(tmp_path), write=True)

    assert out["ok"] is True, out
    assert (tmp_path / "probe.txt").exists(), (
        "write mode failed to write: " + (out.get("reply") or "")[:200]
    )
