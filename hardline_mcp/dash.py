"""A live, read-only view of one store for a human observer.

``hardline-mcp dash`` serves a single page on 127.0.0.1 that polls
``/api/state``: which lanes are held and how much mail waits on each, how busy
the last hour was, which jobs ran, and the latest traffic.

Metadata only. A message body, a job's request, result or error, and a
session's working directory are never selected, so nothing here can show them.
The store is read the way ``report`` reads it: ``mode=ro``, one snapshot closed
before any process is probed, a missing table or column makes its section
``unavailable`` and never zero. No row is written, pruned or created; SQLite
itself may still create its ``-wal``/``-shm`` sidecars for a reader. Anyone who
can open the store file can read more than this shows, so the page's guard is
against browsers (Host and Origin), not against local processes.

Usage: hardline-mcp dash [--db PATH] [--port N]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

from .jobs import ACTIVE_STATES
from .mailbox import _resolve_db
from .procid import ALIVE, DEAD, instance_state
from .sessions import _state

HOST = "127.0.0.1"
REFRESH = 2.0  # seconds: every tab shares one read, however many are open
BUDGET = 3.0  # seconds a read may run before it is abandoned
CHECK_EVERY = 10_000  # SQLite VM steps between looks at the clock
CLIP = 80 # characters of any stored name shown; a label is the user's text, not ours
DEFAULT_PORT = 8765
WINDOW = timedelta(hours=1)
RECENT = 15
JOBS_SHOWN = 40
LINKS_SHOWN = 30
PAGE = Path(__file__).with_name("dash.html")


def _parse(stamp: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else None
    except (AttributeError, ValueError):
        return None


def _ago(stamp: Optional[str], now: datetime) -> Optional[int]:
    moment = _parse(stamp)
    return None if moment is None else max(int((now - moment).total_seconds()), 0)


def _clip(text: Optional[str]) -> Optional[str]:
    return text if text is None or len(text) <= CLIP else text[: CLIP - 1] + "…"


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> "list | None":
    """Rows as dicts, or ``None`` when this store lacks the table or column."""
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc) or "no such column" in str(exc):
            return None
        raise


def _snapshot(db: Path, since: str) -> dict:
    """Every read, in one read transaction. Never a body, request, result or cwd."""
    marks = ", ".join("?" for _ in ACTIVE_STATES)
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        deadline = time.monotonic() + BUDGET
        conn.set_progress_handler(lambda: time.monotonic() > deadline, CHECK_EVERY)  # abort: a long reader pins the WAL
        conn.execute("PRAGMA temp_store = MEMORY")  # sorting and grouping must not touch the disk
        conn.execute("BEGIN")
        return {
            "sessions": _rows(
                conn,
                "SELECT pid, process_key, host_pid, host_key, lane, agent FROM agent_sessions",
            ),
            "unread": _rows(
                conn,
                "SELECT recipient, COUNT(*) AS n, MIN(created_at) AS oldest"
                " FROM messages WHERE acked_at IS NULL GROUP BY recipient",
            ),
            "traffic": _rows(conn, "SELECT created_at FROM messages WHERE created_at >= ?", (since,)),
            # Every name that sent or received anything this hour: the activity test for a line.
            "talkers": _rows(
                conn,
                "SELECT sender AS name FROM messages WHERE created_at >= ?"
                " UNION SELECT recipient FROM messages WHERE created_at >= ?",
                (since, since),
            ),
            # Who reached whom: counts per pair, never the messages themselves.
            "links": _rows(
                conn,
                "SELECT sender, recipient, COUNT(*) AS n, SUM(acked_at IS NULL) AS unread"
                " FROM messages WHERE created_at >= ? GROUP BY sender, recipient"
                " ORDER BY n DESC LIMIT ?",
                (since, LINKS_SHOWN),
            ),
            "recent": _rows(
                conn,
                "SELECT id, sender, recipient, created_at, acked_at, length(body) AS size"
                " FROM messages ORDER BY id DESC LIMIT ?",
                (RECENT,),
            ),
            "jobs": _rows(
                conn,
                "SELECT job_id, agent, requester, label, state, owner_pid, owner_key, child_pid,"
                " created_at, started_at, finished_at FROM jobs"
                f" WHERE state IN ({marks}) OR finished_at >= ?"
                " ORDER BY created_at DESC LIMIT ?",
                (*sorted(ACTIVE_STATES), since, JOBS_SHOWN),
            ),
        }


def state(
    db_path: Optional[Path] = None,
    *,
    now: Optional[datetime] = None,
    probe: Callable = instance_state,
) -> dict:
    """What the page draws. Ages are seconds before ``now``, so the browser's clock never matters."""
    db = _resolve_db(db_path).resolve()
    db.stat()  # a missing store is an error, never an empty page
    now = now or datetime.now(timezone.utc)
    read = _snapshot(db, (now - WINDOW).isoformat(timespec="seconds").replace("+00:00", "Z"))
    cache: dict = {}

    def seen(pid, key):  # once per identity per refresh
        if (pid, key) not in cache:
            cache[pid, key] = probe(pid, key)
        return cache[pid, key]

    unavailable = sorted(name for name, rows in read.items() if rows is None)
    unread = {r["recipient"]: r for r in read["unread"] or []}

    # A session whose process, or whose host, is the CLI a job spawned is an on-demand worker,
    # not a session someone is sitting in.
    spawned = {r["child_pid"] for r in read["jobs"] or [] if r["child_pid"]}
    held: dict = {}  # lane -> the best liveness any holder shows
    for row in read["sessions"] or []:
        verdict = _state(row, seen)  # the server and the host it is bound to, as sessions judges
        if verdict == DEAD:
            continue
        temp = row["pid"] in spawned or row["host_pid"] in spawned
        lane = held.setdefault(
            row["lane"], {"lane": row["lane"], "agent": row["agent"], "liveness": verdict, "temp": temp}
        )
        lane["temp"] = lane["temp"] and temp  # temporary only if every holder is
        if verdict == ALIVE:
            lane["liveness"] = ALIVE
    for recipient in unread:  # mail waiting for a name nobody holds is the thing to see
        held.setdefault(
            recipient,
            {
                "lane": recipient,
                "agent": recipient.split(":")[0],
                # A bare agent name is a shared mailbox any reader may drain.
                "liveness": "unheld" if ":" in recipient else "shared",
                "temp": False,
            },
        )
    # A line is active if it placed or took a call this hour, has calls waiting, or started a job.
    busy = {r["name"] for r in read["talkers"] or []} | {r["requester"] for r in read["jobs"] or []}
    lanes = []
    for lane in sorted(held.values(), key=lambda e: e["lane"]):
        waiting = unread.get(lane["lane"])
        lanes.append(
            {
                **lane,
                "active": bool(waiting) or lane["lane"] in busy,
                "lane": _clip(lane["lane"]),
                "agent": _clip(lane["agent"]),
                "unread":waiting["n"] if waiting else 0,
                "oldest_s": _ago(waiting["oldest"], now) if waiting else None,
            }
        )

    minutes = int(WINDOW.total_seconds() // 60)
    traffic = [0] * minutes
    for row in read["traffic"] or []:
        age = _ago(row["created_at"], now)
        if age is not None and age < minutes * 60:
            traffic[minutes - 1 - age // 60] += 1

    jobs = []
    for row in read["jobs"] or []:
        verdict = row["state"]
        if verdict in ACTIVE_STATES and seen(row["owner_pid"], row["owner_key"]) == DEAD:
            verdict = "lost"  # derived on read, as jobs.py does
        begun = row["started_at"] or row["created_at"]
        jobs.append(
            {
                "id": _clip(row["job_id"]),
                "agent": _clip(row["agent"]),
                "requester": _clip(row["requester"]),
                "label": _clip(row["label"]),
                "state": verdict,
                "start_s": _ago(begun, now),
                "end_s": _ago(row["finished_at"], now),
            }
        )

    recent = [
        {
            "id": r["id"],
            "from": _clip(r["sender"]),
            "to": _clip(r["recipient"]),
            "age_s": _ago(r["created_at"], now),
            "read": r["acked_at"] is not None,
            "size": r["size"],
        }
        for r in read["recent"] or []
    ]
    return {
        "now": now.isoformat(timespec="seconds"),
        "store": db.name,
        "window_s": int(WINDOW.total_seconds()),
        "lanes": lanes,
        "traffic": traffic,
        "temporary": sorted(_clip(l["lane"]) for l in lanes if l["temp"]),
        "links": [
            {"from": _clip(r["sender"]), "to": _clip(r["recipient"]), "n": r["n"], "unread": r["unread"]}
            for r in read["links"] or []
        ],
        "jobs": jobs,
        "recent": recent,
        "unavailable": unavailable,
    }


class _Shared:
    """One refresh at a time, reused for ``REFRESH`` seconds: opening more tabs
    adds no reads, and a slow read is never run twice at once."""

    def __init__(self, db_path: Optional[Path]):
        self._db, self._lock, self._at, self._body = db_path, threading.Lock(), -REFRESH, b""

    def body(self) -> bytes:
        with self._lock:
            if time.monotonic() - self._at >= REFRESH:
                self._body = json.dumps(state(self._db)).encode("utf-8")
                self._at = time.monotonic()
            return self._body


def _handler(db_path: Optional[Path], port: int) -> type:
    shared = _Shared(db_path)
    # A browser tab on another site can reach 127.0.0.1, and a rebound DNS name
    # reaches it with that name in Host. Only the address this server was
    # opened on is answered, and a cross-site Origin is refused outright.
    hosts = {f"{HOST}:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "hardline-dash"
        timeout = 10  # an idle pre-opened socket must not hold a thread forever

        def log_message(self, *args) -> None:  # the terminal belongs to the operator
            pass

        def _send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';"
                " connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.headers.get("Host") not in hosts or self.headers.get("Origin", None) not in (None, *origins):
                return self._send(403, b"forbidden", "text/plain")
            path = self.path.split("?", 1)[0]
            if path == "/":
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/state":
                try:
                    body = shared.body()
                except (OSError, sqlite3.Error):
                    return self._send(503, b"store unavailable", "text/plain")
                return self._send(200, body, "application/json")
            self._send(404, b"not found", "text/plain")

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # on Windows SO_REUSEADDR lets another process take the port


def serve(db_path: Optional[Path] = None, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """Bind and return the server (the caller runs it); ``port`` 0 picks a free one."""
    state(db_path)  # fail on a missing store before binding
    server = _Server((HOST, port), _handler(db_path, port))
    if port == 0:  # the handler must know the port it ended up on
        server.RequestHandlerClass = _handler(db_path, server.server_address[1])
    return server


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="hardline-mcp dash", description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, help="store to show (default: as the servers resolve it)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}")
    args = parser.parse_args(argv)
    try:
        server = serve(args.db, args.port)
    except FileNotFoundError as exc:
        print(f"hardline-mcp dash: no store at {exc.filename}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"hardline-mcp dash: cannot listen on {HOST}:{args.port}: {exc}", file=sys.stderr)
        return 1
    print(f"hardline dash: http://{HOST}:{server.server_address[1]}/  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
