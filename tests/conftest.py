"""Shared test setup.

Lane derivation reads the ambient environment, and the environment differs
between a developer machine (inside a Claude Code session, so
CLAUDE_CODE_SESSION_ID is set) and CI (not). Without this, the same test
would exercise the lane-qualified path locally and the unqualified path in
CI - green in both while covering neither on purpose.

Default every test to the unqualified case; the lane tests opt in explicitly.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Isolate collection, subprocesses, and workers that outlive test fixtures.
_store = tempfile.TemporaryDirectory(prefix="hardline-tests-")
_previous_db = os.environ.get("HARDLINE_DB")
os.environ["HARDLINE_DB"] = str(Path(_store.name) / "mailbox.db")

from hardline_mcp import mailbox

_previous_default = mailbox._DEFAULT_PATH
mailbox._DEFAULT_PATH = Path(os.environ["HARDLINE_DB"])

_LANE_ENV = (
    "HARDLINE_AGENT_LABEL",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_PROJECT_DIR",
)


def pytest_unconfigure(config):
    """Retain isolation until workers stop, including on collection errors."""
    server = sys.modules.get("hardline_mcp.server")
    if server is not None:
        server._async_executor.shutdown(wait=True)
    mailbox._DEFAULT_PATH = _previous_default
    if _previous_db is None:
        os.environ.pop("HARDLINE_DB", None)
    else:
        os.environ["HARDLINE_DB"] = _previous_db
    _store.cleanup()


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
    from hardline_mcp import adapters

    adapters.reset_claimed_lanes()
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
