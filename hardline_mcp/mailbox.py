"""SQLite-backed inter-agent mailbox — the durable core of hardline-mcp.

Every agent (Claude Code, Hermes, Codex) runs its own hardline-mcp subprocess,
so the shared state is a single on-disk SQLite database
(``~/.cache/hardline-mcp/mailbox.db`` by default). SQLite in WAL mode with a
busy timeout handles the concurrent multi-writer case natively — no
temp-file/lock dance — which is why it's used here rather than the JSON
ledger pattern of the sibling vram-mcp project.

Pure logic only: no ``mcp`` import. ``db_path`` and ``now_fn`` are injectable
so tests run against a temp database with a controllable clock.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

_DEFAULT_PATH = Path.home() / ".cache" / "hardline-mcp" / "mailbox.db"


def _resolve_db(db_path: Optional[Path]) -> Path:
    """Pick the mailbox file: an explicit ``db_path`` wins (tests), else the
    ``HARDLINE_DB`` env var (relocate the store, or run isolated instances),
    else the default under ~/.cache. Read at call time so a subprocess started
    with HARDLINE_DB set is honored without re-import."""
    if db_path is not None:
        return db_path
    env = os.environ.get("HARDLINE_DB")
    return Path(env) if env else _DEFAULT_PATH


_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sender     TEXT NOT NULL,
    recipient  TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    acked_at   TEXT
)
"""

# One-time per-db init (schema + WAL) is guarded so it happens exactly once
# per process per path. WAL is a *persistent* DB-header property, so setting it
# per connection is not just wasteful — it's actively harmful under
# concurrency: several connections each trying to switch journal mode contend
# on an exclusive lock and raise "database is locked" (the WAL-mode change does
# not honor busy_timeout). Establish it once up front; per-op connections then
# only need busy_timeout to make concurrent writers WAIT for the single WAL
# write lock instead of erroring.
_init_lock = threading.Lock()
_initialized_paths: set[str] = set()

# Cold-start contention on the WAL transition is brief; a few short retries
# cover it (see _ensure_initialized).
_INIT_ATTEMPTS = 5
_INIT_BACKOFF_S = 0.05

# Batch size for one inbox poll, and the ceiling a caller cannot exceed.
# The ceiling matters more than the default: the bound exists to keep a
# backlog out of the caller's context, so it must not be opt-out-able.
DEFAULT_INBOX_LIMIT = 25
MAX_INBOX_LIMIT = 200


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _ensure_initialized(db_path: Path) -> None:
    """Create the parent dir, enable WAL, and create the schema once per db
    file per process, retrying the cross-process race.

    ``_init_lock`` is an in-process lock, and every agent runs its OWN server
    process against the shared file - so on a cold start (or after the db is
    deleted) a dozen processes can reach the WAL transition simultaneously,
    each holding a lock the others cannot see. The journal-mode change takes
    an exclusive lock and, unlike ordinary writes, does NOT honor
    busy_timeout, so the losers raise "database is locked" outright.

    Retry with a short backoff rather than adding a lockfile: the transition
    is idempotent (a process that finds WAL already set does nothing), fast,
    and only contended in the brief cold-start window, so a few retries close
    the race without a second locking scheme to keep correct.
    """
    key = str(db_path)
    if key in _initialized_paths:
        return
    with _init_lock:
        if key in _initialized_paths:
            return
        db_path.parent.mkdir(parents=True, exist_ok=True)
        last_error: Optional[sqlite3.OperationalError] = None
        for attempt in range(_INIT_ATTEMPTS):
            try:
                with closing(sqlite3.connect(str(db_path), timeout=10.0)) as conn:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute(_SCHEMA)
                    conn.commit()
                break
            except sqlite3.OperationalError as exc:
                # Only the contention this retry exists for. A permanent fault
                # (unwritable path, corrupt file, disk full) is not going to
                # resolve in 500ms, and retrying it just delays a clear error
                # behind a misleading one.
                if "locked" not in str(exc) and "busy" not in str(exc):
                    raise
                last_error = exc
                if attempt == _INIT_ATTEMPTS - 1:
                    raise
                time.sleep(_INIT_BACKOFF_S * (attempt + 1))
        else:  # pragma: no cover - loop always breaks or raises
            raise last_error  # type: ignore[misc]
        _initialized_paths.add(key)


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection for a single operation. Schema + WAL are established
    once by ``_ensure_initialized``; ``busy_timeout`` makes concurrent writers
    wait for the single WAL write lock rather than fail on a momentary lock."""
    _ensure_initialized(db_path)
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
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
    from_agent: str,
    to_agent: str,
    body: str,
    *,
    db_path: Optional[Path] = None,
    now_fn: Callable[[], datetime] = _default_now,
) -> dict:
    """Persist a message from ``from_agent`` to ``to_agent``.

    Returns ``{"message_id", "created_at"}``. Delivery/push is a separate
    concern (see adapters + the server's ``deliver`` flag); this only records.
    """
    db_path = _resolve_db(db_path)
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
    agent,
    *,
    unread_only: bool = True,
    limit: int = DEFAULT_INBOX_LIMIT,
    auto_ack: bool = True,
    db_path: Optional[Path] = None,
    now_fn: Callable[[], datetime] = _default_now,
) -> tuple[list[dict], int]:
    """Messages addressed TO ``agent``, oldest first (read in arrival order).

    Returns ``(messages, remaining)`` where ``remaining`` counts the messages
    still unacked for these recipients AFTER this call - a caller polls again
    while it is non-zero.

    ``agent`` may be one name or several. Several is how a session reads its
    own lane AND the shared unqualified one in a single ordered pass, rather
    than merging two queries at the call site.

    ``unread_only`` (default) hides already-acked messages.

    ``limit`` bounds the batch, clamped to ``MAX_INBOX_LIMIT``. The clamp is
    the actual invariant: an unbounded read let an un-drained backlog grow
    into the caller's context until it overflowed (153 stale async results,
    ~168k tokens, on a single poll), so a caller must not be able to opt back
    out of the bound by passing a huge limit.

    ``auto_ack`` (default) marks exactly the returned ids read, in the SAME
    transaction as the read. This is what makes ``limit`` safe rather than
    harmful: oldest-first + a limit + nothing acking pins the caller to the
    same oldest batch forever and it never sees a new message again. Acking
    server-side advances the cursor per poll, so the backlog drains.

    Acking only ids this call returned is inherently lane-correct - the
    SELECT already restricts to ``agents``, and the UPDATE re-asserts that
    recipient scope so no id can ack outside the requested recipients. Nothing
    is destroyed: ``history`` ignores ``acked_at`` and remains the recovery
    path for anything acked.
    """
    db_path = _resolve_db(db_path)
    agents = [agent] if isinstance(agent, str) else list(agent)
    if not agents:
        return [], 0
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_INBOX_LIMIT
    limit = max(1, min(limit, MAX_INBOX_LIMIT))

    placeholders = ", ".join("?" for _ in agents)
    sql = f"SELECT * FROM messages WHERE recipient IN ({placeholders})"
    if unread_only:
        sql += " AND acked_at IS NULL"
    sql += " ORDER BY id ASC LIMIT ?"

    with closing(_connect(db_path)) as conn:
        # One transaction: read, consume, then count what is left. A concurrent
        # poller on the shared db therefore cannot be handed the same batch.
        with conn:
            rows = conn.execute(sql, (*agents, limit)).fetchall()
            messages = [_row_to_dict(r) for r in rows]
            if auto_ack and messages:
                ids = [m["message_id"] for m in messages]
                id_marks = ", ".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE messages SET acked_at = ? WHERE id IN ({id_marks})"
                    f" AND recipient IN ({placeholders}) AND acked_at IS NULL",
                    (_iso(now_fn()), *ids, *agents),
                )
            remaining = conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE recipient IN ({placeholders})"
                " AND acked_at IS NULL",
                tuple(agents),
            ).fetchone()[0]
        return messages, remaining


def peek(message_id: int, *, db_path: Optional[Path] = None) -> Optional[dict]:
    """One message by id, or None. Lets a caller check who a message belongs
    to before acting on it (see ``ack``'s lane guard)."""
    db_path = _resolve_db(db_path)
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None


def ack(
    message_id: int,
    *,
    lane_suffix: Optional[str] = None,
    db_path: Optional[Path] = None,
    now_fn: Callable[[], datetime] = _default_now,
) -> dict:
    """Mark one message read. Returns ``{"ok": True}`` only if a still-unread
    message with that id existed (idempotent: a second ack returns False).

    ``lane_suffix`` is this process's session lane, or empty when it has
    none. Unqualified recipients stay shared and ackable by anyone, so
    cross-agent messaging is unaffected. A LANE-QUALIFIED message is ackable
    only by the process holding that lane - one session hiding another's
    results is precisely the failure this guards.

    Note the empty case is guarded too, not skipped. Skipping it left a
    bypass: any process without a lane (Hermes, Codex, or a Claude session
    whose environment lacked the variables) applied no condition at all and
    could ack every other session's mail - the exact defect lanes exist to
    prevent, reachable by the callers most likely to poll a shared mailbox.
    A process with no lane owns no lane, so it may ack only bare recipients.
    """
    db_path = _resolve_db(db_path)
    sql = "UPDATE messages SET acked_at = ? WHERE id = ? AND acked_at IS NULL"
    params: tuple = (_iso(now_fn()), message_id)
    if lane_suffix:
        sql += (
            " AND (instr(recipient, ':') = 0"
            " OR substr(recipient, instr(recipient, ':') + 1) = ?)"
        )
        params = params + (lane_suffix,)
    else:
        sql += " AND instr(recipient, ':') = 0"
    with closing(_connect(db_path)) as conn:
        with conn:  # transaction: commit on success, rollback on error
            cur = conn.execute(sql, params)
        return {"ok": cur.rowcount > 0}


def history(
    limit: int = 50,
    agent: Optional[str] = None,
    *,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Recent messages newest-first (the visibility/log feed). ``agent``, if
    given, matches messages where it is EITHER sender or recipient - including
    that agent's session lanes.

    The lane match is what makes this the audit feed it claims to be. Async
    results are addressed to ``claude:<lane>``, so exact-equality filtering
    silently stopped showing them the moment lanes shipped: ``history`` is
    also the ack-proof recovery path (``ack`` only sets ``acked_at``, and this
    query ignores it), so losing lane messages here quietly broke the one way
    to retrieve a result another session had already acked.

    A lane-qualified ``agent`` still matches only itself - ``claude:a`` does
    not gain sublanes.

    DELIBERATE: this sees every lane, so it is NOT session-isolated the way
    ``inbox`` and ``ack`` are. That is the point of an audit feed on a
    single-user machine - lanes exist to stop sessions clobbering each other
    by accident, not to keep one person's sessions secret from themselves,
    and full visibility here is exactly what let one session's lost results
    be recovered for another. Isolation lives in ``inbox``/``ack``; if this
    ever needs to be confidential, that is a different feature with a
    different threat model.
    """
    db_path = _resolve_db(db_path)
    params: tuple = ()
    sql = "SELECT * FROM messages"
    if agent is not None:
        lane_glob = f"{agent}:%"
        sql += " WHERE sender = ? OR recipient = ? OR sender LIKE ? OR recipient LIKE ?"
        params = (agent, agent, lane_glob, lane_glob)
    sql += " ORDER BY id DESC LIMIT ?"
    params = params + (limit,)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]
