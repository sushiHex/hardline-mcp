"""A read-only audit of one store: which code still runs against it, and who
consumed each job's result.

Two questions the docs leave to evidence (docs/architecture.md, "Stable
instance addresses"): has "recorded provenance shows misdelivery" fired, and
does any process predating ``agent_sessions`` remain? Neither gets a verdict
here. The report shows what the records establish and names what they cannot:
a process that never registers is invisible, and a result consumed by another
process is as likely a relaunch, a move to the background or an inherited role
as a misdelivery.

Read-only: opened ``mode=ro``, never through the initializing
``mailbox._connect``; nothing is pruned or created. Every table is read in one
snapshot that is closed before any process is probed, so a slow probe never
holds a reader against the store's writers and checkpoints. Columns are named,
so one that is missing makes its section ``unavailable``, never zero.

Usage: hardline-mcp report [--db PATH]
"""

from __future__ import annotations

import argparse
import functools
import json
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .jobs import ACTIVE_STATES
from .mailbox import _iso, _resolve_db
from .procid import DEAD, instance_state
from .sessions import _is_live, host_state

SAMPLE = 20  # other-instance notices shown, newest first; the count is whole

NOTES = [
    "revision 'unrecorded': code older than revision recording, or no identity token",
    "a process that never registers is invisible: no live legacy process is not proof none remain",
    "other_instance: consumed by another process (a relaunch, a /bg move, an inherited role, "
    "or a misdelivery), not a verdict; notices are matched by job id, sender and recipient, "
    "which a hand-sent copy can imitate",
    "tokenless: records with no creation token not shown dead, whose pid may be their own "
    "process or a stranger's; zero is this run's finding, not a guarantee",
]

# Each section's rows, read in the snapshot. None marks a table current code
# creates on use and this store never had.
_READS = {
    "sessions": "SELECT pid, process_key, lane, host_pid, host_key FROM agent_sessions ORDER BY pid, seq",
    "revisions": "SELECT pid, process_key, revision FROM process_revisions",
    "legacy": "SELECT DISTINCT pid, process_key FROM sessions",
    "jobs": "SELECT job_id, agent, requester, state, owner_pid, owner_key, child_pid, child_key FROM jobs",
    "acks": "SELECT message_id, pid, process_key, conversation FROM message_acks",
    # instr, not LIKE: LIKE reads `_` as a wildcard. Parsed and checked below.
    "notices": "SELECT id, sender, recipient, body, created_at, acked_at FROM messages"
    " WHERE acked_at IS NOT NULL AND instr(body, '\"job_finished\"') > 0 ORDER BY id DESC",
    "unrecorded": "SELECT COUNT(*) AS n FROM messages m WHERE acked_at IS NOT NULL"
    " AND NOT EXISTS (SELECT 1 FROM message_acks a WHERE a.message_id = m.id)",
}
_CREATED_ON_USE = {
    "revisions": "process_revisions",
    "legacy": "sessions",
    "acks": "message_acks",
    "unrecorded": "message_acks",
}


def report(db_path: Optional[Path] = None) -> dict:
    """The audit of the store at ``db_path`` (resolved as the servers resolve it)."""
    db = _resolve_db(db_path).resolve()
    db.stat()  # a missing store is an error, never an empty report
    read = _snapshot(db)
    probe = functools.lru_cache(maxsize=None)(instance_state)  # once per identity
    return {
        "generated_at": _iso(datetime.now(timezone.utc)),
        "db_path": str(db),
        "processes": _section(_processes, read, probe, "sessions", "revisions"),
        "legacy_sessions": _section(_legacy_sessions, read, probe, "legacy"),
        "job_notices": _section(_job_notices, read, probe, "jobs", "acks", "notices", "unrecorded"),
        "tokenless": _section(_tokenless, read, probe, "sessions", "jobs"),
        "notes": NOTES,
    }


def _snapshot(db: Path) -> dict:
    """Every read, in one read transaction, as plain rows or the error."""
    tables_sql = "SELECT name FROM sqlite_master WHERE type = 'table'"
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        tables = {r[0] for r in conn.execute(tables_sql)}
        read = {}
        for name, sql in _READS.items():
            table = _CREATED_ON_USE.get(name)
            if table and table not in tables:
                read[name] = None
                continue
            try:
                read[name] = [dict(r) for r in conn.execute(sql)]
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc) and "no such column" not in str(exc):
                    raise
                read[name] = exc
        return read


def _section(build: Callable, read: dict, probe: Callable, *names: str) -> dict:
    failed = [read[n] for n in names if isinstance(read[n], Exception)]
    if failed:
        return {"unavailable": str(failed[0])}
    return build(*(read[n] for n in names), probe=probe)


def _processes(sessions: list, revisions: Optional[list], *, probe: Callable) -> dict:
    """Registered server processes, one entry per identity."""
    by_identity: dict[tuple, list] = {}
    for row in sessions:
        by_identity.setdefault((row["pid"], row["process_key"]), []).append(row)
    recorded = {(r["pid"], r["process_key"]): r["revision"] for r in revisions or ()}
    registered = [
        {
            "pid": pid,
            "process_key": key,
            "state": probe(pid, key),
            "host_states": sorted({host_state(r, probe) for r in rows}),
            "revision": recorded.get((pid, key)),
            "lanes": [r["lane"] for r in rows],
        }
        for (pid, key), rows in by_identity.items()
    ]
    running = [p for p in registered if p["state"] != DEAD]
    return {
        "revisions": dict(Counter(p["revision"] or "unrecorded" for p in running)),
        "states": dict(Counter(p["state"] for p in registered)),
        "registered": registered,
    }


def _legacy_sessions(legacy: Optional[list], *, probe: Callable) -> dict:
    """The pre-``agent_sessions`` registry, which only older revisions write."""
    if legacy is None:
        return {"table": False}
    return {
        "table": True,
        "processes": dict(Counter(probe(r["pid"], r["process_key"]) for r in legacy)),
    }


def _job_id(body: str) -> Optional[str]:
    """The job a notice body names, if it is one: any message body is free text."""
    try:
        notice = json.loads(body)
    except ValueError:
        return None
    if not isinstance(notice, dict) or notice.get("type") != "job_finished":
        return None
    job_id = notice.get("job_id")
    return job_id if isinstance(job_id, str) else None


def _job_notices(
    jobs: list, acks: Optional[list], notices: list, unrecorded: Optional[list], *, probe: Callable
) -> dict:
    """Acked job notices: consumed by the process that dispatched the job, by
    another, or by one nothing recorded (an older revision, or no token)."""
    by_job = {j["job_id"]: j for j in jobs}
    by_message = {a["message_id"]: a for a in acks or ()}
    counts: Counter = Counter(same_instance=0, other_instance=0, unknown=0)
    sample = []
    for notice in notices:
        job = by_job.get(_job_id(notice["body"]))
        if job is None or (notice["sender"], notice["recipient"]) != (job["agent"], job["requester"]):
            continue
        ack = by_message.get(notice["id"])
        if ack is None or ack["process_key"] is None or job["owner_key"] is None:
            counts["unknown"] += 1
        elif (ack["pid"], ack["process_key"]) == (job["owner_pid"], job["owner_key"]):
            counts["same_instance"] += 1
        else:
            counts["other_instance"] += 1
            if len(sample) < SAMPLE:
                sample.append(
                    {
                        "message_id": notice["id"],
                        "job_id": job["job_id"],
                        "recipient": notice["recipient"],
                        "finished_at": notice["created_at"],
                        "acked_at": notice["acked_at"],
                        "dispatched_by": {"pid": job["owner_pid"], "process_key": job["owner_key"]},
                        "acked_by": ack,
                    }
                )
    return {
        **counts,
        "other_instance_sample": sample,
        "acks_recorded": acks is not None,
        # Every kind of message, so it overlaps "unknown" above; not additive.
        "acked_messages_without_record": unrecorded[0]["n"] if unrecorded is not None else None,
    }


def _tokenless(sessions: list, jobs: list, *, probe: Callable) -> dict:
    """Records that still act on a pid without a creation token (TODO.md,
    "Known limits"), judged as their consumers judge them: a stranger on a
    reused pid passes for the process, so a registration keeps its lanes held
    (a server honoured on liveness alone, a host held as unknown), a job looks
    owned, and a cancel kills a child's tree unverified. Those shown dead act
    on nothing. Counts for this run only: new ones appear when a token will
    not read."""
    active = [j for j in jobs if j["state"] in ACTIVE_STATES]
    return {
        "processes": len(
            {
                (r["pid"], r["process_key"])
                for r in sessions
                if (r["process_key"] is None or r["host_pid"] is not None and r["host_key"] is None)
                and _is_live(r, probe)
            }
        ),
        "unfinished_jobs": sum(
            1 for j in active if j["owner_key"] is None and probe(j["owner_pid"], None) != DEAD
        ),
        "children": sum(
            1
            for j in active
            if j["child_pid"] and j["child_key"] is None and probe(j["child_pid"], None) != DEAD
        ),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="hardline-mcp report", description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, help="store to audit (default: as the servers resolve it)")
    args = parser.parse_args(argv)
    try:
        audit = report(args.db)
    except FileNotFoundError as exc:
        print(f"hardline-mcp report: no store at {exc.filename}", file=sys.stderr)
        return 1
    print(json.dumps(audit, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
