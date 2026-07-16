"""SQLite-backed inter-agent mailbox — the durable core of comms-mcp.

Every agent (Claude Code, Hermes, Codex) runs its own comms-mcp subprocess,
so the shared state is a single on-disk SQLite database
(``~/.cache/comms-mcp/mailbox.db`` by default). SQLite in WAL mode with a
busy timeout handles the concurrent multi-writer case natively — no
temp-file/lock dance — which is why it's used here rather than the JSON
ledger pattern of the sibling vram-mcp project.

Pure logic only: no ``mcp`` import. ``db_path`` and ``now_fn`` are injectable
so tests run against a temp database with a controllable clock.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

_DEFAULT_PATH = Path.home() / ".cache" / "comms-mcp" / "mailbox.db"


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating parent dir + schema on first use) with WAL + busy timeout
    so concurrent agent subprocesses can read/write without corrupting each
    other or spuriously failing on a momentary lock."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            sender     TEXT NOT NULL,
            recipient  TEXT NOT NULL,
            body       TEXT NOT NULL,
            created_at TEXT NOT NULL,
            acked_at   TEXT
        )
        """
    )
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "message_id": row["id"],
        "sender": row["sender"],
        "recipient": row["recipient"],
        "body": row["body"],
        "created_at": row["created_at"],
        "acked_at": row["acked_at"],
    }


def send(
    from_agent: str, to_agent: str, body: str, *,
    db_path: Optional[Path] = None, now_fn: Callable[[], datetime] = _default_now,
) -> dict:
    """Persist a message from ``from_agent`` to ``to_agent``.

    Returns ``{"message_id", "created_at"}``. Delivery/push is a separate
    concern (see adapters + the server's ``deliver`` flag); this only records.
    """
    db_path = db_path or _DEFAULT_PATH
    created = _iso(now_fn())
    with closing(_connect(db_path)) as conn:
        with conn:  # transaction: commit on success, rollback on error
            cur = conn.execute(
                "INSERT INTO messages (sender, recipient, body, created_at) "
                "VALUES (?, ?, ?, ?)",
                (from_agent, to_agent, body, created),
            )
        return {"message_id": cur.lastrowid, "created_at": created}


def inbox(
    agent: str, *, unread_only: bool = True,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Messages addressed TO ``agent``, oldest first (read in arrival order).

    ``unread_only`` (default) hides already-acked messages.
    """
    db_path = db_path or _DEFAULT_PATH
    sql = "SELECT * FROM messages WHERE recipient = ?"
    if unread_only:
        sql += " AND acked_at IS NULL"
    sql += " ORDER BY id ASC"
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(sql, (agent,)).fetchall()
        return [_row_to_dict(r) for r in rows]


def ack(
    message_id: int, *,
    db_path: Optional[Path] = None, now_fn: Callable[[], datetime] = _default_now,
) -> dict:
    """Mark one message read. Returns ``{"ok": True}`` only if a still-unread
    message with that id existed (idempotent: a second ack returns False)."""
    db_path = db_path or _DEFAULT_PATH
    with closing(_connect(db_path)) as conn:
        with conn:  # transaction: commit on success, rollback on error
            cur = conn.execute(
                "UPDATE messages SET acked_at = ? WHERE id = ? AND acked_at IS NULL",
                (_iso(now_fn()), message_id),
            )
        return {"ok": cur.rowcount > 0}


def history(
    limit: int = 50, agent: Optional[str] = None, *,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Recent messages newest-first (the visibility/log feed). ``agent``, if
    given, matches messages where it is EITHER sender or recipient."""
    db_path = db_path or _DEFAULT_PATH
    params: tuple = ()
    sql = "SELECT * FROM messages"
    if agent is not None:
        sql += " WHERE sender = ? OR recipient = ?"
        params = (agent, agent)
    sql += " ORDER BY id DESC LIMIT ?"
    params = params + (limit,)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]
