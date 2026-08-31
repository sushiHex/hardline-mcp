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
from typing import Callable, Iterable, Optional, Union

# "Which lanes does this caller own": none, one, or several. Several arises
# once a session can be renamed - see _lanes.
LaneSpec = Union[str, Iterable[str], None]

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


# Bumped when a table is added or a column's meaning changes. Recorded in
# `meta` so a running server can report what store it is talking to rather
# than leaving "is this the new schema?" to be inferred from behaviour.
SCHEMA_VERSION = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sender     TEXT NOT NULL,
    recipient  TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    acked_at   TEXT
);

-- Every read of this table filters on recipient and unread-ness, and without
-- an index each one scans the whole table. That is survivable for a poll that
-- finds work and wasteful for the far more common one that finds none: with
-- ~26 sessions polling a shared mailbox, the empty reads dominate.
CREATE INDEX IF NOT EXISTS messages_recipient_idx ON messages (recipient, acked_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Async dispatch used to be fire-and-forget and process-local: a restart lost
-- the task with no record it had ever existed, and a timeout produced a result
-- with nothing in it. A job row is the durable identity that survives both.
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    agent        TEXT NOT NULL,
    requester    TEXT NOT NULL,
    label        TEXT,
    state        TEXT NOT NULL,
    request      TEXT NOT NULL,
    result       TEXT,
    error        TEXT,
    owner_pid    INTEGER NOT NULL,
    child_pid    INTEGER,
    -- Process-identity token for child_pid (creation time). A pid alone is
    -- not an identity: a finished child's pid can be reused, and cancelling
    -- on a stale pid would kill an unrelated process.
    child_key    TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);

CREATE INDEX IF NOT EXISTS jobs_state_idx ON jobs (state, created_at);
CREATE INDEX IF NOT EXISTS jobs_requester_idx ON jobs (requester, created_at);

-- Identity was the one thing here with no durable record. Jobs and messages
-- both survive a restart; the SESSION that owns a lane existed only as a
-- function of one process's environment, so nothing could answer "who can I
-- address?" or "is that lane still held?". Callers guessed, and lane-qualified
-- mail sent to a session that had exited became permanently unconsumable -
-- only the lane's holder may ack it, and the holder was gone.
--
-- One row per (process, LANE). A session owns every lane it has held, because
-- renaming ADDS a name rather than replacing one - an async result's recipient
-- is fixed at dispatch, so results owed to the old name must stay consumable.
-- Recording only the current name would contradict that: the old lane would
-- show no holder, so it would read as dead, senders would be warned nobody
-- could receive it, and another session could CLAIM it while the original was
-- still consuming it. Both would then drain each other's mail.
--
-- Liveness is NOT stored: it is derived on read from the pid and its
-- creation-time token, exactly as jobs derives `lost`. A crashed session
-- therefore needs no cleanup - it simply stops being alive.
CREATE TABLE IF NOT EXISTS agent_sessions (
    pid         INTEGER NOT NULL,
    -- Fully-qualified recipient ("codex:construction"), so it compares
    -- directly against messages.recipient with no reassembly.
    lane        TEXT NOT NULL,
    agent       TEXT NOT NULL,
    -- The human name claimed via register_session, NULL when the lane was
    -- derived from the environment. Distinguishes "chose to be called this"
    -- from "this is what the env implied".
    label       TEXT,
    -- Creation-time token for pid. A pid is not an identity: without this, a
    -- reused pid would inherit the previous session's lane and its mail.
    process_key TEXT,
    cwd         TEXT,
    started_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    -- When THIS lane was taken. Human-readable only: timestamps here have
    -- second precision, so two claims inside one second cannot be ordered by
    -- it. `seq` is what actually orders them.
    claimed_at  TEXT NOT NULL,
    -- Monotonic per process. The highest is the name the session is currently
    -- addressed by; the rest are what it can still consume. Ordering on the
    -- timestamp and tie-breaking on lane text would let a rapid rename
    -- advertise whichever name sorted last.
    seq         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (pid, lane)
);

CREATE INDEX IF NOT EXISTS agent_sessions_lane_idx ON agent_sessions (lane);
"""

# Named `agent_sessions`, not `sessions`, and that is a correctness decision
# rather than taste. This branch gave `sessions` three different shapes, and
# under an editable install a process runs whatever the tree said when it
# SPAWNED - so several of those shapes are live against one store at the same
# time, indefinitely, with no coordinated restart to end the overlap.
#
# Reshaping in place meant inspecting the table and then dropping it, and those
# two steps cannot be made one without a transaction spanning both: process A
# reads the old shape, B drops it, builds the right one and registers itself,
# then A acts on its stale reading and destroys B's table and B's committed row.
# `meta.schema_version` cannot arbitrate, because every one of those revisions
# calls itself 4.
#
# A distinct name removes the whole class of failure instead of racing better.
# Old revisions keep writing to `sessions`, this one owns `agent_sessions`, and
# nothing ever has to drop anything. The orphan table is small and harmless;
# destroying another process's registrations is neither.

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

# history returns whole bodies, so it needs the same ceiling or it is simply
# the same flood through another door. Its default is unchanged (50).
DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 200


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


# Columns added to an EXISTING table after it first shipped. CREATE TABLE IF
# NOT EXISTS cannot add them, so a store created by an earlier version keeps
# the old shape and every write naming the new column fails.
_ADDED_COLUMNS = {
    "jobs": {"child_key": "TEXT"},
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Bring an older table up to the current shape. Idempotent."""
    for table, columns in _ADDED_COLUMNS.items():
        try:
            present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue
        if not present:  # table absent entirely; the schema above creates it
            continue
        for name, decl in columns.items():
            if name in present:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            except sqlite3.OperationalError as exc:
                # Check-then-ALTER is a race with ~26 processes initializing
                # the same store: two can both see the column missing, one adds
                # it, and the other gets "duplicate column name". That is the
                # desired end state reached by someone else, not a failure -
                # and it is neither "locked" nor "busy", so the retry loop
                # above would re-raise it and break initialization outright.
                if "duplicate column" not in str(exc).lower():
                    raise


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
        # Cached by PATH, so a store that is deleted underneath a running fleet
        # would otherwise never be rebuilt: every process would go on believing
        # it had initialized a file that is no longer there, and every call
        # would fail with "no such table" for the life of the process. One stat
        # is cheap beside opening SQLite.
        #
        # This catches deletion, which is the case that happens. A file
        # SWAPPED for a different one still exists, so it is not caught here -
        # verifying the schema on every connection would cost a query per
        # operation to defend against something nobody does by accident.
        if db_path.exists():
            return
        _initialized_paths.discard(key)
    with _init_lock:
        if key in _initialized_paths:
            return
        db_path.parent.mkdir(parents=True, exist_ok=True)
        last_error: Optional[sqlite3.OperationalError] = None
        for attempt in range(_INIT_ATTEMPTS):
            try:
                with closing(sqlite3.connect(str(db_path), timeout=10.0)) as conn:
                    conn.execute("PRAGMA journal_mode=WAL")
                    # executescript, not execute: the schema is several
                    # statements now. Every one is IF NOT EXISTS, so this stays
                    # idempotent and an existing messages-only store gains the
                    # new tables without a migration step.
                    conn.executescript(_SCHEMA)
                    _add_missing_columns(conn)
                    # INSERT OR REPLACE rather than ON CONFLICT ... DO UPDATE:
                    # upsert syntax needs SQLite 3.24+, and this is the one
                    # statement that would make an otherwise-compatible store
                    # fail to initialize at all.
                    conn.execute(
                        "INSERT OR REPLACE INTO meta (key, value)"
                        " VALUES ('schema_version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
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


def _lanes(owned: LaneSpec) -> tuple[str, ...]:
    """Normalize "which recipients does this caller own" to a tuple, once.

    Entries are FULLY-QUALIFIED recipients (``claude:fonts.1a2b3c4d``), not
    bare suffixes. Matching on the suffix alone let a process holding
    ``construction`` consume ``codex:construction`` AND ``claude:construction``
    - one identity reaching into another agent's mail. That hole predates
    runtime labels but was nearly unreachable while lanes were derived from
    session ids, which never collide; the moment sessions can NAME themselves,
    two agents both called ``construction`` is the obvious case, not the exotic
    one.

    A process owns SEVERAL recipients as soon as it can be renamed: the lane it
    derived from its environment, plus every name it has since claimed. Mail
    addressed to the old one is still owed to it - a rename that stranded
    in-flight results would recreate the exact defect lanes exist to prevent
    (an async result's recipient is captured at dispatch, so a rename between
    dispatch and delivery lands mail on the previous lane).

    Materializes to a tuple deliberately. ``inbox`` consults ownership once per
    message and again for ``remaining``, so a one-shot iterator would be
    exhausted after the first message: later messages would silently stop being
    ackable while ``remaining`` read zero.
    """
    if not owned:
        return ()
    if isinstance(owned, str):
        return (owned,)
    return tuple(dict.fromkeys(r for r in owned if r))


def _lane_clause(owned: tuple[str, ...]) -> tuple[str, tuple]:
    """SQL for "this caller may consume this recipient", plus its params.

    The rule lives in exactly two forms - this and ``_lane_ackable`` - because
    it is enforced in both SQL and Python. It used to live in three, the same
    predicate hand-written into ``ack`` and into ``inbox``'s remaining count,
    which is one copy more than can be kept in step.
    """
    if not owned:
        # A process with no lane owns no lane, so it may consume only bare
        # recipients. Guarded explicitly rather than skipped: an unconditional
        # branch here let any process without a lane ack every session's mail.
        return " AND instr(recipient, ':') = 0", ()
    marks = ", ".join("?" for _ in owned)
    return (
        f" AND (instr(recipient, ':') = 0 OR recipient IN ({marks}))",
        owned,
    )


def _lane_ackable(recipient: str, owned: LaneSpec) -> bool:
    """May a caller owning ``owned`` consume a message sent to ``recipient``?

    The Python mirror of ``_lane_clause``: unqualified recipients stay shared
    and consumable by anyone, a lane-qualified one only by a process holding
    that exact recipient, and a process with no lane owns no lane so it may
    consume only bare recipients. Kept beside its SQL twin because ``inbox``'s
    consuming read must not be able to drift from ``ack``'s rule - a read that
    consumed what an ack would have refused is the same defect with a
    different entry point.
    """
    if ":" not in recipient:
        return True
    return recipient in _lanes(owned)


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
    owned: LaneSpec = None,
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

    ``auto_ack`` (default) consumes exactly the returned ids under a single
    atomic claim (BEGIN IMMEDIATE). This is what makes ``limit`` safe rather
    than harmful: oldest-first + a limit + nothing acking pins the caller to
    the same oldest batch forever and it never sees a new message again.
    Consuming server-side advances the cursor per poll, so a backlog drains.

    Consuming honours ``lane_suffix`` exactly as ``ack`` does, so reading a
    lane you do not own shows you the messages but does NOT consume them.
    It is also ignored unless ``unread_only`` - see ``consuming`` below.

    Nothing is destroyed: ``history`` ignores ``acked_at`` and remains the
    recovery path for anything consumed.
    """
    db_path = _resolve_db(db_path)
    agents = [agent] if isinstance(agent, str) else list(agent)
    if not agents:
        return [], 0
    # ONCE, here. Ownership is consulted per message and again for the
    # remaining count, so normalizing at each use would exhaust a one-shot
    # iterator after the first message - and a bare string would be iterated
    # character by character, producing a placeholder per letter.
    owned = _lanes(owned)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_INBOX_LIMIT
    limit = max(1, min(limit, MAX_INBOX_LIMIT))

    # Consuming an already-acked row is impossible, so pairing auto_ack with
    # unread_only=False would re-serve the same oldest acked batch on every
    # call while ``remaining`` stayed positive - reintroducing, in a browsing
    # mode, the exact pin that consuming exists to prevent.
    consuming = auto_ack and unread_only

    placeholders = ", ".join("?" for _ in agents)
    sql = f"SELECT * FROM messages WHERE recipient IN ({placeholders})"
    if unread_only:
        sql += " AND acked_at IS NULL"
    sql += " ORDER BY id ASC LIMIT ?"

    with closing(_connect(db_path)) as conn:
        if consuming:
            # Look before reserving the single writer. A consuming read takes
            # BEGIN IMMEDIATE, and taking it to discover there is nothing to do
            # serializes every idle poll against every real writer - which, with
            # a fleet of sessions polling a shared mailbox, is almost all of
            # them. This probe is a plain indexed read that blocks nobody.
            #
            # Racy by nature, and harmlessly so: a message arriving between the
            # probe and the return is found by the next poll, which is exactly
            # what would have happened had it arrived a moment later.
            probe = conn.execute(
                f"SELECT 1 FROM messages WHERE recipient IN ({placeholders})"
                " AND acked_at IS NULL LIMIT 1",
                tuple(agents),
            ).fetchone()
            if probe is None:
                return [], 0
        # BEGIN IMMEDIATE for a CONSUMING read only. Python's legacy
        # transaction mode opens a transaction only on DML, so a bare
        # `with conn:` leaves the SELECT outside it entirely: two pollers both
        # read the same ids, one UPDATE wins and the other matches zero rows -
        # yet BOTH return the batch. Verified: that interleaving
        # double-delivers. Reserving the writer up front makes read-then-consume
        # a single atomic claim. (BEGIN DEFERRED is not enough under WAL - the
        # upgrade can fail with SQLITE_BUSY_SNAPSHOT, which busy_timeout cannot
        # resolve.)
        #
        # A non-consuming read must NOT take it. WAL exists so readers and
        # writers coexist; reserving the single writer for a call that will
        # never write serializes browsing, empty, and foreign-lane reads
        # against every real writer, and sustained polling could starve them
        # into SQLITE_BUSY once busy_timeout elapses.
        try:
            if consuming:
                conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(sql, (*agents, limit)).fetchall()
            messages = [_row_to_dict(r) for r in rows]

            if consuming and messages:
                # Same ownership rule as ``ack``: a lane-qualified message is
                # consumable only by the process holding that lane. Without
                # this, inbox(agent="claude:other") would silently drain
                # another session's results through the default path - the
                # precise failure lanes were introduced to prevent.
                ackable = [
                    m["message_id"]
                    for m in messages
                    if _lane_ackable(m["recipient"], owned)
                ]
                if ackable:
                    stamp = _iso(now_fn())
                    id_marks = ", ".join("?" for _ in ackable)
                    cur = conn.execute(
                        f"UPDATE messages SET acked_at = ? WHERE id IN ({id_marks})"
                        f" AND recipient IN ({placeholders}) AND acked_at IS NULL",
                        (stamp, *ackable, *agents),
                    )
                    # Reflect the commit in what we hand back; returning
                    # acked_at=None for a row this call just consumed is a
                    # stale representation the caller may act on. Stamp only
                    # if the UPDATE really matched every row we intended -
                    # otherwise the stamp would be an assumption, not a fact.
                    if cur.rowcount == len(ackable):
                        consumed = set(ackable)
                        for msg in messages:
                            if msg["message_id"] in consumed:
                                msg["acked_at"] = stamp

            # Count only what THIS caller could actually consume. Counting
            # rows it can never ack (another session's lane) would leave
            # ``remaining`` permanently positive, and the documented "poll
            # while remaining" loop would never terminate - head-of-line
            # blocking behind mail that is not yours to take.
            lane_clause, lane_params = _lane_clause(owned)
            remaining_sql = (
                f"SELECT COUNT(*) FROM messages WHERE recipient IN ({placeholders})"
                " AND acked_at IS NULL" + lane_clause
            )
            remaining = conn.execute(
                remaining_sql, tuple(agents) + lane_params
            ).fetchone()[0]
            if consuming:
                conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return messages, remaining


def recipients(*, db_path: Optional[Path] = None) -> list[dict]:
    """Every recipient name the mailbox has actually seen, with counts.

    Exists because agent identity was undiscoverable: ``history`` filtered by
    a name that carries no traffic returns an empty list, which is
    indistinguishable from "no messages" - one agent searched its own display
    name for a while before learning its mailbox identity was ``hermes``.
    Reporting the names in use turns that guess into a lookup.
    """
    db_path = _resolve_db(db_path)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT recipient, COUNT(*) AS total,"
            " SUM(CASE WHEN acked_at IS NULL THEN 1 ELSE 0 END) AS unread,"
            " MAX(created_at) AS newest"
            " FROM messages GROUP BY recipient ORDER BY recipient"
        ).fetchall()
        return [
            {
                "recipient": r["recipient"],
                "total": r["total"],
                "unread": r["unread"] or 0,
                "newest": r["newest"],
            }
            for r in rows
        ]


def senders(*, db_path: Optional[Path] = None) -> list[str]:
    """Distinct sender names the mailbox has seen."""
    db_path = _resolve_db(db_path)
    with closing(_connect(db_path)) as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT sender FROM messages ORDER BY sender"
            ).fetchall()
        ]


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
    owned: LaneSpec = None,
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

    ``lane_suffix`` may name SEVERAL lanes (see ``_lanes``): a session that has
    been renamed still owns the lane it was addressed by beforehand, and mail
    already in flight to it must stay consumable.
    """
    db_path = _resolve_db(db_path)
    clause, lane_params = _lane_clause(_lanes(owned))
    sql = "UPDATE messages SET acked_at = ? WHERE id = ? AND acked_at IS NULL" + clause
    params: tuple = (_iso(now_fn()), message_id) + lane_params
    with closing(_connect(db_path)) as conn:
        with conn:  # transaction: commit on success, rollback on error
            cur = conn.execute(sql, params)
        return {"ok": cur.rowcount > 0}


def history(
    limit: int = DEFAULT_HISTORY_LIMIT,
    agent: Optional[str] = None,
    *,
    before_id: Optional[int] = None,
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

    ``before_id`` pages backward (``id < before_id``), which is what makes
    this feed a usable recovery path rather than a peephole. ``inbox``
    consumes what it returns, so a response lost in transit is recoverable
    only through here - but newest-first plus a capped limit meant anything
    older than one page was unreachable, and you do not know the message id
    to ``peek`` precisely when you lost the response. Paging closes that.

    Note the agent predicate is parenthesized: it is an OR-chain, so an
    unparenthesized form would bind a later AND to only its last term.

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
    # Clamped for the same reason inbox is: this returns whole bodies, so an
    # unbounded limit is the identical context flood through another door.
    # SQLite also reads a negative LIMIT as "no limit", so -1 was unbounded.
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_HISTORY_LIMIT
    limit = max(1, min(limit, MAX_HISTORY_LIMIT))
    params: tuple = ()
    where: list[str] = []
    if agent is not None:
        lane_glob = f"{agent}:%"
        where.append(
            "(sender = ? OR recipient = ? OR sender LIKE ? OR recipient LIKE ?)"
        )
        params = (agent, agent, lane_glob, lane_glob)
    if before_id is not None:
        where.append("id < ?")
        params = params + (before_id,)
    sql = "SELECT * FROM messages"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params = params + (limit,)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]
