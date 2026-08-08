"""Shared test setup.

Lane derivation reads the ambient environment, and the environment differs
between a developer machine (inside a Claude Code session, so
CLAUDE_CODE_SESSION_ID is set) and CI (not). Without this, the same test
would exercise the lane-qualified path locally and the unqualified path in
CI - green in both while covering neither on purpose.

Default every test to the unqualified case; the lane tests opt in explicitly.
"""

import pytest

_LANE_ENV = (
    "HARDLINE_AGENT_LABEL",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_PROJECT_DIR",
)


@pytest.fixture(autouse=True)
def _no_ambient_lane(monkeypatch):
    for name in _LANE_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def in_session(monkeypatch):
    """Act as a Claude Code session: returns the lane suffix it produces."""
    monkeypatch.delenv("HARDLINE_AGENT_LABEL", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "1a2b3c4d-dead-beef-0000-000000000000")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "C:/Users/x/repos/fonts")
    return "fonts.1a2b3c4d"
