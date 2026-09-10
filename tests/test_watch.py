"""Real SQLite observations and deterministic clocks; no live agent calls."""

import argparse
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
from dataclasses import replace

import pytest

from hardline_mcp import mailbox, procid, watch


@pytest.fixture
def target(tmp_path):
    db = tmp_path / "mail # percent%.db"
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.executescript(mailbox._SCHEMA)
    return watch.Target(db, "codex", ("codex:mine",))


def sql(target, statement, params=()):
    conn = sqlite3.connect(target.db)
    try:
        with conn:
            return conn.execute(statement, params).fetchall()
    finally:
        conn.close()


def send(target, recipient, acked=None):
    sql(
        target,
        "INSERT INTO messages(sender,recipient,body,created_at,acked_at) VALUES ('hermes',?,'body', 't',?)",
        (recipient, acked),
    )


def register(target, lane, key="key"):
    sql(
        target,
        "INSERT INTO agent_sessions(pid,lane,agent,process_key,started_at,last_seen,claimed_at) VALUES (123,?,?,?,'t','t','t')",
        (lane, target.agent, key),
    )


@pytest.fixture
def owner(target, monkeypatch):
    monkeypatch.setattr(procid, "instance_state", lambda *_: procid.ALIVE)
    target = replace(target, lanes=(), owner_pid=123, owner_key="key")
    register(target, "codex:mine")
    return target


@pytest.mark.parametrize("agent,other", [("claude", "codex"), ("codex", "claude")])
def test_exact_unread_scope(target, agent, other):
    target = replace(target, agent=agent, lanes=(agent + ":mine",))
    for recipient in (other, "hermes", other + ":mine", agent + ":other"):
        send(target, recipient)
    send(target, agent, "already read")
    assert not watch.read_pending(target)
    send(target, agent)
    assert watch.read_pending(target)
    sql(target, "UPDATE messages SET acked_at='t'")
    send(target, agent + ":mine")
    assert watch.read_pending(target)
    sql(target, "UPDATE messages SET acked_at='t'")
    assert not watch.read_pending(target)


def test_claim_includes_old_mail_and_release_removes_it(owner):
    send(owner, "codex:claimed")
    assert not watch.read_pending(owner)
    register(owner, "codex:claimed")
    assert watch.read_pending(owner)
    sql(owner, "DELETE FROM agent_sessions WHERE lane='codex:claimed'")
    assert not watch.read_pending(owner)
    send(owner, "codex:mine")
    assert watch.read_pending(owner)


def test_missing_owner_never_falls_back_to_bare_mail(owner):
    send(owner, "codex")
    with pytest.raises(watch.Unavailable):
        watch.read_pending(replace(owner, owner_key="different"))


@pytest.mark.parametrize(
    "state,exception",
    [(procid.DEAD, watch.TargetLost), (procid.UNKNOWN, watch.Unavailable)],
)
def test_process_probe_is_authoritative(owner, monkeypatch, state, exception):
    monkeypatch.setattr(procid, "instance_state", lambda *_: state)
    with pytest.raises(exception):
        watch.read_pending(owner)


def test_read_only_connection_and_no_initialization(target, monkeypatch):
    send(target, "codex")
    before = sql(target, "SELECT * FROM messages")
    connect = sqlite3.connect
    statements = []

    def read_only(database, **kwargs):
        assert database.endswith("?mode=ro") and kwargs["uri"] is True
        conn = connect(database, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE messages SET acked_at='forbidden'")
        conn.set_trace_callback(statements.append)
        return conn

    def forbidden(*_):
        pytest.fail("observer called a write-capable mailbox helper")

    monkeypatch.setattr(mailbox, "_connect", forbidden)
    with monkeypatch.context() as scoped:
        scoped.setattr(sqlite3, "connect", read_only)
        assert watch.read_pending(target)
    assert before == sql(target, "SELECT * FROM messages")
    assert len(statements) == 1 and statements[0].startswith("SELECT EXISTS")


def test_new_connection_sees_replaced_file(target):
    assert not watch.read_pending(target)
    replacement = replace(target, db=target.db.with_name("replacement.db"))
    with contextlib.closing(sqlite3.connect(replacement.db)) as conn:
        conn.executescript(mailbox._SCHEMA)
    send(replacement, "codex:mine")
    replacement.db.replace(target.db)
    assert watch.read_pending(target)
    sql(target, "DELETE FROM messages")
    sql(target, "DELETE FROM sqlite_sequence WHERE name='messages'")
    send(target, "codex")
    assert watch.read_pending(target)


def test_locked_database_is_unavailable_then_recovers(target):
    with contextlib.closing(sqlite3.connect(target.db)) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        with pytest.raises(watch.Unavailable, match="locked"):
            watch.read_pending(target)
        writer.rollback()
    assert not watch.read_pending(target)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def exercise(target, *, duration, tick=lambda _: None, **kwargs):
    clock = Clock()
    notices, reports = [], []

    def wait(delay):
        clock.now += delay
        tick(clock.now)
        return clock.now >= duration

    result = watch.run(
        target,
        clock=clock,
        wait=wait,
        poll=lambda n, pending: (
            (notices.append((clock.now, n)) or True) if n is not None else False
        ),
        report=reports.append,
        **kwargs,
    )
    return result, notices, reports


def test_backlog_is_one_notice_and_reminders_stop_after_drain(target):
    sql(
        target,
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<5000) INSERT INTO messages(sender,recipient,body,created_at) SELECT 'claude','codex','long body','t' FROM n",
    )

    def tick(now):
        if now == 7:
            sql(target, "UPDATE messages SET acked_at='t'")
        if now == 8:
            send(target, "codex:mine")

    result, notices, _ = exercise(target, duration=11, remind_after=5, tick=tick)
    assert result == 0
    assert [t for t, _ in notices] == [0, 5, 8]
    assert [n["sequence"] for _, n in notices] == [1, 2, 3]
    assert all(set(n) == {"event", "agent", "sequence"} for _, n in notices)


def test_drain_and_refill_between_polls_still_gets_reminder(target):
    send(target, "codex")

    def tick(now):
        if now == 1:
            sql(target, "UPDATE messages SET acked_at='t'")
            send(target, "codex")

    result, notices, _ = exercise(target, duration=7, remind_after=5, tick=tick)
    assert result == 0
    assert [t for t, _ in notices] == [0, 5]


def test_empty_is_silent_and_new_mail_wakes(target):
    result, notices, _ = exercise(
        target, duration=4, tick=lambda t: send(target, "codex") if t == 2 else None
    )
    assert result == 0 and [t for t, _ in notices] == [2]


def test_deferred_delivery_does_not_advance_sequence_or_deadline(target):
    send(target, "codex")
    clock, attempts = Clock(), []

    def deliver(notice, pending):
        if notice is None:
            return False
        attempts.append((clock.now, notice["sequence"]))
        return clock.now >= 2

    def wait(dt):
        clock.now += dt
        return clock.now >= 5

    assert watch.run(target, clock=clock, wait=wait, poll=deliver) == 0
    assert attempts == [(0, 1), (1, 1), (2, 1)]


def test_unavailability_is_bounded_and_not_empty(target, monkeypatch):
    clock, reports = Clock(), []

    def unavailable(_):
        raise watch.Unavailable(f"failure at {clock.now}")

    def wait(dt):
        clock.now += dt
        return False

    monkeypatch.setattr(watch, "read_pending", unavailable)
    assert (
        watch.run(target, clock=clock, wait=wait, interval=60, report=reports.append)
        == 1
    )
    assert clock.now == 30
    assert len(reports) == 2


def test_recovery_keeps_reminder_deadline(target, monkeypatch):
    send(target, "codex")
    read = watch.read_pending
    state = {"offline": False}

    def sometimes(t):
        if state["offline"]:
            raise watch.Unavailable("offline")
        return read(t)

    monkeypatch.setattr(watch, "read_pending", sometimes)
    result, notices, reports = exercise(
        target,
        duration=7,
        remind_after=5,
        tick=lambda t: state.update(offline=t in (1, 2)),
    )
    assert result == 0 and [t for t, _ in notices] == [0, 5]
    assert "observation recovered" in reports


@pytest.mark.parametrize("pending", [False, True])
def test_once_reports_unread_without_waiting_or_acking(target, capsys, pending):
    if pending:
        send(target, "codex")
    assert (
        watch.main(
            [
                "--db",
                str(target.db),
                "--agent",
                "codex",
                "--lane",
                "codex:mine",
                "--once",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    if pending:
        assert json.loads(output)["event"] == "mail_pending"
    else:
        assert output == ""
    assert watch.read_pending(target) == pending


def test_missing_or_corrupt_database_is_not_created_or_hidden(target, capsys):
    target.db.unlink()
    assert watch.run(target, once=True) == 1
    assert not target.db.exists()
    target.db.write_bytes(b"not a database" * 100)
    assert watch.run(target, once=True) == 1
    assert "DatabaseError" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra",
    [
        ["--interval", "nan"],
        ["--interval", "inf"],
        ["--interval", "0"],
        ["--remind-after", "nan"],
        ["--remind-after", "4"],
        ["--interval", "60"],
        ["--owner-key", "unpaired"],
    ],
)
def test_invalid_options_fail_before_observing(extra):
    with pytest.raises(SystemExit) as exc:
        watch.main(["--agent", "codex", "--lane", "codex:mine", "--once", *extra])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--lane", "claude:mine"],
        ["--lane", "codex:"],
        ["--owner-pid", "0", "--owner-key", "key"],
        ["--owner-pid", "123"],
        ["--owner-pid", "123", "--owner-key", " "],
        ["--owner-pid", "123", "--owner-key", "key", "--lane", "codex:mine"],
    ],
)
def test_invalid_target_never_guesses(args):
    with pytest.raises(SystemExit) as exc:
        watch.main(["--agent", "codex", "--once", *args])
    assert exc.value.code == 2


def test_db_resolution_and_deduplication(target, monkeypatch):
    monkeypatch.setenv("HARDLINE_DB", str(target.db))
    parser = argparse.ArgumentParser()
    watch.add_arguments(parser)
    args = parser.parse_args(
        ["--agent", "codex", "--lane", "codex:mine", "--lane", "codex:mine"]
    )
    assert watch.target_from_args(parser, args) == target


def test_connection_is_closed_before_delivery_and_wait(target):
    send(target, "codex")

    def writer(_, pending=True):
        sql(target, "BEGIN EXCLUSIVE")
        return True

    assert watch.run(target, poll=writer, wait=writer) == 0


def test_lost_owner_and_closed_output_exit_cleanly(target, monkeypatch):
    monkeypatch.setattr(
        watch, "read_pending", lambda _: (_ for _ in ()).throw(watch.TargetLost("gone"))
    )
    assert watch.run(target, once=True) == 3
    monkeypatch.setattr(watch, "read_pending", lambda _: True)
    assert (
        watch.run(
            target, poll=lambda _, pending: (_ for _ in ()).throw(BrokenPipeError())
        )
        == 0
    )


@pytest.mark.parametrize(
    "args", [["--help"], ["watch", "--help"], ["watch-codex", "--help"], ["unknown"]]
)
def test_cli_does_not_import_server(args, tmp_path):
    script = (
        "import sys\nfrom hardline_mcp.cli import main\ntry:\n main("
        + repr(args)
        + ")\nexcept SystemExit:\n pass\nassert 'hardline_mcp.server' not in sys.modules\nassert 'websockets' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "HARDLINE_DB": str(tmp_path / "never.db")},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "never.db").exists()


def test_diagnostics_escape_untrusted_text(capsys):
    watch.diagnostic("bad\nline\t\x1b[0m caf\u00e9")
    stderr = capsys.readouterr().err
    assert len(stderr.splitlines()) == 1 and "\x1b" not in stderr
    assert stderr.isascii()


def test_closed_stdout_stops_the_real_watcher_cleanly(target):
    send(target, "codex")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hardline_mcp.watch",
            "--agent",
            "codex",
            "--lane",
            "codex:mine",
            "--db",
            str(target.db),
            "--remind-after",
            "5",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    lines = []
    reader = threading.Thread(
        target=lambda: lines.append(process.stdout.readline()), daemon=True
    )
    reader.start()
    try:
        reader.join(timeout=5)
        assert not reader.is_alive(), "watcher did not flush its first notice"
        assert json.loads(lines[0])["event"] == "mail_pending"
        process.stdout.close()
        assert process.wait(timeout=10) == 0, process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        reader.join(timeout=5)
        process.stdout.close()
        process.stderr.close()
