"""Codex's own sandbox, live: does hardline's pin keep a reviewer out of the
secrets on this platform?

Opt in with HARDLINE_LIVE_SANDBOX=1; needs `codex` on PATH. No model and no
login: `codex sandbox` runs one command under exactly the overrides
`ask_codex` would pass, preflight included. Every secret is a dummy file in a
temporary home, so nothing real is ever read.
"""

import os
import subprocess
import sys

import pytest

from hardline_mcp import adapters

pytestmark = pytest.mark.skipif(
    os.environ.get("HARDLINE_LIVE_SANDBOX") != "1",
    reason="live Codex sandbox; opt in with HARDLINE_LIVE_SANDBOX=1",
)

# Captured before conftest replaces it with a stub for every other test.
_started = adapters._codex_sandbox_started

# Prints each file's text, or that it could not be read; one process, so one
# sandbox start covers every path.
_READ = (
    "import sys\n"
    "for p in sys.argv[1:]:\n"
    "    try:\n"
    "        print(p, open(p, encoding='utf-8').read().strip())\n"
    "    except OSError as e:\n"
    "        print(p, 'UNREADABLE', type(e).__name__)\n"
)


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A home whose every secret is a dummy, and a project beside them."""
    home = tmp_path / "home"
    monkeypatch.setattr(adapters.Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("GH_CONFIG_DIR", str(home / "gh"))
    monkeypatch.setenv("HARDLINE_DB", str(home / "store" / "mb.db"))
    for path, text in {
        ".claude/.credentials.json": "DUMMY-CLAUDE",
        ".claude.json": "DUMMY-CLAUDE-JSON",
        ".codex/auth.json": "DUMMY-CODEX",
        "gh/hosts.yml": "DUMMY-GH",
        "store/mb.db": "DUMMY-STORE",
        ".ssh/id_ed25519": "DUMMY-SSH",
        ".claude/skills/SKILL.md": "SKILL-TEXT",
        "project/README.md": "PROJECT-TEXT",
    }.items():
        (home / path).parent.mkdir(parents=True, exist_ok=True)
        (home / path).write_text(text + "\n", encoding="utf-8")
    return home


def _pinned(cwd, write=False):
    """The overrides `ask_codex` would pass for a call in ``cwd``."""
    sandbox = adapters._codex_sandbox()
    assert sandbox.profile, "no permission profile on this platform"
    exe = adapters._prefix_for("codex")[0]
    overrides = sandbox.overrides + adapters._codex_pin(sandbox, write, dict(os.environ), str(cwd))
    return exe, overrides


def _run(cwd, *paths, write=False):
    exe, overrides = _pinned(cwd, write)
    started = _started(exe, overrides, dict(os.environ), str(cwd))
    assert started.get("ok"), started
    out = subprocess.run(
        [exe, "sandbox", *overrides, "--", sys.executable, "-I", "-c", _READ, *map(str, paths)],
        cwd=cwd, env=dict(os.environ), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    return out.stdout + out.stderr


def test_the_read_only_pin_denies_every_secret_and_nothing_else(home):
    secrets = [
        home / ".claude" / ".credentials.json", home / ".claude.json",
        home / ".codex" / "auth.json", home / "gh" / "hosts.yml",
        home / "store" / "mb.db", home / ".ssh" / "id_ed25519",
    ]
    out = _run(home / "project", *secrets, home / "project" / "README.md")
    assert "DUMMY-" not in out, out
    assert "PROJECT-TEXT" in out, out  # the sandbox ran, and read what it may


def test_the_read_only_pin_refuses_a_write(home):
    project = home / "project"
    exe, overrides = _pinned(project)
    out = subprocess.run(
        [exe, "sandbox", *overrides, "--", sys.executable, "-I", "-c",
         "try:\n    open('written.txt', 'w').write('x')\n    print('WROTE')\n"
         "except OSError:\n    print('REFUSED')\n"],
        cwd=project, env=dict(os.environ), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert "REFUSED" in out.stdout, out.stdout + out.stderr  # it ran, and was refused
    assert not (project / "written.txt").exists()


def test_a_workdir_inside_a_denied_directory(home):
    """Its own read entry is more specific than the denial. MXC and Seatbelt
    honour it; bubblewrap hides it (only writable paths are remounted inside a
    denied directory), so on Linux the preflight cannot start there and
    `ask_codex` refuses the call instead of reviewing nothing."""
    skills = home / ".claude" / "skills"
    exe, overrides = _pinned(skills)
    started = _started(exe, overrides, dict(os.environ), str(skills))
    if sys.platform.startswith("linux"):
        assert not started.get("ok"), started
        return
    out = _run(skills, skills / "SKILL.md", home / ".claude" / ".credentials.json")
    assert "SKILL-TEXT" in out and "DUMMY-" not in out, out
