"""Read-only, level-triggered mailbox observation shared by host adapters."""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import math
import os
import signal
import sqlite3
import stat
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import mailbox, procid


class Unavailable(Exception):
    """Observation may recover; it must never be mistaken for an empty inbox."""


class TargetLost(Exception):
    """The bound instance has ended. Re-arming requires a new explicit target."""


@dataclass(frozen=True)
class Target:
    db: Path
    agent: str
    lanes: tuple[str, ...] = ()
    owner_pid: int | None = None
    owner_key: str | None = None


_OWNER_QUERY = """
WITH owned AS (
    SELECT lane FROM agent_sessions
    WHERE pid = :pid AND process_key = :key AND agent = :agent
), recipients AS (
    SELECT :agent AS recipient UNION SELECT lane FROM owned
)
SELECT EXISTS(SELECT 1 FROM owned), EXISTS(
    SELECT 1 FROM messages
    WHERE recipient IN (SELECT recipient FROM recipients) AND acked_at IS NULL
)
"""


def read_pending(target: Target) -> bool:
    """Read a fresh snapshot, never using the store's initializing helpers."""
    if target.owner_pid is not None:
        state = procid.instance_state(target.owner_pid, target.owner_key)
        if state == procid.DEAD:
            raise TargetLost("mailbox owner exited or its PID was reused; re-arm")
        if state == procid.UNKNOWN:
            raise Unavailable("cannot verify mailbox owner")
    try:
        target.db.stat()  # Distinguish a missing file from permission failures.
        uri = target.db.as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=0.25)) as conn:
            if target.owner_pid is not None:
                registered, pending = conn.execute(
                    _OWNER_QUERY,
                    {
                        "pid": target.owner_pid,
                        "key": target.owner_key,
                        "agent": target.agent,
                    },
                ).fetchone()
                if not registered:
                    raise Unavailable(
                        "mailbox owner is not registered; call list_agents and re-arm"
                    )
            else:
                recipients = (target.agent, *target.lanes)
                marks = ",".join("?" for _ in recipients)
                pending = conn.execute(
                    f"SELECT EXISTS(SELECT 1 FROM messages WHERE recipient IN ({marks})"
                    " AND acked_at IS NULL)",
                    recipients,
                ).fetchone()[0]
            return bool(pending)
    except FileNotFoundError as exc:
        raise Unavailable(f"mailbox not found at {target.db}") from exc
    except sqlite3.OperationalError as exc:
        # Named result codes and sqlite_errorcode were added in 3.11.
        # SQLite's stable BUSY/LOCKED codes keep the 3.10 fallback narrow.
        code = getattr(exc, "sqlite_errorcode", 0) & 255
        message = str(exc).lower()
        if code in (5, 6) or message in (
            "database is locked",
            "database table is locked",
            "database is busy",
            "no such table: messages",
            "no such table: agent_sessions",
        ):
            raise Unavailable(str(exc)) from exc
        if message == "unable to open database file" and not target.db.exists():
            raise Unavailable(f"mailbox not found at {target.db}") from exc
        raise


def diagnostic(message: str) -> None:
    """Keep errors on one ASCII-safe line, including on Windows terminals."""
    escaped = json.dumps(message, ensure_ascii=True)
    print("hardline watch: " + escaped[:2048], file=sys.stderr, flush=True)


def emit(notice: dict | None, pending: bool = True) -> bool:
    if notice is None:
        return False
    try:
        print(json.dumps(notice, separators=(",", ":"), ensure_ascii=True), flush=True)
    except OSError as exc:
        # Windows CRT reports EINVAL for a pipe whose reader has gone away.
        closed_pipe = isinstance(exc, BrokenPipeError) or (
            os.name == "nt"
            and exc.errno == errno.EINVAL
            and stat.S_ISFIFO(os.fstat(sys.stdout.fileno()).st_mode)
        )
        if not closed_pipe:
            raise
        # Prevent interpreter shutdown from retrying the failed flush (exit 120).
        with contextlib.suppress(OSError):
            sys.stdout.close()
        raise BrokenPipeError("observer stdout closed") from exc
    return True


def run(
    target: Target,
    *,
    interval: float = 1.0,
    remind_after: float = 30.0,
    once: bool = False,
    poll: Callable[[dict | None, bool], bool] = emit,
    clock: Callable[[], float] = time.monotonic,
    wait: Callable[[float], bool] | None = None,
    report: Callable[[str], None] = diagnostic,
    grace: float = 30.0,
) -> int:
    """Notify until observed empty; defer busy hosts without accumulating mail.

    Call ``poll(notice, pending)`` once per snapshot. A None notice means no
    delivery is due; pending distinguishes an empty inbox from a quiet backlog.
    The host validates even when quiet and returns True only for acceptance.
    The observer alone schedules reminders; ``wait`` returns True to stop.
    """
    wait = wait or threading.Event().wait
    due: float | None = None
    failed_since: float | None = None
    sequence = 0
    ready = False
    try:
        while True:
            try:
                pending = read_pending(target)
                if not ready:
                    mode = "instance" if target.owner_pid is not None else "lanes"
                    report(
                        f"observer_ready: {target.db} agent={target.agent} target={mode}"
                    )
                    ready = True
                notice = None
                if not pending:
                    due = None
                elif due is None or clock() >= due:
                    notice = {
                        "event": "mail_pending",
                        "agent": target.agent,
                        "sequence": sequence + 1,
                    }
                accepted = poll(notice, pending)
                if notice is not None and accepted:
                    sequence += 1
                    due = clock() + remind_after
                if failed_since is not None:
                    report("observation recovered")
                failed_since = None
                if once:
                    return 0
                delay = interval
            except Unavailable as exc:
                now = clock()
                if failed_since is None:
                    failed_since = now
                    report(f"unavailable: {exc}")
                remaining = grace - (now - failed_since)
                if once or remaining <= 0:
                    report(f"error: observation unavailable: {exc}")
                    return 1
                delay = min(interval, remaining)
            if wait(delay):
                return 0
    except TargetLost as exc:
        report(f"target lost: {exc}")
        return 3
    except (KeyboardInterrupt, BrokenPipeError):
        return 0
    except Exception as exc:
        report(f"error {type(exc).__name__}: {exc}")
        return 1


def add_arguments(parser: argparse.ArgumentParser, *, agent: str | None = None) -> None:
    """A fixed host agent requires instance binding; generic observation allows lanes."""
    parser.add_argument(
        "--db",
        type=Path,
        help="Mailbox path (otherwise HARDLINE_DB or the server default)",
    )
    if agent is not None:
        parser.set_defaults(agent=agent, lane=[])
        parser.add_argument("--owner-pid", type=int, required=True)
    else:
        parser.add_argument(
            "--agent", choices=("claude", "codex", "hermes"), required=True
        )
        selection = parser.add_mutually_exclusive_group(required=True)
        selection.add_argument("--owner-pid", type=int)
        selection.add_argument("--lane", action="append", default=[])
        parser.add_argument(
            "--once", action="store_true", help="Observe once without sleeping"
        )
    parser.add_argument(
        "--owner-key", help="Creation token supplied by the owning MCP server"
    )
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--remind-after", type=float, default=30.0)


def target_from_args(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> Target:
    for name, lower, upper in (("interval", 0.2, 60), ("remind_after", 5, 3600)):
        value = getattr(args, name)
        if not math.isfinite(value) or not lower <= value <= upper:
            parser.error(
                f"--{name.replace('_', '-')} must be finite and within [{lower}, {upper}]"
            )
    if args.remind_after < args.interval:
        parser.error("--remind-after must be at least --interval")
    if args.owner_pid is not None:
        if args.owner_pid <= 0 or not args.owner_key or not args.owner_key.strip():
            parser.error(
                "instance mode requires a positive --owner-pid and nonempty --owner-key"
            )
    elif args.owner_key is not None:
        parser.error("--owner-key requires --owner-pid")
    lanes = tuple(dict.fromkeys(args.lane))
    prefix = args.agent + ":"
    if any(not lane.startswith(prefix) or not lane[len(prefix) :] for lane in lanes):
        parser.error(
            f"each --lane must be a complete recipient beginning with {prefix}"
        )
    return Target(
        mailbox._resolve_db(args.db).expanduser().resolve(),
        args.agent,
        lanes,
        args.owner_pid,
        args.owner_key,
    )


@contextlib.contextmanager
def stopping():
    """Restore handlers for callers embedding the CLI in a larger process."""
    stopped = threading.Event()
    saved = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            saved[sig] = signal.signal(sig, lambda *_: stopped.set())
    try:
        yield stopped.wait
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def launch_info(agent: str | None) -> dict:
    """Describe this server's observer; neither start it nor touch its store."""
    key = procid.process_key(os.getpid())
    if not agent or not key:
        return {
            "argv": None,
            "reason": "server agent or process creation token is unavailable",
        }
    return {
        "argv": [
            sys.executable,
            "-m",
            "hardline_mcp.watch",
            "--db",
            str(mailbox._resolve_db(None).expanduser().resolve()),
            "--agent",
            agent,
            "--owner-pid",
            str(os.getpid()),
            "--owner-key",
            key,
        ]
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    args = parser.parse_args(argv)
    target = target_from_args(parser, args)
    with stopping() as wait:
        return run(
            target,
            interval=args.interval,
            remind_after=args.remind_after,
            once=args.once,
            wait=wait,
        )


if __name__ == "__main__":
    raise SystemExit(main())
