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
        ".codex/state_5.sqlite": "DUMMY-CODEX-STATE",
        ".codex/AGENTS.md": "CODEX-AGENTS-TEXT",
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
        home / ".codex" / "auth.json", home / ".codex" / "state_5.sqlite",
        home / "gh" / "hosts.yml", home / "store" / "mb.db", home / ".ssh" / "id_ed25519",
    ]
    readable = [home / "project" / "README.md", home / ".codex" / "AGENTS.md"]
    out = _run(home / "project", *secrets, *readable)
    assert "DUMMY-" not in out, out
    # The sandbox ran, and read what it may: the project, Codex's instructions.
    assert "PROJECT-TEXT" in out and "CODEX-AGENTS-TEXT" in out, out


def test_the_preflight_catches_a_policy_without_the_denials(home):
    """What a managed requirements file that rejects hardline's profile would
    leave: the sandbox, but none of the denials."""
    sandbox = adapters._codex_sandbox()
    exe = adapters._prefix_for("codex")[0]
    started = _started(exe, sandbox.overrides, dict(os.environ), str(home / "project"))
    assert not started.get("ok") and "denied paths still open" in started["error"], started


def _command(cwd, code):
    exe, overrides = _pinned(cwd)
    out = subprocess.run(
        [exe, "sandbox", *overrides, "--", sys.executable, "-I", "-c", code],
        cwd=cwd, env=dict(os.environ), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    return out.stdout + out.stderr


def test_the_pin_keeps_credentials_out_of_commands(home, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "DUMMY-GH-TOKEN")
    monkeypatch.setenv("HARDLINE_LIVE_PLAIN", "PLAIN-TEXT")
    out = _command(home / "project", "import os; print(os.environ.get('GH_TOKEN'), "
                                     "os.environ.get('HARDLINE_LIVE_PLAIN'))")
    assert "DUMMY-GH-TOKEN" not in out and "PLAIN-TEXT" in out, out


@pytest.mark.skipif(sys.platform == "win32", reason="no /proc or ps -E; measured on a Windows 11 "
                    "host: a command sees only its own job's processes")
def test_a_command_cannot_read_codex_s_own_environment(home, monkeypatch):
    """Codex keeps the credentials its commands lose; that is only a boundary
    if no command can read them back out of Codex's process. The same routes
    must find the command's own variable, or "sealed" means "looked nowhere".
    Neither value appears in the command line, which `ps` also shows: the
    token is spelled backwards, and the plain value is read at run time."""
    monkeypatch.setenv("GH_TOKEN", "DUMMY-GH-TOKEN")
    monkeypatch.setenv("HARDLINE_LIVE_PLAIN", "PLAIN-ROUTE")
    out = _command(home / "project", (
        "import glob, os, subprocess\n"
        "token, plain = 'NEKOT-HG-YMMUD'[::-1], os.environ['HARDLINE_LIVE_PLAIN']\n"
        "seen = []\n"
        "for path in glob.glob('/proc/[0-9]*/environ'):\n"
        "    try:\n"
        "        seen.append(open(path, 'rb').read().decode('latin-1'))\n"
        "    except OSError:\n"
        "        pass\n"
        "try:\n"
        "    seen.append(subprocess.run(['ps', '-E', '-ww', '-A', '-o', 'command='],\n"
        "                               capture_output=True, text=True).stdout)\n"
        "except OSError:\n"
        "    pass\n"
        "print('LEAKED' if any(token in s for s in seen) else 'SEALED',\n"
        "      'route' if any(plain in s for s in seen) else 'no-route')\n"
    ))
    assert "SEALED route" in out, out


def test_the_preflight_catches_a_policy_that_passes_credentials(home):
    """What a layer turning Codex's default excludes back off would leave."""
    exe, overrides = _pinned(home / "project")
    at = overrides.index("shell_environment_policy.ignore_default_excludes=false")
    started = _started(exe, overrides[:at - 1] + overrides[at + 1:], dict(os.environ),
                       str(home / "project"))
    assert not started.get("ok") and "still reach its commands" in started["error"], started


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
    """Its own read entry is more specific than the denial: MXC and Seatbelt
    read the workdir and nothing else under ~/.claude. bubblewrap leaves it
    unreadable, which the preflight reports instead of a review of nothing."""
    skills = home / ".claude" / "skills"
    if sys.platform.startswith("linux"):
        exe, overrides = _pinned(skills)
        started = _started(exe, overrides, dict(os.environ), str(skills))
        assert not started.get("ok") and "cannot read the workdir" in started["error"], started
        return
    out = _run(skills, skills / "SKILL.md", home / ".claude" / ".credentials.json")
    assert "SKILL-TEXT" in out and "DUMMY-" not in out, out
