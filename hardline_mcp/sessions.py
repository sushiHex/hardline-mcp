"""Which sessions exist, what they are called, and which are still alive.

Identity was the one thing in hardline with no durable record. A job survives a
restart; a message survives a restart; the SESSION holding a lane existed only
as a function of one process's environment. Three consequences, all present in
the live store:

* Nothing could answer "who can I address?". ``list_agents`` reported the
  recipients the MAILBOX had seen, which is a log of who was once written to -
  so long-dead sessions were presented as destinations.
* Codex and Hermes set none of the lane variables, so every Codex session
  shared one unqualified identity and no individual session could be reached.
  Their MCP registration is a single static env block, so ``HARDLINE_AGENT_LABEL``
  cannot distinguish two sessions launched from it.
* A lane-qualified message to a session that had exited became permanently
  unconsumable. Only the lane's holder may ack it, and the holder was gone. 51
  such messages had accumulated across 11 dead lanes.

This module is the third durable table, and it follows the two rules ``jobs``
established rather than inventing its own:

**The record is written by the thing itself; the derived state is computed on
read.** Liveness is never stored. A session that crashes cannot write "I died",
so asking the OS at read time is the only answer that is true - and it costs
nothing until somebody asks. No heartbeat thread, no TTL, no reaper.

**A pid is not an identity.** Every row carries the creation-time token from
``procid``. Without it a reused pid would inherit the previous session's lane,
and with it its mail.

One row per (process, LANE), not per process. A session owns every lane it has
held - renaming adds a name rather than replacing one, so results dispatched
under the old name stay consumable - and a registry that recorded only the
current name would contradict that: the old lane would show no holder, so
``list_agents`` would report it dead, ``send`` would warn nobody could receive
it, and another session could CLAIM it while the original was still consuming
it. Both would then hold the lane and drain each other's mail nondeterministic-
ally, which is the precise failure lanes exist to prevent.

Scope is deliberately one question - who is alive NOW. The history of who was
alive belongs to the messages and jobs tables, which already keep it; a
registry that also tried to be a history would be two things, and the dead rows
it kept would be exactly the stale destinations this exists to stop reporting.

Two consequences that look like bugs and are decisions
------------------------------------------------------

**A label is a role, not an instance.** Mail addressed to ``codex:construction``
is consumable by whoever holds that name, including a session that claims it
AFTER the message was sent. So a label can be sent to before anyone answers to
it, and a later claimant inherits the backlog. The alternative - an ownership
epoch in the recipient, making each claim a distinct address - would mean mail
sent to a name nobody currently holds is undeliverable by construction, which
is the stranding this exists to remove, and would make ``send`` to a session
that has not started yet impossible. A human alias cannot also be a stable
instance address; this picks the alias, because that is what the name is FOR.

**A claim does not survive its process.** Runtime claims live only in memory,
so a ``/mcp`` reconnect returns a Codex or Hermes session to anonymity and it
must call ``register_session`` again. Persisting a claim across processes would
require deciding that a NEW process is the same session as a dead one, which
nothing here can know - the pid is different and the identity token is
deliberately not reusable. The decision above is what makes this recoverable
rather than fatal: re-claiming the same label succeeds (the old holder is dead)
and the session inherits the mail that arrived while it was away.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

from .mailbox import _connect, _default_now, _iso, _resolve_db
from .procid import instance_alive, process_key


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "lane": row["lane"],
        "agent": row["agent"],
        "label": row["label"],
        "pid": row["pid"],
        "cwd": row["cwd"],
        "started_at": row["started_at"],
        "last_seen": row["last_seen"],
    }


def _is_live(row: sqlite3.Row) -> bool:
    """Is the process behind this row still running, and still itself?"""
    return instance_alive(row["pid"], row["process_key"])


def _prune_dead(conn: sqlite3.Connection) -> None:
    """Drop rows whose process is gone.

    On read, like ``jobs._sweep_lost``, and for the same reason: the process
    that would have removed its own row is precisely the one that died. A
    session that crashes therefore needs no cleanup path at all.

    Deletes on (pid, process_key), never pid alone. Between the probe and the
    DELETE the OS can hand that pid to a new process which registers itself,
    and a pid-only predicate would delete the live newcomer on the strength of
    a liveness decision made about its predecessor. Matching the token we
    actually probed makes the delete a compare-and-swap.
    """
    rows = conn.execute("SELECT pid, lane, process_key FROM sessions").fetchall()
    dead = [(r["pid"], r["lane"], r["process_key"]) for r in rows if not _is_live(r)]
    if not dead:
        return
    with conn:
        conn.executemany(
            "DELETE FROM sessions WHERE pid = ? AND lane = ?"
            " AND process_key IS ?",
            dead,
        )


def drop_lane(
    lane: str, *, pid: Optional[int] = None, db_path: Optional[Path] = None
) -> bool:
    """Give up one lane this process holds. Returns whether a row was there.

    The undo half of a claim whose local adoption was refused after the durable
    write succeeded. Scoped to this pid so it can never release somebody else's
    hold on the same name.
    """
    db_path = _resolve_db(db_path)
    pid = os.getpid() if pid is None else pid
    with closing(_connect(db_path)) as conn:
        with conn:
            cur = conn.execute(
                "DELETE FROM sessions WHERE pid = ? AND lane = ?", (pid, lane)
            )
        return cur.rowcount > 0


def _upsert(conn, *, pid, lane, agent, label, key, cwd, stamp) -> None:
    """Write one (process, lane) row, creating it only if not already there.

    UPDATE-then-INSERT rather than INSERT OR REPLACE, so ``started_at`` and
    ``claimed_at`` keep saying when the session and the lane began instead of
    resetting on every heartbeat, and rather than ON CONFLICT upsert, which
    needs SQLite 3.24+ - the store is otherwise readable by older builds and one
    statement is not worth the floor.

    Shared by ``register`` and ``claim`` because it is one rule. Two copies of
    an upsert is how ``started_at`` ends up resetting on one path and not the
    other, and nothing about that reads as wrong at either site.
    """
    cur = conn.execute(
        "UPDATE sessions SET agent = ?, label = ?, process_key = ?, cwd = ?,"
        " last_seen = ? WHERE pid = ? AND lane = ?",
        (agent, label, key, cwd, stamp, pid, lane),
    )
    if cur.rowcount == 0:
        # `seq`, not the timestamp, is what orders a session's names. Stored
        # times have second precision, so two claims inside one second are
        # indistinguishable by time and the sort would fall back to the lane
        # TEXT - making a rapid rename advertise whichever name happens to sort
        # last. A per-process counter cannot tie.
        conn.execute(
            "INSERT INTO sessions (pid, lane, agent, label, process_key, cwd,"
            " started_at, last_seen, claimed_at, seq)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,"
            " (SELECT COALESCE(MAX(seq), 0) + 1 FROM sessions WHERE pid = ?))",
            (pid, lane, agent, label, key, cwd, stamp, stamp, stamp, pid),
        )


def register(
    *,
    agent: str,
    lane: Optional[str] = None,
    lanes: Optional[Iterable[str]] = None,
    label: Optional[str] = None,
    pid: Optional[int] = None,
    cwd: Optional[str] = None,
    db_path: Optional[Path] = None,
    now_fn: Callable[[], datetime] = _default_now,
) -> dict:
    """Record (or refresh) the lanes this process holds. Idempotent.

    ``lanes`` is the COMPLETE set; anything previously recorded for this pid and
    absent from it is removed, so the table always says exactly what the process
    would claim to own. ``lane`` is the single-lane spelling, kept because most
    callers hold exactly one.

    Only ever writes rows whose pid is this process, so the cross-process case
    cannot collide. A row left behind by a dead session that happened to hold
    this pid is correctly overwritten: the pid is ours now.

    Safe without an explicit transaction even though ``_upsert`` is a
    check-then-act, because the UPDATE takes SQLite's single write lock whether
    or not it matches a row: a second writer blocks there and then finds the row
    its own UPDATE was looking for. ``claim`` cannot lean on that - its check is
    a SELECT, which takes no such lock - so it opens the transaction itself.
    """
    db_path = _resolve_db(db_path)
    pid = os.getpid() if pid is None else pid
    held = tuple(dict.fromkeys(list(lanes or ()) + ([lane] if lane else [])))
    if not held:
        return {"agent": agent, "label": label, "pid": pid, "lanes": []}
    stamp = _iso(now_fn())
    key = process_key(pid)
    cwd = cwd if cwd is not None else str(Path.cwd())
    with closing(_connect(db_path)) as conn:
        with conn:
            for one in held:
                _upsert(
                    conn,
                    pid=pid,
                    lane=one,
                    agent=agent,
                    label=label,
                    key=key,
                    cwd=cwd,
                    stamp=stamp,
                )
            marks = ", ".join("?" for _ in held)
            conn.execute(
                f"DELETE FROM sessions WHERE pid = ? AND lane NOT IN ({marks})",
                (pid, *held),
            )
    return {
        "lane": held[-1],
        "lanes": list(held),
        "agent": agent,
        "label": label,
        "pid": pid,
    }


def _group(rows: list[sqlite3.Row]) -> list[dict]:
    """Collapse per-lane rows into one entry per session.

    ``lane`` is the name the session is ADDRESSED by - the most recently
    claimed - while ``lanes`` is everything it can still consume. Reporting
    only the current one is what let a renamed session's old lane read as
    unheld.
    """
    by_pid: dict[int, dict] = {}
    for row in sorted(rows, key=lambda r: (r["pid"], r["seq"])):
        entry = by_pid.get(row["pid"])
        if entry is None:
            entry = _row_to_dict(row)
            entry["lanes"] = []
            by_pid[row["pid"]] = entry
        entry["lanes"].append(row["lane"])
        # Rows are in claimed_at order, so the last one wins as the current name.
        entry["lane"] = row["lane"]
        entry["label"] = row["label"]
    return sorted(by_pid.values(), key=lambda e: (e["started_at"], e["pid"]))


def live(
    *,
    agent: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Sessions whose process is still running, oldest first.

    Prunes the dead as it goes, so the registry answers "who is here" and
    never accumulates the stale destinations it exists to stop reporting.
    """
    db_path = _resolve_db(db_path)
    with closing(_connect(db_path)) as conn:
        _prune_dead(conn)
        sql = "SELECT * FROM sessions"
        params: tuple = ()
        if agent:
            sql += " WHERE agent = ?"
            params = (agent,)
        rows = conn.execute(sql, params).fetchall()
        # Re-check rather than trust the prune: a session can exit between the
        # DELETE and the SELECT, and reporting a destination that has just gone
        # is the failure this module exists to remove.
        return _group([r for r in rows if _is_live(r)])


def holders(lane: str, *, db_path: Optional[Path] = None) -> list[dict]:
    """Live sessions holding ``lane`` (a fully-qualified recipient).

    Normally zero or one. Two would mean two processes answering to one name,
    which ``claim`` refuses to create but a hand-set ``HARDLINE_AGENT_LABEL``
    on two sessions can still produce - so callers get a list and can say so.
    """
    db_path = _resolve_db(db_path)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute("SELECT * FROM sessions WHERE lane = ?", (lane,)).fetchall()
        return [_row_to_dict(r) for r in rows if _is_live(r)]


def claim(
    *,
    agent: str,
    label: str,
    lanes: Iterable[str] = (),
    pid: Optional[int] = None,
    cwd: Optional[str] = None,
    db_path: Optional[Path] = None,
    now_fn: Callable[[], datetime] = _default_now,
) -> dict:
    """Take ``agent:label`` as this process's lane, unless someone live holds it.

    Returns ``{"ok": True, "lane": ...}`` or ``{"ok": False, "error": ...}``.
    ``lanes`` is what the process already holds; they are retained alongside the
    new name so nothing in flight to them strands.

    A name collision is resolved by liveness rather than by seniority: a dead
    holder's claim means nothing, and refusing on its behalf would make a label
    unusable forever after the session that used it crashed. A LIVE holder is
    refused and named, because silently moving a name would redirect mail
    somebody is still waiting on.

    The check and the write are one transaction, opened with BEGIN IMMEDIATE.
    Without it this is a check-then-act across two connections: two sessions
    claiming the same label at once both read "nobody holds it", both insert
    their OWN rows - which collide with nothing, since the key is (pid, lane) -
    and both end up holding the lane. They would then each consume the other's
    mail. ``register``'s UPDATE-then-INSERT is safe for the opposite reason (its
    UPDATE takes the write lock immediately), and relying on that here would be
    relying on a lock this code does not take. BEGIN DEFERRED is not enough
    under WAL: the read-to-write upgrade can fail with SQLITE_BUSY_SNAPSHOT,
    which busy_timeout cannot resolve.
    """
    db_path = _resolve_db(db_path)
    pid = os.getpid() if pid is None else pid
    lane = f"{agent}:{label}"
    held = tuple(dict.fromkeys([*lanes, lane]))
    stamp = _iso(now_fn())
    key = process_key(pid)
    cwd = cwd if cwd is not None else str(Path.cwd())
    with closing(_connect(db_path)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM sessions WHERE lane = ?", (lane,)
            ).fetchall()
            existing = [
                _row_to_dict(r) for r in rows if r["pid"] != pid and _is_live(r)
            ]
            if existing:
                conn.rollback()
                held_by = ", ".join(f"pid {h['pid']} ({h['cwd']})" for h in existing)
                return {
                    "ok": False,
                    "error": (
                        f"lane {lane!r} is already held by a live session: "
                        f"{held_by}. Pick another label, or let that session "
                        "exit first."
                    ),
                    "lane": lane,
                    "held_by": existing,
                }
            # Any remaining row for this lane belongs to a dead session (or to
            # us). Clear it so the takeover leaves exactly one holder.
            conn.execute(
                "DELETE FROM sessions WHERE lane = ? AND pid != ?", (lane, pid)
            )
            for one in held:
                _upsert(
                    conn,
                    pid=pid,
                    lane=one,
                    agent=agent,
                    label=label if one == lane else None,
                    key=key,
                    cwd=cwd,
                    stamp=stamp,
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return {"ok": True, "lane": lane, "label": label, "pid": pid, "lanes": list(held)}


def unregister(
    *, pid: Optional[int] = None, db_path: Optional[Path] = None
) -> bool:
    """Remove every row for this process. Returns whether any were there.

    Not required for correctness - a vanished process is pruned on the next
    read either way - but a clean shutdown that says so keeps the registry
    honest between a session ending and anyone next looking.
    """
    db_path = _resolve_db(db_path)
    pid = os.getpid() if pid is None else pid
    with closing(_connect(db_path)) as conn:
        with conn:
            cur = conn.execute("DELETE FROM sessions WHERE pid = ?", (pid,))
        return cur.rowcount > 0
