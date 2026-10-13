"""Shared test setup.

Lane derivation reads the ambient environment, and the environment differs
between a developer machine (inside a Claude Code session, so
CLAUDE_CODE_SESSION_ID is set) and CI (not). Without this, the same test
would exercise the lane-qualified path locally and the unqualified path in
CI - green in both while covering neither on purpose.

Default every test to the unqualified case; the lane tests opt in explicitly.
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# No fsync per commit in this process. Tests need no durability against power
# loss, and on Windows CI runners each commit's fsync cost about 180 ms: one
# 260-job listing test took 37 s. Locking, WAL and atomicity, which the suite
# does exercise, are untouched. Patched at sqlite3.connect because the modules
# import mailbox._connect by name.
_sqlite_connect = sqlite3.connect


def _connect_without_fsync(*args, **kwargs):
    conn = _sqlite_connect(*args, **kwargs)
    conn.execute("PRAGMA synchronous=OFF")
    return conn


sqlite3.connect = _connect_without_fsync

# Isolate collection, subprocesses, and workers that outlive test fixtures.
_store = tempfile.TemporaryDirectory(prefix="hardline-tests-")
_previous_db = os.environ.get("HARDLINE_DB")
os.environ["HARDLINE_DB"] = str(Path(_store.name) / "mailbox.db")
# GitHub snapshots are private repository content: never the operator's cache.
_previous_snapshots = os.environ.get("HARDLINE_GITHUB_SNAPSHOT_DIR")
os.environ["HARDLINE_GITHUB_SNAPSHOT_DIR"] = str(Path(_store.name) / "github")

# Run inside a Claude Code session, the suite inherits that session's own
# inbox and the token that makes a message its own child's. A server under test
# - in process or spawned - would prove it the session's (its ancestors include
# that claude.exe) and post real notices into it. Never the operator's inbox.
_INBOX_ENV = ("CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN")
_previous_inbox = {name: os.environ.pop(name, None) for name in _INBOX_ENV}

from hardline_mcp import mailbox

_previous_default = mailbox._DEFAULT_PATH
mailbox._DEFAULT_PATH = Path(os.environ["HARDLINE_DB"])

_LANE_ENV = (
    "HARDLINE_AGENT_LABEL",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_PROJECT_DIR",
)


# CI sets HARDLINE_STRICT_SKIPS=1. There a skip must be declared - a skip or
# skipif mark, as the opt-in live suites and platform-only tests have. A skip
# decided at run time (a missing extra, a probe that stopped answering) fails
# instead, since it would read exactly like a pass.
_STRICT_SKIPS = os.environ.get("HARDLINE_STRICT_SKIPS") == "1"


def _declared_skip(item) -> bool:
    return any(
        mark.name == "skip" or (mark.args and not isinstance(mark.args[0], str) and mark.args[0])
        for mark in item.iter_markers()
        if mark.name in ("skip", "skipif")
    )


_failed_skips: list[str] = []


def _fail_skip(report, what: str) -> None:
    reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else report.longrepr
    report.outcome = "failed"
    report.longrepr = f"{what} under HARDLINE_STRICT_SKIPS: {str(reason).removeprefix('Skipped: ')}"
    _failed_skips.append(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    """A report failed after the session counted it still fails the run."""
    if _failed_skips and exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    report = (yield).get_result()
    if (
        _STRICT_SKIPS
        and report.skipped
        and not hasattr(report, "wasxfail")
        and not _declared_skip(item)
    ):
        _fail_skip(report, "skipped at run time")


@pytest.hookimpl(tryfirst=True)  # before the terminal reporter files it as a skip
def pytest_collectreport(report):
    if _STRICT_SKIPS and report.skipped:
        _fail_skip(report, "module skipped")


def pytest_unconfigure(config):
    """Retain isolation until workers stop, including on collection errors."""
    server = sys.modules.get("hardline_mcp.server")
    if server is not None:
        server._async_executor.shutdown(wait=True)
    mailbox._DEFAULT_PATH = _previous_default
    for name, previous in (
        ("HARDLINE_DB", _previous_db),
        ("HARDLINE_GITHUB_SNAPSHOT_DIR", _previous_snapshots),
        *_previous_inbox.items(),
    ):
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous
    _store.cleanup()


@pytest.fixture
def flood():
    """Store messages as ``mailbox.send`` does, in one transaction.

    For the tests about volume - limits, clamps, paging - which need hundreds
    of messages, not hundreds of commits: a commit each cost about 17 ms on
    Windows, seconds per test.
    """
    import contextlib

    def store(db, bodies, *, sender="codex", recipient="hermes", now_fn=mailbox._default_now):
        with contextlib.closing(mailbox._connect(Path(db))) as conn, conn:
            conn.executemany(
                "INSERT INTO messages (sender, recipient, body, created_at) VALUES (?, ?, ?, ?)",
                [(sender, recipient, body, mailbox._iso(now_fn())) for body in bodies],
            )

    return store


@pytest.fixture(autouse=True)
def _no_ambient_lane(monkeypatch):
    for name in _LANE_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_leaked_claims():
    """Forget runtime lane claims between tests.

    A claim is process-local state, not an env var, so monkeypatch cannot undo
    it: without this a test that renames its session silently changes the
    identity of every test that runs afterwards, and the failures land
    somewhere else entirely.
    """
    from hardline_mcp import adapters, procid

    adapters.reset_claimed_lanes()
    procid._current_identity = None
    server = sys.modules.get("hardline_mcp.server")
    if server is not None:
        server._last_heartbeat.clear()
        server._registration_failure.clear()
    yield
    adapters.reset_claimed_lanes()


@pytest.fixture(autouse=True)
def _no_ambient_parent_lane(monkeypatch):
    """Default every test to a process with NO session identity.

    A lane now falls back to the process that spawned this one, which in
    production is the agent session and under pytest is whatever ran the tests.
    That makes the "no lane at all" case unreachable by accident, and it is the
    case most of these tests are about — so the same reasoning that clears the
    lane environment applies here: pin the baseline, and let the tests that
    care about parent derivation opt in.
    """
    from hardline_mcp import adapters

    monkeypatch.setattr(adapters, "_session_anchor", [{"lane": "", "agent": ""}])


# Modules that launch REAL agents when opted in. Their children must be
# isolated for real, so the probe stub below steps aside for them alone.
_LIVE_MODULES = {"test_live_agents", "test_live_watch", "test_spawn_behaviour"}


def _real_probes_for(module_name: str) -> bool:
    """By module, never by ambient flag: a live flag must not un-stub unit tests."""
    return module_name.rsplit(".", 1)[-1] in _LIVE_MODULES


@pytest.fixture(autouse=True)
def _no_codex_isolation_probes(monkeypatch, request):
    """Default every test to a Codex with the required features, no servers,
    and an MXC sandbox that starts commands.

    Every ask_codex asks the real CLI what to disable (#42). Left in place,
    that would run `codex features list` / `codex mcp list` from the test
    suite and shift every captured command. The isolation tests restore the
    real functions.
    """
    if _real_probes_for(request.module.__name__):
        return
    from hardline_mcp import adapters

    monkeypatch.setattr(
        adapters,
        "_codex_features",
        lambda exe, env, cwd, on_spawn=None: (
            frozenset(adapters._CODEX_REQUIRED_FEATURES),
            {"ok": True},
        ),
    )
    monkeypatch.setattr(
        adapters,
        "_codex_mcp_servers",
        lambda exe, env, cwd, on_spawn=None: ([], {"ok": True}),
    )
    monkeypatch.setattr(
        adapters,
        "_codex_sandbox_started",
        lambda exe, overrides, env, cwd, on_spawn=None: {"ok": True},
    )


@pytest.fixture
def spawned_by_codex(monkeypatch):
    """Act as a hardline a Codex terminal session spawned, telling us nothing.

    The real case: no environment identifies the session, so both the lane and
    the agent come from the process that started it.
    """
    from hardline_mcp import adapters

    monkeypatch.setattr(
        adapters,
        "_session_anchor",
        [{"lane": "construction.a1b2c3d4", "agent": "codex"}],
    )
    return "construction.a1b2c3d4"


@pytest.fixture
def in_session(monkeypatch):
    """Act as a Claude Code session: returns the lane suffix it produces."""
    monkeypatch.delenv("HARDLINE_AGENT_LABEL", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "1a2b3c4d-dead-beef-0000-000000000000")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "C:/Users/x/repos/fonts")
    return "fonts.1a2b3c4d"
