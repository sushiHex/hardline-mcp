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
from .procid import ALIVE, DEAD, UNKNOWN, current_identity, instance_state, process_key


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "lane": row["lane"],
        "agent": row["agent"],
        "label": row["label"],
        "pid": row["pid"],
        "process_key": row["process_key"],
        "host_pid": row["host_pid"] if "host_pid" in row.keys() else None,
        "host_key": row["host_key"] if "host_key" in row.keys() else None,
        "cwd": row["cwd"],
        "started_at": row["started_at"],
        "last_seen": row["last_seen"],
        # Say WHICH of the two live-ish answers this is. Folding UNKNOWN into
        # "live" and then reporting it as a live session is the same overclaim
        # as telling a sender nothing can consume their message: the honest
        # word is that the process could not be probed, and a caller cannot
        # recover that from a boolean.
        "liveness": _state(row),
    }


def _state(row: sqlite3.Row) -> str:
    """``ALIVE`` / ``DEAD`` / ``UNKNOWN`` for the process behind this row."""
    state = instance_state(row["pid"], row["process_key"])
    if state == DEAD:
        return state
    host = host_state(row)
    if host == DEAD:
        return DEAD
    return UNKNOWN if UNKNOWN in (state, host) else state


def host_state(row: sqlite3.Row) -> str:
    """Host liveness for current and legacy rows; absent identity adds no constraint."""
    if "host_pid" not in row.keys() or row["host_pid"] is None:
        return ALIVE
    return instance_state(row["host_pid"], row["host_key"])


def _is_live(row: sqlite3.Row) -> bool:
    """May this row still be treated as a destination?

    UNKNOWN counts as live. A probe that could not answer is not evidence of
    death, and this predicate gates who may TAKE a lane - so being wrong
    optimistically means reporting a destination that has gone, while being
    wrong pessimistically means handing a live session's name to somebody else.
    """
    return _state(row) != DEAD


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
    rows = conn.execute("SELECT * FROM agent_sessions").fetchall()
    # DEAD only, never merely not-ALIVE. Deleting on an inconclusive probe is
    # how a live session gets unregistered and then has its name claimed by
    # someone else - and the deletion also destroys the evidence that it was
    # ever there.
    dead = [
        (r["pid"], r["lane"], r["process_key"], r["host_pid"], r["host_key"])
        for r in rows
        if _state(r) == DEAD
    ]
    if not dead:
        return
    with conn:
        conn.executemany(
            "DELETE FROM agent_sessions WHERE pid = ? AND lane = ?"
            " AND process_key IS ? AND host_pid IS ? AND host_key IS ?",
            dead,
        )


def _live_work(conn: sqlite3.Connection, lane: str) -> list[dict]:
    """Unfinished jobs whose results are owed to ``lane``, held by a LIVE owner.

    The registry cannot see every consumer. A process running older code never
    registers at all; one whose announcement failed is absent; one behind an
    unanswerable probe looks gone. Taking a lane on the strength of "no row
    here" is therefore taking it on the strength of not having looked.

    An unfinished job is the one piece of hard evidence available. Its
    ``requester`` is the lane, fixed when the job was DISPATCHED - before any
    message exists, so a mailbox-only check would approve a takeover moments
    before the result lands - and its owner's liveness is checkable. A live
    owner with unfinished work means somebody is still coming back for this
    name.
    """
    rows = conn.execute(
        "SELECT job_id, owner_pid, owner_key, state, label FROM jobs"
        " WHERE requester = ? AND state IN ('queued', 'running')",
        (lane,),
    ).fetchall()
    return [
        {
            "job_id": r["job_id"],
            "owner_pid": r["owner_pid"],
            "state": r["state"],
            "label": r["label"],
        }
        for r in rows
        if instance_state(r["owner_pid"], r["owner_key"]) != DEAD
    ]


def _unread_for(conn: sqlite3.Connection, lane: str) -> int:
    """Messages waiting at ``lane`` that a claimant would inherit."""
    return conn.execute(
        "SELECT COUNT(*) FROM messages WHERE recipient = ? AND acked_at IS NULL",
        (lane,),
    ).fetchone()[0]


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
                "DELETE FROM agent_sessions WHERE pid = ? AND lane = ?", (pid, lane)
            )
        return cur.rowcount > 0


def _upsert(
    conn, *, pid, lane, agent, label, key, cwd, stamp, host_pid, host_key
) -> None:
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
    # COALESCE, so ``label=None`` means "leave it alone" rather than "clear it".
    # A lane's label is the name it was CLAIMED under and never changes, but
    # both callers rewrite every retained lane on each pass and pass None for
    # the ones they are merely keeping. Assigning directly stripped the name
    # from every previous lane - and if the new claim was then rolled back, from
    # the one the session was still answering to, leaving list_agents
    # advertising it with no label at all.
    cur = conn.execute(
        "UPDATE agent_sessions SET agent = ?, label = COALESCE(?, label),"
        " process_key = ?, cwd = ?, last_seen = ?, host_pid = COALESCE(?, host_pid),"
        " host_key = CASE WHEN ? IS NULL THEN host_key ELSE ? END"
        " WHERE pid = ? AND lane = ?",
        (agent, label, key, cwd, stamp, host_pid, host_pid, host_key, pid, lane),
    )
    if cur.rowcount == 0:
        # `seq`, not the timestamp, is what orders a session's names. Stored
        # times have second precision, so two claims inside one second are
        # indistinguishable by time and the sort would fall back to the lane
        # TEXT - making a rapid rename advertise whichever name happens to sort
        # last. A per-process counter cannot tie.
        conn.execute(
            "INSERT INTO agent_sessions (pid, lane, agent, label, process_key, cwd,"
            " started_at, last_seen, claimed_at, host_pid, host_key, seq)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            " (SELECT COALESCE(MAX(seq), 0) + 1 FROM agent_sessions WHERE pid = ?))",
            (
                pid,
                lane,
                agent,
                label,
                key,
                cwd,
                stamp,
                stamp,
                stamp,
                host_pid,
                host_key,
                pid,
            ),
        )


def _refusal(conn, lane: str, pid: int) -> dict | None:
    """One ownership rule; acquisition reserves the writer before evaluating it."""
    rows = list(conn.execute("SELECT * FROM agent_sessions WHERE lane = ?", (lane,)))
    # The legacy table too. `holders` consults both, and `claim` asking
    # a narrower question than the thing that reports the answer is how
    # a lane reads as held everywhere except at the moment somebody
    # takes it.
    rows += _legacy_holders(conn, lane)
    existing = [_row_to_dict(r) for r in rows if r["pid"] != pid and _is_live(r)]
    if existing:
        held_by = ", ".join(
            f"pid {h['pid']} ({h['liveness']}, {h['cwd']})" for h in existing
        )
        unsure = [h for h in existing if h["liveness"] != "alive"]
        return {
            "ok": False,
            "error": (
                f"lane {lane!r} is already held by {held_by}. "
                + (
                    "That process could not be probed, so this refuses "
                    "rather than risk taking a name somebody is still "
                    "reading. "
                    if unsure
                    else ""
                )
                + "Pick another label, or let that session exit first."
            ),
            "lane": lane,
            "held_by": existing,
        }
    # No REGISTERED holder is not the same as no holder. Before taking
    # a name on the strength of an empty table, look for work that is
    # still owed to it and still owned by something alive.
    outstanding = [j for j in _live_work(conn, lane) if j["owner_pid"] != pid]
    if outstanding:
        which = ", ".join(
            f"{j['job_id']} ({j['state']}, owner pid {j['owner_pid']})"
            for j in outstanding
        )
        return {
            "ok": False,
            "error": (
                f"lane {lane!r} has no registered holder, but unfinished "
                f"work is still addressed to it: {which}. Something is "
                "consuming this name without being registered - an older "
                "hardline, or one whose registration failed. Refusing "
                "rather than splitting its mail."
            ),
            "lane": lane,
            "outstanding_jobs": outstanding,
        }
    return None


def _acquire(
    *,
    agent,
    lanes,
    label,
    primary,
    pid,
    cwd,
    db_path,
    now_fn,
    atomic: bool,
    host_pid,
    host_key,
) -> dict:
    pid = os.getpid() if pid is None else pid
    requested = tuple(dict.fromkeys(lanes))
    if host_pid is not None and instance_state(host_pid, host_key) == DEAD:
        return {
            "ok": False,
            "error": "launching host exited or its PID was reused",
            "lanes": [],
            "contested": list(requested),
        }
    stamp = _iso(now_fn())
    key = current_identity()[1] if pid == os.getpid() else process_key(pid)
    accepted, refused = [], []
    inherited = 0
    with closing(_connect(_resolve_db(db_path))) as conn:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            for lane in requested:
                refusal = _refusal(conn, lane, pid)
                if refusal:
                    if atomic:
                        conn.rollback()
                        return refusal
                    refused.append(lane)
                    continue
                if not accepted and key is not None:
                    conn.execute(
                        "DELETE FROM agent_sessions WHERE pid = ? AND process_key IS NOT ?",
                        (pid, key),
                    )
                if lane == primary:
                    inherited = _unread_for(conn, lane)
                conn.execute(
                    "DELETE FROM agent_sessions WHERE lane = ? AND pid != ?",
                    (lane, pid),
                )
                _upsert(
                    conn,
                    pid=pid,
                    lane=lane,
                    agent=agent,
                    label=label if lane == primary else None,
                    key=key,
                    cwd=cwd if cwd is not None else str(Path.cwd()),
                    stamp=stamp,
                    host_pid=host_pid,
                    host_key=host_key,
                )
                accepted.append(lane)
    return {
        "ok": True,
        "agent": agent,
        "label": label,
        "pid": pid,
        "lane": accepted[-1] if accepted else None,
        "lanes": accepted,
        "contested": refused,
        "inherited_unread": inherited,
    }


def register(
    *,
    agent: str,
    lane: Optional[str] = None,
    lanes: Optional[Iterable[str]] = None,
    label: Optional[str] = None,
    pid: Optional[int] = None,
    cwd: Optional[str] = None,
    host_pid: Optional[int] = None,
    host_key: Optional[str] = None,
    db_path: Optional[Path] = None,
    now_fn: Callable[[], datetime] = _default_now,
) -> dict:
    """Acquire or refresh each requested lane; report contested ones separately.

    Ownership checks and writes share one transaction with explicit claims.
    Refreshes are additive: a stale announcement cannot remove a newer claim.
    """
    requested = list(lanes or ()) + ([lane] if lane else [])
    return _acquire(
        agent=agent,
        lanes=requested,
        label=label,
        primary=lane or (requested[-1] if requested else None),
        pid=pid,
        cwd=cwd,
        db_path=db_path,
        now_fn=now_fn,
        host_pid=host_pid,
        host_key=host_key,
        atomic=False,
    )


def granted(
    lanes: Iterable[str],
    *,
    db_path: Optional[Path] = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[str, ...]:
    """Return only this process's uncontested, durable lane grants."""
    if conn is None:
        with closing(_connect(_resolve_db(db_path))) as snapshot:
            snapshot.execute("BEGIN")
            return granted(lanes, conn=snapshot)
    pid, key = current_identity()
    owned = []
    for lane in lanes:
        if _refusal(conn, lane, pid):
            continue
        rows = list(
            conn.execute("SELECT * FROM agent_sessions WHERE lane = ?", (lane,))
        )
        rows += _legacy_holders(conn, lane)
        if any(
            r["pid"] == pid and r["process_key"] == key and _is_live(r) for r in rows
        ):
            owned.append(lane)
    return tuple(owned)


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
        sql = "SELECT * FROM agent_sessions"
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

    Normally zero or one. Older revisions can leave conflicting live rows,
    so callers receive a list and can report the conflict.
    """
    db_path = _resolve_db(db_path)
    with closing(_connect(db_path)) as conn:
        rows = list(
            conn.execute("SELECT * FROM agent_sessions WHERE lane = ?", (lane,))
        )
        rows += _legacy_holders(conn, lane)
        return [_row_to_dict(r) for r in rows if _is_live(r)]


def _legacy_holders(conn: sqlite3.Connection, lane: str) -> list:
    """Rows for ``lane`` in the pre-rename `sessions` table, if it is there.

    Renaming the table removed a migration that could destroy another
    process's registrations, and bought a split-brain in exchange: a process on
    older code writes `sessions` and reads only `sessions`, while this one uses
    `agent_sessions`. Neither cohort could see the other, so each could hand
    out a lane the other was holding - which is the failure the registry
    exists to prevent, reintroduced by the fix for a different one.

    Reading the old table for OWNERSHIP questions closes this half of it. The
    other half cannot be closed from here: an older process will not consult
    `agent_sessions` no matter what this one writes.
    """
    try:
        return list(conn.execute("SELECT * FROM sessions WHERE lane = ?", (lane,)))
    except sqlite3.Error:
        return []  # no legacy table, which is the normal case after a while


def claim(
    *,
    agent: str,
    label: str,
    lanes: Iterable[str] = (),
    pid: Optional[int] = None,
    cwd: Optional[str] = None,
    host_pid: Optional[int] = None,
    host_key: Optional[str] = None,
    db_path: Optional[Path] = None,
    now_fn: Callable[[], datetime] = _default_now,
) -> dict:
    """Acquire the named role and retained lanes together, or change nothing.

    A successful claimant inherits unread mail already addressed to the role.
    Live or unprobeable holders and outstanding work prevent a takeover.
    """
    lane = f"{agent}:{label}"
    return _acquire(
        agent=agent,
        lanes=[*lanes, lane],
        label=label,
        primary=lane,
        pid=pid,
        cwd=cwd,
        db_path=db_path,
        now_fn=now_fn,
        host_pid=host_pid,
        host_key=host_key,
        atomic=True,
    )


def unregister(*, pid: Optional[int] = None, db_path: Optional[Path] = None) -> bool:
    """Remove every row for this process. Returns whether any were there.

    Not required for correctness - a vanished process is pruned on the next
    read either way - but a clean shutdown that says so keeps the registry
    honest between a session ending and anyone next looking.
    """
    db_path = _resolve_db(db_path)
    pid = os.getpid() if pid is None else pid
    with closing(_connect(db_path)) as conn:
        with conn:
            cur = conn.execute("DELETE FROM agent_sessions WHERE pid = ?", (pid,))
        return cur.rowcount > 0
