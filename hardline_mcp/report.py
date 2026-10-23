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

from .delivery import GRACE, NOT_TOP_LEVEL
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
    "other_instance_by_suffix: whether the lane's eight-character suffix equals the acking "
    "conversation's first eight; a correlation, not provenance: an explicit label can carry "
    "any suffix, eight characters are not the whole id, and the conversation is the acking "
    "server's CLAUDE_CODE_SESSION_ID, so a Codex ack normally records none",
    "tokenless: records with no creation token not shown dead, whose pid may be their own "
    "process or a stranger's; zero is this run's finding, not a guarantee",
    "codex_routes: facts each Codex wake wrote about itself; 'ambiguous' is a steer that may or may "
    "not have landed, counted apart from steered; a dead process's row stays until pruned",
    "codex_lane_servers_without_row: registered processes not shown dead holding a codex: lane "
    "with no route row - older code, a wake not yet called (current code writes a row at the "
    "first call: last_route 'not_top_level' for a subagent's server, or a last_route_reason "
    "'unpinned: ...' naming the keys of a call that pinned nothing), a failed write, or not a "
    "Codex client at all; their notices are in no total. A "
    "row's zeros mean no counted route outcome, not no notice: a failed queue is in no count "
    "(current code records it as last_route 'failed'; older code recorded nothing)",
    "unread_mail: counts only, nothing is acked or deleted; a registered holder is a session row "
    "not shown dead, not a consumer known to be reading",
    "codex_routes 'failed': a codex queue that reported failure, its error in last_route_reason, "
    "uncounted; current code sends such a notice again later",
    "stalled_wakes: live servers with mail waiting past the delivery grace: 'stalled', a "
    "notice's receipt overdue (the notice may have failed, or the session is busy or ignoring "
    "it); 'unannounced', nothing owed yet mail newer than the last notice (the wake is not "
    "announcing); 'not_top_level', a Codex server called only from a thread Codex names as "
    "not top-level (a subagent's), whose wake is not armed; 'unwatched', no wake facts at all "
    "(older code, a "
    "Codex server that never pinned a thread and has not been called, or no wake). Evidence, "
    "not proof; since is when it was owed, or the oldest such arrival",
]

# Each section's rows, read in the snapshot. None marks a table current code
# creates on use and this store never had.
_READS = {
    "sessions": "SELECT pid, process_key, lane, host_pid, host_key, cwd, started_at"
    " FROM agent_sessions ORDER BY pid, seq",
    "revisions": "SELECT pid, process_key, revision FROM process_revisions",
    "legacy": "SELECT DISTINCT pid, process_key FROM sessions",
    "jobs": "SELECT job_id, agent, requester, state, owner_pid, owner_key, child_pid, child_key FROM jobs",
    "acks": "SELECT message_id, pid, process_key, conversation FROM message_acks",
    # instr, not LIKE: LIKE reads `_` as a wildcard. Parsed and checked below.
    "notices": "SELECT id, sender, recipient, body, created_at, acked_at FROM messages"
    " WHERE acked_at IS NOT NULL AND instr(body, '\"job_finished\"') > 0 ORDER BY id DESC",
    "unrecorded": "SELECT COUNT(*) AS n FROM messages m WHERE acked_at IS NOT NULL"
    " AND NOT EXISTS (SELECT 1 FROM message_acks a WHERE a.message_id = m.id)",
    # Counts only: no body, sender or id leaves the store. MIN works on text
    # because every writer stores UTC ISO-8601 (mailbox._iso of a UTC clock).
    "unread": "SELECT recipient, COUNT(*) AS n, MIN(created_at) AS oldest,"
    " MAX(created_at) AS newest FROM messages WHERE acked_at IS NULL GROUP BY recipient",
    # Every row: a wake writes one when it pins its thread, before any notice,
    # or at a first call that pinned nothing, saying why.
    "routes": "SELECT pid, process_key, last_route, last_route_reason, steered, queued, ambiguous"
    " FROM codex_routes ORDER BY pid",
    "pushes": "SELECT pid, process_key, last_push_at, oldest_unreceipted_at FROM push_delivery"
    " ORDER BY pid",
    # Arrival times only, compared with each wake's last notice; never shown.
    "waiting": "SELECT recipient, created_at FROM messages WHERE acked_at IS NULL",
}
_CREATED_ON_USE = {
    "pushes": "push_delivery",
    "routes": "codex_routes",
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
    now = datetime.now(timezone.utc)
    return {
        "generated_at": _iso(now),
        "db_path": str(db),
        "processes": _section(_processes, read, probe, "sessions", "revisions"),
        "legacy_sessions": _section(_legacy_sessions, read, probe, "legacy"),
        "job_notices": _section(_job_notices, read, probe, "jobs", "acks", "notices", "unrecorded"),
        "tokenless": _section(_tokenless, read, probe, "sessions", "jobs"),
        "unread_mail": _section(
            functools.partial(_unread_mail, now=now), read, probe, "unread", "sessions"
        ),
        "codex_routes": _section(
            functools.partial(_codex_routes, sessions=read["sessions"]), read, probe, "routes"
        ),
        "stalled_wakes": _section(
            functools.partial(_stalled_wakes, now=now),
            read, probe, "pushes", "routes", "sessions", "waiting",
        ),
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
    """Registered server processes, one entry per identity. Where each came
    from too - its host, working directory and start - since a row is pruned
    once its process is gone, and a saved report is then all that is left."""
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
            "host_pids": sorted({r["host_pid"] for r in rows if r["host_pid"] is not None}),
            "cwds": sorted({r["cwd"] for r in rows if r["cwd"]}),
            "started_at": min((r["started_at"] for r in rows if r["started_at"]), default=None),
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


SUFFIX_FACTS = ("suffix_equal", "suffix_differs", "no_suffix", "conversation_unrecorded")


def _suffix_fact(lane: str, conversation: Optional[str]) -> str:
    """Whether the lane a notice went to ends in the first eight characters of
    the conversation that acked it, as a label derived for that conversation
    would (``adapters``). A correlation, never provenance: an explicit label
    can carry any suffix, and eight characters are not the whole id."""
    if not conversation:
        return "conversation_unrecorded"
    suffix = lane.rpartition(".")[2] if "." in lane else ""
    if len(suffix) != 8:
        return "no_suffix"
    return "suffix_equal" if conversation[:8] == suffix else "suffix_differs"


def _job_notices(
    jobs: list, acks: Optional[list], notices: list, unrecorded: Optional[list], *, probe: Callable
) -> dict:
    """Acked job notices: consumed by the process that dispatched the job, by
    another, or by one nothing recorded (an older revision, or no token).
    Other-instance notices are also grouped by ``_suffix_fact``, each group
    with its own newest ids, so no group can crowd another out of view."""
    by_job = {j["job_id"]: j for j in jobs}
    by_message = {a["message_id"]: a for a in acks or ()}
    counts: Counter = Counter(same_instance=0, other_instance=0, unknown=0)
    by_suffix: dict = {fact: {"count": 0, "message_ids": []} for fact in SUFFIX_FACTS}
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
            fact = _suffix_fact(notice["recipient"], ack["conversation"])
            group = by_suffix[fact]
            group["count"] += 1
            if len(group["message_ids"]) < SAMPLE:
                group["message_ids"].append(notice["id"])
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
                        "suffix": fact,
                    }
                )
    return {
        **counts,
        "other_instance_by_suffix": by_suffix,
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
        # A cancel finds a dead owner's job lost and kills nothing.
        "children": sum(
            1
            for j in active
            if j["child_pid"] and j["child_key"] is None and probe(j["child_pid"], None) != DEAD
            and probe(j["owner_pid"], j["owner_key"]) != DEAD
        ),
    }


def _codex_routes(
    routes: Optional[list], *, sessions: "list | Exception", probe: Callable
) -> dict:
    """Which way each Codex wake sent its notices: steered into the turn,
    queued (with the last reason), or ambiguous. Totals are over the rows
    shown, so the registered processes holding a ``codex:`` lane that wrote
    none are listed beside them: without that, zero totals read the same as
    nothing recording. The registry is enrichment here: a failed read of it
    leaves the rows standing."""
    rows = [{**r, "state": probe(r["pid"], r["process_key"])} for r in routes or ()]
    return {
        **{k: sum(r[k] or 0 for r in rows) for k in ("steered", "queued", "ambiguous")},
        "table": routes is not None,
        "codex_lane_servers_without_row": _without_row(rows, sessions, probe),
        "processes": rows,
    }


def _without_row(rows: list, sessions: "list | Exception", probe: Callable) -> "list | dict":
    """Registered processes not shown dead that hold a ``codex:`` lane and have
    no route row, with their lanes. A lane prefix, not the client's identity."""
    if isinstance(sessions, Exception):
        return {"unavailable": str(sessions)}
    recorded = {(r["pid"], r["process_key"]) for r in rows}
    lanes: dict[tuple, list] = {}
    for s in sessions:
        identity = (s["pid"], s["process_key"])
        if s["lane"].startswith("codex:") and identity not in recorded and _is_live(s, probe):
            lanes.setdefault(identity, []).append(s["lane"])
    return [{"pid": p, "process_key": k, "lanes": v} for (p, k), v in lanes.items()]


def _stalled_wakes(
    pushes: Optional[list],
    routes: Optional[list],
    sessions: list,
    waiting: list,
    *,
    now: datetime,
    probe: Callable,
) -> dict:
    """Live servers whose registered lanes hold mail their wake has not got
    read, each past ``delivery.GRACE``, in one of four ways:

    - ``stalled``: a notice has owed its receipt that long (as
      ``delivery.derive`` reads ``unreceipted``) while the lanes hold mail;
    - ``unannounced``: nothing is owed, yet mail arrived after the last notice,
      or none was ever sent - a free wake announces new mail within a poll;
    - ``not_top_level``: a Codex server called only from a thread Codex names
      as not top-level (a subagent's), whose wake is not armed (#109);
    - ``unwatched``: no wake facts at all, so no notice can be shown either way.

    Joined on pid and creation token, against every unread message, not the
    sampled ``unread_mail`` lists. Evidence, not a verdict: a session may be
    busy or ignoring notices, a lane may be claimed by older code, and an
    arrival stamped just before a notice was sent but committed after its scan
    is counted as announced."""
    cutoff = now - GRACE
    arrivals: dict[str, list[datetime]] = {}
    for r in waiting:
        at = _parse(r["created_at"])
        if at is not None:
            arrivals.setdefault(r["recipient"], []).append(at)
    lanes: dict[tuple, list] = {}
    for s in sessions:
        if _is_live(s, probe):
            lanes.setdefault((s["pid"], s["process_key"]), []).append(s["lane"])
    facts = {(p["pid"], p["process_key"]): p for p in pushes or ()}
    not_top = {(r["pid"], r["process_key"]) for r in routes or () if r["last_route"] == NOT_TOP_LEVEL}
    found: dict[str, list] = {"stalled": [], "unannounced": [], "not_top_level": [], "unwatched": []}
    for (pid, key), held in lanes.items():
        p = facts.get((pid, key))
        owed = _parse(p["oldest_unreceipted_at"]) if p else None
        since = _parse(p["last_push_at"]) if p else None
        if owed is not None and owed > cutoff:
            continue  # a notice still within its grace
        kind = (
            "stalled" if owed else "unannounced" if p
            else "not_top_level" if (pid, key) in not_top else "unwatched"
        )
        # Stalled, the whole backlog waits on the receipt; otherwise, mail past
        # the grace that arrived after the last notice could have named it.
        unread = {
            lane: ats
            for lane in held
            if (ats := [
                at for at in arrivals.get(lane, ())
                if kind == "stalled" or (at <= cutoff and (since is None or at > since))
            ])
        }
        if not unread:
            continue
        oldest = owed if kind == "stalled" else min(min(ats) for ats in unread.values())
        found[kind].append({
            "pid": pid,
            "process_key": key,
            "since": _iso(oldest),  # owed since, or the oldest such mail's arrival
            "overdue_minutes": int((now - oldest).total_seconds() // 60),
            "unread": {lane: len(ats) for lane, ats in unread.items()},
        })
    with_facts = sum(1 for identity in lanes if identity in facts)
    return {
        "table": pushes is not None,
        "servers_with_facts": with_facts,  # live registered servers whose wake writes facts
        "servers_without_facts": len(lanes) - with_facts,  # older code, or no wake
        **found,
    }


def _parse(stamp: Optional[str]) -> Optional[datetime]:
    """A stored UTC timestamp, or None if absent or unparsable."""
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return then if then.tzinfo else then.replace(tzinfo=timezone.utc)


def _age_days(created_at: str, now: datetime) -> Optional[int]:
    """Whole days from a stored timestamp to ``now``; None if it does not parse."""
    try:
        then = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max((now - then).days, 0)


def _unread_mail(unread: list, sessions: list, *, now: datetime, probe: Callable) -> dict:
    """Unread messages per recipient, counted and never touched.

    ``holder`` is what ``agent_sessions`` says of a lane: ``registered``, one
    row not shown dead (UNKNOWN counts, as when a claim is judged);
    ``contested``, several, of which ``sessions.granted`` grants none; ``dead``,
    rows all shown dead; or ``none``. A registration, not a consumer proven
    reading: a process that never registers is invisible. A bare name has no
    lane to hold - one shared copy, consumable by any session - so it is listed
    apart. Lanes with a live registration sort first, being the case to act
    on, oldest unread first: the longest wait. The rest follow newest arrival
    first, so mail stranded this week is not crowded out by a months-old
    backlog. Each list shows SAMPLE entries; the totals are whole."""
    live: dict[str, int] = {}  # lane -> rows not shown dead
    for row in sessions:
        live[row["lane"]] = live.get(row["lane"], 0) + bool(_is_live(row, probe))

    def entry(row: dict) -> dict:
        return {
            "recipient": row["recipient"],
            "unread": row["n"],
            "oldest": row["oldest"],
            "oldest_age_days": _age_days(row["oldest"], now),
            "newest": row["newest"],
        }

    lanes, bare = [], []
    for row in sorted(unread, key=lambda r: (r["oldest"], r["recipient"])):
        if ":" not in row["recipient"]:
            bare.append(entry(row))
            continue
        n = live.get(row["recipient"])
        holder = (
            "none" if n is None else "dead" if n == 0 else "registered" if n == 1 else "contested"
        )
        lanes.append({**entry(row), "holder": holder})
    registered = ("registered", "contested")
    held = [e for e in lanes if e["holder"] in registered]  # stable: oldest first
    unheld = sorted(
        (e for e in lanes if e["holder"] not in registered),
        key=lambda e: (e["newest"] or "", e["recipient"]), reverse=True,
    )
    lanes = held + unheld
    on_registered = sum(e["unread"] for e in lanes if e["holder"] in registered)
    other = sum(e["unread"] for e in lanes) - on_registered
    return {
        "total_unread": on_registered + other + sum(e["unread"] for e in bare),
        "on_registered_lanes": on_registered,
        "on_other_lanes": other,
        "on_bare_names": sum(e["unread"] for e in bare),
        "lanes": lanes[:SAMPLE],
        "lanes_not_shown": max(len(lanes) - SAMPLE, 0),
        "bare": bare[:SAMPLE],
        "bare_not_shown": max(len(bare) - SAMPLE, 0),
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
