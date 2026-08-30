"""Shared test setup.

Lane derivation reads the ambient environment, and the environment differs
between a developer machine (inside a Claude Code session, so
CLAUDE_CODE_SESSION_ID is set) and CI (not). Without this, the same test
would exercise the lane-qualified path locally and the unqualified path in
CI - green in both while covering neither on purpose.

Default every test to the unqualified case; the lane tests opt in explicitly.
"""

import sqlite3

import pytest

from hardline_mcp import mailbox

_LANE_ENV = (
    "HARDLINE_AGENT_LABEL",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_PROJECT_DIR",
)


def _live_row_count() -> int | None:
    """Rows in the OPERATOR's real mailbox, or None if it does not exist."""
    path = mailbox._DEFAULT_PATH
    if not path.exists():
        return None
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as conn:
            return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    except sqlite3.Error:
        return None


@pytest.fixture(scope="session", autouse=True)
def _never_write_the_real_mailbox():
    """Fail the run if the suite wrote to the operator's live mailbox.

    Patching ``mailbox._DEFAULT_PATH`` is not sufficient on its own: a test
    that dispatches through the REAL thread pool has its worker call
    ``mailbox.send`` after the test returns, by which point monkeypatch has
    restored the real path - so the delivery lands in production. That leak ran
    unnoticed from 2026-07-30 and put 71 stray ``{"reply": "eventually"}``
    messages into the live store, where they sat in an inbox nobody drained.

    SESSION scope, deliberately. A per-test version was written first and it
    MISSED this exact bug: the offending write happens on a pool thread after
    the test body returns, so a teardown check races the thing it is checking
    and usually wins. Comparing once around the whole run cannot race, at the
    cost of naming the run rather than the test - and a leak is rare enough
    that bisecting it afterwards is fine.

    Tests that legitimately spawn late-delivering workers must wait for the
    delivery before returning; this makes forgetting loud.
    """
    before = _live_row_count()
    yield
    # Drain the pool BEFORE comparing. Without this the guard races the very
    # write it exists to catch and loses: the offending delivery happens on a
    # worker thread that has not been joined yet, so both a per-test teardown
    # and a plain session teardown read the count too early and report clean.
    # Verified - the first two versions of this guard MISSED the leak.
    try:
        from hardline_mcp import server

        server._async_executor.shutdown(wait=True)
    except Exception:  # noqa: BLE001 - the guard must never break the run
        pass
    after = _live_row_count()
    if before is not None and after is not None and after != before:
        pytest.fail(
            f"the suite wrote to the OPERATOR'S real mailbox "
            f"({mailbox._DEFAULT_PATH}): {before} -> {after} rows. A worker most "
            "likely delivered after its test returned, once monkeypatch had "
            "restored _DEFAULT_PATH. Find the test that dispatches through the "
            "real executor and make it wait for delivery."
        )


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
    from hardline_mcp import adapters, server

    adapters.reset_claimed_lanes()
    server._reset_registry_state()
    yield
    adapters.reset_claimed_lanes()
    server._reset_registry_state()


@pytest.fixture
def in_session(monkeypatch):
    """Act as a Claude Code session: returns the lane suffix it produces."""
    monkeypatch.delenv("HARDLINE_AGENT_LABEL", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "1a2b3c4d-dead-beef-0000-000000000000")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "C:/Users/x/repos/fonts")
    return "fonts.1a2b3c4d"
