"""Whether pushed mail is reaching a session: recorded facts, derived answer.

Claude Code never acknowledges a channel notification, and drops it silently
when the session was launched without the channel flag or has since moved to a
background host. Mail being consumed is no proof either: a Monitor or a manual
``inbox`` consumes it just the same. The one positive evidence is a RECEIPT - a
nonce that exists only inside a notification, echoed back by the model.

So each serving process records facts about itself, and readers derive the
state, the way liveness is derived rather than stored:

* ``declared``          - push offered to a Claude Code client, nothing pushed
* ``receipted``         - every push so far is covered by a receipt
* ``awaiting_receipt``  - a push has gone unreceipted for less than the grace
* ``unreceipted``       - a push has gone unreceipted for longer: the channel
                          may be broken, or the session is not acting on pushes

No row means unknown - a non-Claude client or older code - never "no push".
Rows are only read for live sessions, so a dead process's row is inert.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from .mailbox import PUSH_DELIVERY_TABLE, _connect, _iso, _resolve_db
from .procid import DEAD, instance_state

GRACE = timedelta(minutes=10)


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(PUSH_DELIVERY_TABLE)


def prune(*, db_path: Optional[Path] = None) -> None:
    """Drop rows of processes that are certainly gone (DEAD, never UNKNOWN).

    Compare-and-swap on (pid, process_key), as ``sessions._prune_dead`` does,
    so a pid reused between the probe and the delete is never touched.
    """
    with closing(_connect(_resolve_db(db_path))) as conn:
        _ensure(conn)
        rows = conn.execute("SELECT pid, process_key FROM push_delivery").fetchall()
        dead = [tuple(r) for r in rows if instance_state(r[0], r[1]) == DEAD]
        if dead:
            with conn:
                conn.executemany(
                    "DELETE FROM push_delivery WHERE pid = ? AND process_key = ?", dead
                )


def record(
    pid: int,
    process_key: str,
    *,
    declared_at: datetime,
    last_push_at: Optional[datetime] = None,
    last_receipted_push_at: Optional[datetime] = None,
    oldest_unreceipted_at: Optional[datetime] = None,
    db_path: Optional[Path] = None,
) -> None:
    """Write this process's current facts, replacing its previous row."""
    stamp = lambda dt: _iso(dt) if dt else None  # noqa: E731
    with closing(_connect(_resolve_db(db_path))) as conn:
        with conn:
            _ensure(conn)
            conn.execute(
                "INSERT OR REPLACE INTO push_delivery VALUES (?, ?, ?, ?, ?, ?)",
                (
                    pid,
                    process_key,
                    _iso(declared_at),
                    stamp(last_push_at),
                    stamp(last_receipted_push_at),
                    stamp(oldest_unreceipted_at),
                ),
            )


def derive(row: Optional[dict], now: datetime) -> Optional[str]:
    """The state a fact row implies, or None for no row."""
    if row is None:
        return None
    if not row.get("last_push_at"):
        return "declared"
    oldest = row.get("oldest_unreceipted_at")
    if not oldest:
        return "receipted"
    if isinstance(oldest, str):  # a stored row; live state passes datetimes
        oldest = datetime.fromisoformat(oldest.replace("Z", "+00:00"))
    return "awaiting_receipt" if now - oldest < GRACE else "unreceipted"


def facts_for(
    instances: Iterable[tuple[int, Optional[str]]], *, db_path: Optional[Path] = None
) -> dict[tuple[int, str], dict]:
    """Fact rows for the given (pid, process_key) instances that have one."""
    wanted = {(pid, key) for pid, key in instances if key}
    if not wanted:
        return {}
    try:
        with closing(_connect(_resolve_db(db_path))) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM push_delivery").fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return {}  # written by nothing yet, so nothing to report
    return {
        (r["pid"], r["process_key"]): dict(r)
        for r in rows
        if (r["pid"], r["process_key"]) in wanted
    }
