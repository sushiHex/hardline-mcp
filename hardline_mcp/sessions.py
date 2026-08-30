"""Which sessions exist, what they are called, and which are still alive.

Identity was the one thing in hardline with no durable record. A job survives a
restart; a message survives a restart; the SESSION holding a lane existed only
as a function of one process's environment. Three consequences, all observed in
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

Scope is deliberately one question - who is alive NOW. The history of who was
alive belongs to the messages and jobs tables, which already keep it; a
registry that also tried to be a history would be two things, and the dead rows
it kept would be exactly the stale destinations this exists to stop reporting.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

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
    """
    rows = conn.execute("SELECT pid, process_key FROM sessions").fetchall()
    dead = [r["pid"] for r in rows if not _is_live(r)]
    if not dead:
        return
    marks = ", ".join("?" for _ in dead)
    with conn:
        conn.execute(f"DELETE FROM sessions WHERE pid IN ({marks})", tuple(dead))


def register(
    *,
    agent: str,
    lane: str,
    label: Optional[str] = None,
    pid: Optional[int] = None,
    cwd: Optional[str] = None,
    db_path: Optional[Path] = None,
    now_fn: Callable[[], datetime] = _default_now,
) -> dict:
    """Record (or refresh) this process's session row. Idempotent.

    UPDATE-then-INSERT rather than INSERT OR REPLACE, so ``started_at`` keeps
    saying when the session began instead of resetting on every heartbeat, and
    rather than ON CONFLICT upsert, which needs SQLite 3.24+ - the store is
    otherwise readable by older builds and one statement is not worth the floor.

    Only ever writes the row whose pid is this process, so the cross-process
    case cannot collide. A row left behind by a dead session that happened to
    hold this pid is correctly overwritten: the pid is ours now.
    """
    db_path = _resolve_db(db_path)
    pid = os.getpid() if pid is None else pid
    stamp = _iso(now_fn())
    key = process_key(pid)
    cwd = cwd if cwd is not None else str(Path.cwd())
    with closing(_connect(db_path)) as conn:
        with conn:
            cur = conn.execute(
                "UPDATE sessions SET agent = ?, lane = ?, label = ?,"
                " process_key = ?, cwd = ?, last_seen = ? WHERE pid = ?",
                (agent, lane, label, key, cwd, stamp, pid),
            )
            if cur.rowcount == 0:
                conn.execute(
                    "INSERT INTO sessions (pid, agent, lane, label, process_key,"
                    " cwd, started_at, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (pid, agent, lane, label, key, cwd, stamp, stamp),
                )
    return {"lane": lane, "agent": agent, "label": label, "pid": pid}


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
        sql += " ORDER BY started_at ASC, pid ASC"
        rows = conn.execute(sql, params).fetchall()
        # Re-check rather than trust the prune: a session can exit between the
        # DELETE and the SELECT, and reporting a destination that has just gone
        # is the failure this module exists to remove.
        return [_row_to_dict(r) for r in rows if _is_live(r)]


def holders(lane: str, *, db_path: Optional[Path] = None) -> list[dict]:
    """Live sessions currently holding ``lane`` (a fully-qualified recipient).

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
    pid: Optional[int] = None,
    cwd: Optional[str] = None,
    db_path: Optional[Path] = None,
    now_fn: Callable[[], datetime] = _default_now,
) -> dict:
    """Take ``agent:label`` as this process's lane, unless someone live holds it.

    Returns ``{"ok": True, "lane": ...}`` or ``{"ok": False, "error": ...}``.

    A name collision is resolved by liveness rather than by seniority: a dead
    holder's claim means nothing, and refusing on its behalf would make a label
    unusable forever after the session that used it crashed. A LIVE holder is
    refused and named, because silently moving a name would redirect mail
    somebody is still waiting on.
    """
    pid = os.getpid() if pid is None else pid
    lane = f"{agent}:{label}"
    existing = [h for h in holders(lane, db_path=db_path) if h["pid"] != pid]
    if existing:
        held_by = ", ".join(f"pid {h['pid']} ({h['cwd']})" for h in existing)
        return {
            "ok": False,
            "error": (
                f"lane {lane!r} is already held by a live session: {held_by}. "
                "Pick another label, or let that session exit first."
            ),
            "lane": lane,
            "held_by": existing,
        }
    register(
        agent=agent,
        lane=lane,
        label=label,
        pid=pid,
        cwd=cwd,
        db_path=db_path,
        now_fn=now_fn,
    )
    return {"ok": True, "lane": lane, "label": label, "pid": pid}


def unregister(
    *, pid: Optional[int] = None, db_path: Optional[Path] = None
) -> bool:
    """Remove this process's row. Returns whether one was there.

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
