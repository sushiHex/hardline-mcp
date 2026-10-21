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

One more state comes from a Codex route row instead: ``not_top_level``, a
server called so far only from a thread Codex names as not top-level (a
subagent's). Its wake is not armed, so nothing announces its lanes' mail,
though the holder can still read it (#109).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from .mailbox import CODEX_ROUTES_TABLE, PUSH_DELIVERY_TABLE, _connect, _iso, _resolve_db
from .procid import DEAD, instance_state

GRACE = timedelta(minutes=10)
ROUTE_KEYS = ("last_route", "last_route_reason", "steered", "queued", "ambiguous")
# A route row's last_route, and the delivery state it implies, for a Codex
# server called only from threads Codex names as not top-level: unarmed.
NOT_TOP_LEVEL = "not_top_level"


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(PUSH_DELIVERY_TABLE)
    conn.execute(CODEX_ROUTES_TABLE)


def prune(*, db_path: Optional[Path] = None) -> None:
    """Drop rows of processes that are certainly gone (DEAD, never UNKNOWN).

    Compare-and-swap on (pid, process_key), as ``sessions._prune_dead`` does,
    so a pid reused between the probe and the delete is never touched.
    """
    with closing(_connect(_resolve_db(db_path))) as conn:
        _ensure(conn)
        for table in ("push_delivery", "codex_routes"):
            rows = conn.execute(f"SELECT pid, process_key FROM {table}").fetchall()
            dead = [tuple(r) for r in rows if instance_state(r[0], r[1]) == DEAD]
            if dead:
                with conn:
                    conn.executemany(f"DELETE FROM {table} WHERE pid = ? AND process_key = ?", dead)


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
                "INSERT OR REPLACE INTO push_delivery (pid, process_key, declared_at,"
                " last_push_at, last_receipted_push_at, oldest_unreceipted_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    pid,
                    process_key,
                    _iso(declared_at),
                    stamp(last_push_at),
                    stamp(last_receipted_push_at),
                    stamp(oldest_unreceipted_at),
                ),
            )


def record_routes(pid: int, process_key: str, *, db_path: Optional[Path] = None, **routes) -> None:
    """Write which way this process's Codex notices went (``ROUTE_KEYS``): its
    own running totals, replacing its previous row. A table of its own, apart
    from ``record``, so neither write can lose the other."""
    with closing(_connect(_resolve_db(db_path))) as conn:
        with conn:
            _ensure(conn)
            conn.execute(
                "INSERT OR REPLACE INTO codex_routes (pid, process_key, "
                + ", ".join(ROUTE_KEYS)
                + ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pid, process_key, *(routes.get(k) for k in ROUTE_KEYS)),
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


def _read(
    instances: Iterable[tuple[int, Optional[str]]], db_path: Optional[Path]
) -> tuple[dict[tuple[int, str], dict], set[tuple[int, str]]]:
    """``(push fact rows, identities marked not_top_level)`` for the given
    instances, read in one snapshot. The writer commits the two separately, so
    a pin can be seen half-written; push facts outranking the mark covers the
    order it writes them in."""
    wanted = {(pid, key) for pid, key in instances if key}
    if not wanted:
        return {}, set()
    with closing(_connect(_resolve_db(db_path))) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        pushes = _select(conn, "SELECT * FROM push_delivery")
        routes = _select(conn, "SELECT pid, process_key FROM codex_routes WHERE last_route = ?", NOT_TOP_LEVEL)
        conn.rollback()
    facts = {(r["pid"], r["process_key"]): dict(r) for r in pushes}
    marked = {(r["pid"], r["process_key"]) for r in routes}
    return {i: f for i, f in facts.items() if i in wanted}, marked & wanted


def _select(conn: sqlite3.Connection, sql: str, *args) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return []  # written by nothing yet, so nothing to report


def facts_for(
    instances: Iterable[tuple[int, Optional[str]]], *, db_path: Optional[Path] = None
) -> dict[tuple[int, str], dict]:
    """Fact rows for the given (pid, process_key) instances that have one."""
    return _read(instances, db_path)[0]


def states_for(
    instances: Iterable[tuple[int, Optional[str]]], now: datetime, *, db_path: Optional[Path] = None
) -> dict[tuple[int, str], str]:
    """The delivery state of each instance that records one: derived from its
    push facts, else ``not_top_level`` from its route row's mark."""
    facts, marked = _read(instances, db_path)
    return {**{i: NOT_TOP_LEVEL for i in marked}, **{i: derive(f, now) for i, f in facts.items()}}
