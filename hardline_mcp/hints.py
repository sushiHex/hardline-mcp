"""Names a conversation held before: advice for it after a restart, never ownership.

A claimed name lives in its process. When Claude Code relaunches a conversation,
or reconnects its MCP server, the new hardline holds the lane its environment
implies but not the names the conversation claimed, and mail sent to those
waits unread. This module remembers which conversation last held each claimed
name, so that conversation can be TOLD - in ``list_agents``, ``inbox`` and its
wake - and choose to ask for one back through ``register_session``.

Advice only, by design. A hint claims nothing, routes nothing and reads no
mail; every grant still goes through the one ownership rule. So a stale hint
costs a suggestion, never a name: a hint an older revision never cleared, a
second window on the same transcript, a name claimed weeks ago in another
context. Restoring names automatically was reviewed and rejected - see
docs/session-continuity.md.

Keyed by the full conversation id (``CLAUDE_CODE_SESSION_ID``), so a fork,
which gets a new id, inherits nothing. Written after a grant, replaced by the
next grant of the same name on current code, removed by the release of the
grant that wrote it, and bounded. No liveness and no pending state is stored;
``writer`` identifies the granting process only so its own release removes
its own hint and never a newer one.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Optional

from .mailbox import LANE_HINTS_TABLE, _connect, _default_now, _iso, _resolve_db

# A conversation claims few names; the cap only stops one that renames itself
# endlessly from growing the table. Losing an old hint loses a suggestion.
PER_CONVERSATION = 16
ROWS = 1024


def _ensure(conn: sqlite3.Connection) -> None:
    # executescript commits first: call before a transaction, never inside one.
    conn.executescript(LANE_HINTS_TABLE)


def record(
    lane: str, conversation: Optional[str], writer: str = "", *, db_path: Optional[Path] = None
) -> None:
    """``lane`` was just granted to ``conversation``, by the process ``writer``.
    A grant hardline cannot attribute to a conversation (``None``: a Codex or
    Hermes claimant, a lane granted automatically) only clears the old hint:
    the name has moved on, whoever holds it now."""
    with closing(_connect(_resolve_db(db_path))) as conn:
        # Look before reserving the writer: clearing runs on every automatic
        # registration, and there is almost never anything to clear.
        if not conversation and not _rows(conn, "SELECT 1 FROM lane_hints WHERE recipient = ?", lane):
            return
        _ensure(conn)
        with conn:
            conn.execute("DELETE FROM lane_hints WHERE recipient = ?", (lane,))
            if not conversation:
                return
            conn.execute(
                "INSERT INTO lane_hints VALUES (?, ?, ?, ?)",
                (lane, conversation, writer, _iso(_default_now())),
            )
            # Newest kept: a reinserted row gets a new rowid.
            conn.execute(
                "DELETE FROM lane_hints WHERE conversation = ? AND rowid NOT IN ("
                "SELECT rowid FROM lane_hints WHERE conversation = ? ORDER BY rowid DESC LIMIT ?)",
                (conversation, conversation, PER_CONVERSATION),
            )
            conn.execute(
                "DELETE FROM lane_hints WHERE rowid NOT IN ("
                "SELECT rowid FROM lane_hints ORDER BY rowid DESC LIMIT ?)",
                (ROWS,),
            )


def forget(
    lane: str,
    conversation: Optional[str],
    writer: Optional[str] = None,
    *,
    db_path: Optional[Path] = None,
) -> bool:
    """Drop ``conversation``'s hint for ``lane``; True if there was one.

    With ``writer``, only the hint that process's grant wrote: a release must
    not remove the hint of a newer grant of the same name, which another
    window on the same transcript can make the moment the release commits.
    Without it - a conversation dismissing advice it does not want - whatever
    hint this conversation has for the name. Never another conversation's.
    """
    if not conversation:
        return False
    with closing(_connect(_resolve_db(db_path))) as conn:
        _ensure(conn)
        with conn:
            cursor = conn.execute(
                "DELETE FROM lane_hints WHERE recipient = ? AND conversation = ?"
                " AND (? IS NULL OR writer = ?)",
                (lane, conversation, writer, writer),
            )
    return cursor.rowcount > 0


def held_before(conversation: Optional[str], *, db_path: Optional[Path] = None) -> list[str]:
    """The names ``conversation`` last held, oldest first."""
    if not conversation:
        return []
    with closing(_connect(_resolve_db(db_path))) as conn:
        rows = _rows(
            conn, "SELECT recipient FROM lane_hints WHERE conversation = ? ORDER BY rowid", conversation
        )
    return [r[0] for r in rows]


def _rows(conn: sqlite3.Connection, sql: str, *params) -> list:
    """A read that finds nothing in a store no hint was ever written to."""
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return []
