"""The store audit: what it classifies, and that it never writes."""

import os
import sqlite3
import subprocess
import sys
from collections import Counter

import pytest

from hardline_mcp import jobs, mailbox, procid, report, sessions


def _schema(db):
    raw = sqlite3.connect(db)
    names = sorted(r[0] for r in raw.execute("SELECT name FROM sqlite_master"))
    raw.close()
    return names


@pytest.fixture
def child():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    yield proc.pid, procid.process_key(proc.pid)
    proc.kill()
    proc.wait()


def test_revisions_are_counted_per_running_process(tmp_path, child):
    db = tmp_path / "mb.db"
    me = procid.current_identity()
    sessions.register(agent="codex", lanes=["codex:a", "codex:b"], db_path=db)
    sessions.record_revision("abc123", db_path=db)
    pid, key = child
    sessions.register(agent="codex", lane="codex:old", pid=pid, db_path=db)
    raw = sqlite3.connect(db)
    with raw:  # a dead registrant, and a stale revision for a reused pid
        raw.execute(
            "INSERT INTO agent_sessions (pid, lane, agent, process_key, started_at,"
            " last_seen, claimed_at) VALUES (2147483646, 'codex:gone', 'codex', 'k', 't', 't', 't')"
        )
        raw.execute("INSERT INTO process_revisions VALUES (?, 'earlier', 'zzz')", (pid,))
    raw.close()

    got = report.report(db)["processes"]
    assert got["revisions"] == {"abc123": 1, "unrecorded": 1}
    assert got["states"] == {procid.ALIVE: 2, procid.DEAD: 1}
    mine = next(p for p in got["registered"] if (p["pid"], p["process_key"]) == me)
    assert mine["lanes"] == ["codex:a", "codex:b"]


def test_job_notices_are_classified_by_who_acked_them(tmp_path, monkeypatch):
    db = tmp_path / "mb.db"

    def finished(owner):
        monkeypatch.setattr(jobs, "current_identity", lambda: owner)
        job = jobs.create(agent="codex", requester="claude:r", label=None, request={}, db_path=db)
        jobs.mark_running(job, db_path=db)
        jobs.finish(job, result={"ok": True}, db_path=db)
        return job

    me = procid.current_identity()
    finished(me)
    elsewhere = finished((me[0] + 1, "another-instance"))
    finished((me[0] + 2, None))  # an owner with no token proves nothing
    mailbox.send("codex", "claude:other", f'{{"type": "job_finished", "job_id": "{elsewhere}"}}', db_path=db)
    monkeypatch.setattr(mailbox, "current_identity", lambda: me)
    mailbox.inbox(["claude:r", "claude:other"], owned=["claude:r", "claude:other"], db_path=db)

    got = report.report(db)["job_notices"]
    assert (got["same_instance"], got["other_instance"], got["unknown"]) == (1, 1, 1)
    (other,) = got["other_instance_sample"]
    assert other["job_id"] == elsewhere
    assert other["dispatched_by"] == {"pid": me[0] + 1, "process_key": "another-instance"}
    assert (other["acked_by"]["pid"], other["acked_by"]["process_key"]) == me
    assert got["acked_messages_without_record"] == 0


def _other_instance_notices(db, monkeypatch, lanes, conversation):
    """One finished job per lane, dispatched elsewhere, acked here as ``conversation``."""
    monkeypatch.setattr(jobs, "current_identity", lambda: (1, "dispatcher"))
    for lane in lanes:
        job = jobs.create(agent="codex", requester=lane, label=None, request={}, db_path=db)
        jobs.mark_running(job, db_path=db)
        jobs.finish(job, result={"ok": True}, db_path=db)
    mailbox.inbox(lanes, owned=lanes, conversation=conversation, db_path=db)
    return report.report(db)["job_notices"]


def test_other_instance_notices_are_grouped_by_lane_suffix_against_the_acker(
    tmp_path, monkeypatch
):
    """The 10-27 review's relaunch reading as a fact: does the lane end in the
    acking conversation's first eight characters? Each group keeps its own ids."""
    lanes = ["claude:stray.99999999", "claude:short.abcd", "claude:held", "claude:p.abcdef12"]
    got = _other_instance_notices(
        tmp_path / "mb.db", monkeypatch, lanes, "abcdef12-3456-7890-abcd-ef1234567890"
    )
    by_suffix = got.get("other_instance_by_suffix", {})
    counts = {fact: by_suffix.get(fact, {}).get("count") for fact in report.SUFFIX_FACTS}
    assert counts == {
        "suffix_equal": 1, "suffix_differs": 1, "no_suffix": 2, "conversation_unrecorded": 0,
    }
    facts = {e["recipient"]: e.get("suffix") for e in got["other_instance_sample"]}
    assert facts == {
        "claude:p.abcdef12": "suffix_equal",
        "claude:stray.99999999": "suffix_differs",
        "claude:short.abcd": "no_suffix",  # four characters prove nothing
        "claude:held": "no_suffix",
    }
    ids = by_suffix.get("no_suffix", {}).get("message_ids", [])
    assert len(ids) == 2 and ids == sorted(ids, reverse=True)


def test_an_ack_with_no_conversation_is_unrecorded_not_a_mismatch(tmp_path, monkeypatch):
    got = _other_instance_notices(tmp_path / "mb.db", monkeypatch, ["claude:p.abcdef12"], None)
    group = got.get("other_instance_by_suffix", {}).get("conversation_unrecorded", {})
    assert group.get("count") == 1


def test_records_acting_on_a_pid_alone_are_counted_as_their_consumers_judge_them(
    tmp_path, child
):
    """A registration counts once per process, by its own token or its host's,
    unless either is shown dead; a job's owner or child only while unfinished."""
    db = tmp_path / "mb.db"
    sessions.register(agent="codex", lane="codex:mine", db_path=db)
    me = procid.current_identity()
    pid, key = child
    gone = 2147483646
    raw = sqlite3.connect(db)
    with raw:
        raw.executemany(
            "INSERT INTO agent_sessions (pid, lane, agent, process_key, host_pid, host_key,"
            " started_at, last_seen, claimed_at) VALUES (?, ?, 'codex', ?, ?, ?, 't', 't', 't')",
            [
                (pid, "codex:a", None, None, None),
                (pid, "codex:b", None, None, None),  # the same process
                (gone, "codex:gone", None, None, None),
                (pid, "codex:hosted", key, me[0], None),  # its host held as unknown
                (me[0], "codex:orphan", None, gone, "k"),  # its host is dead
            ],
        )
        raw.executemany(
            "INSERT INTO jobs (job_id, agent, requester, state, request, owner_pid,"
            " owner_key, child_pid, child_key, created_at)"
            " VALUES (?, 'codex', 'claude:r', ?, '{}', ?, ?, ?, ?, 't')",
            [
                ("queued", jobs.QUEUED, pid, None, None, None),
                ("running", jobs.RUNNING, pid, None, None, None),
                ("done", jobs.COMPLETED, pid, None, pid, None),
                ("orphaned", jobs.RUNNING, gone, None, gone, None),
                ("tokened", jobs.RUNNING, pid, key, pid, key),
                ("unverified-child", jobs.RUNNING, pid, key, pid, None),
                ("lost-owner", jobs.RUNNING, gone, "k", pid, None),  # cancel finds it lost
                ("dead-child", jobs.RUNNING, pid, key, gone, None),
            ],
        )
    raw.close()
    assert report.report(db)["tokenless"] == {"processes": 2, "unfinished_jobs": 2, "children": 1}


def test_any_body_is_free_text(tmp_path, monkeypatch):
    """A body is whatever a sender wrote: odd ones are skipped, never fatal."""
    db = tmp_path / "mb.db"
    monkeypatch.setattr(jobs, "current_identity", lambda: (1, "owner"))
    job = jobs.create(agent="codex", requester="claude:r", label=None, request={}, db_path=db)
    for body in (
        '{"type": "job_finished", "job_id": []}',
        '{"type": "job_finished", "job_id": {"x": 1}}',
        f'{{"type": "jobXfinished", "job_id": "{job}"}}',
        '["job_finished"]',
        "not json, but it says \"job_finished\"",
        f'{{"type": "note", "about": "job_finished", "job_id": "{job}"}}',
        f'{{"type":"job_finished","job_id":"{job}"}}',  # compact, and real
    ):
        mailbox.send("codex", "claude:r", body, db_path=db)
    mailbox.inbox("claude:r", owned="claude:r", db_path=db)

    got = report.report(db)["job_notices"]
    assert (got["same_instance"], got["other_instance"], got["unknown"]) == (0, 1, 0)


def test_a_registry_missing_a_column_reads_unavailable_even_when_empty(tmp_path):
    db = tmp_path / "mb.db"
    raw = sqlite3.connect(db)
    with raw:
        raw.executescript(mailbox._SCHEMA.replace("    process_key TEXT,\n    host_pid", "    host_pid"))
    raw.close()
    assert "no such column: process_key" in str(report.report(db)["processes"])


def test_probes_run_once_per_identity_after_the_snapshot_closes(tmp_path, monkeypatch, child):
    """This process is a server, the host of both servers and a legacy writer:
    probed once. And no connection of the report's is open while it probes."""
    db = tmp_path / "mb.db"
    me = procid.current_identity()
    host = {"host_pid": me[0], "host_key": me[1]}
    sessions.register(agent="codex", lane="codex:a", **host, db_path=db)
    sessions.register(agent="codex", lane="codex:b", pid=child[0], **host, db_path=db)
    raw = sqlite3.connect(db)
    with raw:
        raw.execute("CREATE TABLE sessions (pid INTEGER, lane TEXT, process_key TEXT)")
        raw.execute("INSERT INTO sessions VALUES (?, 'codex:a', ?)", me)
    raw.close()
    opened, probed = [], Counter()
    connect = sqlite3.connect

    def tracked(*args, **kwargs):
        opened.append(connect(*args, **kwargs))
        return opened[-1]

    def still_open(conn):
        try:
            conn.in_transaction
        except sqlite3.ProgrammingError:  # closed
            return False
        return True

    def probe(pid, key):
        assert not any(map(still_open, opened)), "probed with the snapshot open"
        probed[(pid, key)] += 1
        return procid.ALIVE

    monkeypatch.setattr(report.sqlite3, "connect", tracked)
    monkeypatch.setattr(report, "instance_state", probe)
    report.report(db)
    assert opened and set(probed) == {me, child} and set(probed.values()) == {1}


def test_the_audit_never_writes(tmp_path):
    """Built raw, not through mailbox: this process must not have initialized
    it, or an initializing open would find it cached and change nothing."""
    db = tmp_path / "mb.db"
    raw = sqlite3.connect(db)
    with raw:  # a store an older revision built: none of the created-on-use tables
        raw.executescript(mailbox._SCHEMA)
        for table in ("process_revisions", "message_acks", "sessions"):
            raw.execute(f"DROP TABLE IF EXISTS {table}")
        pid, key = procid.current_identity()
        raw.execute(
            "INSERT INTO agent_sessions (pid, lane, agent, process_key, started_at,"
            " last_seen, claimed_at) VALUES (?, 'codex:a', 'codex', ?, 't', 't', 't')",
            (pid, key),
        )
    raw.close()
    before = _schema(db)

    got = report.report(db)
    assert got["processes"]["revisions"] == {"unrecorded": 1}
    assert got["legacy_sessions"] == {"table": False}
    assert got["job_notices"]["acks_recorded"] is False
    assert got["job_notices"]["acked_messages_without_record"] is None
    assert _schema(db) == before

    missing = tmp_path / "none.db"
    with pytest.raises(FileNotFoundError):
        report.report(missing)
    assert not missing.exists()


def test_a_missing_table_reads_unavailable_not_zero(tmp_path):
    db = tmp_path / "mb.db"
    mailbox.send("a", "b", "c", db_path=db)
    raw = sqlite3.connect(db)
    with raw:
        raw.execute("DROP TABLE jobs")
    raw.close()
    assert "no such table: jobs" in report.report(db)["job_notices"]["unavailable"]


def test_unread_mail_is_counted_by_recipient_and_held_or_not(tmp_path, monkeypatch):
    db = tmp_path / "mb.db"
    mailbox.send("x", "nobody", "creates the store", db_path=db)
    gone = 2147483646
    monkeypatch.setattr(
        report, "instance_state",
        lambda pid, *_a, **_k: procid.DEAD if pid == gone else procid.ALIVE,
    )
    raw = sqlite3.connect(db)
    with raw:
        raw.execute("DELETE FROM messages")
        raw.executemany(
            "INSERT INTO agent_sessions (pid, lane, agent, process_key, started_at,"
            " last_seen, claimed_at) VALUES (?, ?, 'codex', 'k', 't', 't', 't')",
            [
                (1001, "codex:held"),
                (gone, "codex:dead"),
                (1002, "codex:twice"),  # two live registrations: granted to neither
                (1003, "codex:twice"),
            ],
        )
        raw.executemany(
            "INSERT INTO messages (sender, recipient, body, created_at, acked_at)"
            " VALUES ('x', ?, 'SECRET-BODY', ?, ?)",
            [
                ("codex:held", "2020-01-08T00:00:00Z", None),
                ("codex:held", "2020-01-07T00:00:00Z", None),
                ("codex:held", "2020-01-01T00:00:00Z", "2020-01-03T00:00:00Z"),  # acked
                ("claude:orphan", "2020-01-05T00:00:00Z", None),
                ("codex:dead", "2020-01-06T00:00:00Z", None),
                ("codex:twice", "2020-01-09T00:00:00Z", None),
                ("hermes", "2020-01-04T00:00:00Z", None),  # bare
                ("claude:gone", "2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z"),  # only acked
            ],
        )
    raw.close()

    got = report.report(db)
    mail = got["unread_mail"]
    assert "SECRET-BODY" not in str(got)
    totals = ("total_unread", "on_registered_lanes", "on_other_lanes", "on_bare_names")
    assert tuple(mail[k] for k in totals) == (6, 3, 2, 1)
    by_name = {e["recipient"]: e for e in mail["lanes"]}
    held = by_name["codex:held"]
    assert (held["unread"], held["oldest"], held["holder"]) == (2, "2020-01-07T00:00:00Z", "registered")
    assert held["oldest_age_days"] > 2000
    holders = {name: e["holder"] for name, e in by_name.items()}
    assert holders == {
        "codex:held": "registered", "codex:twice": "contested",
        "claude:orphan": "none", "codex:dead": "dead",
    }
    # Live registrations first though newest: a holder not reading is the one to act on.
    assert [e["recipient"] for e in mail["lanes"]] == [
        "codex:held", "codex:twice", "claude:orphan", "codex:dead",
    ]
    (bare,) = mail["bare"]
    assert (bare["recipient"], bare["unread"], bare["oldest"]) == ("hermes", 1, "2020-01-04T00:00:00Z")
    assert "holder" not in bare
    assert mail["lanes_not_shown"] == mail["bare_not_shown"] == 0


def test_mail_no_wake_got_read_is_listed_four_ways(tmp_path, monkeypatch):
    """Stalled on a receipt, unannounced by a free wake, never announced by a
    subagent's server, or unwatched: each past the grace, joined on the
    creation token, the dead never listed."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    ago = lambda minutes: mailbox._iso(now - timedelta(minutes=minutes))  # noqa: E731
    gone = 2147483646
    db = tmp_path / "mb.db"
    mailbox.send("x", "nobody", "creates the store", db_path=db)
    monkeypatch.setattr(
        report, "instance_state", lambda pid, *_a, **_k: procid.DEAD if pid == gone else procid.ALIVE
    )
    raw = sqlite3.connect(db)
    with raw:
        raw.execute("DELETE FROM messages")
        raw.executemany(
            "INSERT INTO agent_sessions (pid, lane, agent, process_key, started_at,"
            " last_seen, claimed_at) VALUES (?, ?, 'codex', ?, 't', 't', 't')",
            [(1, "codex:a", "k1"), (2, "codex:b", "k2"), (3, "codex:quiet", "k3"),
             (4, "codex:c", "k4"), (5, "codex:e", "k5"), (6, "codex:f", "new"),
             (7, "codex:d", "k7"), (8, "codex:h", "k8"), (gone, "codex:g", "kg")],
        )
        raw.executemany(
            "INSERT INTO codex_routes (pid, process_key, last_route) VALUES (?, ?, ?)",
            [(8, "k8", "not_top_level"),
             (4, "k4", "not_top_level"),    # push facts outrank the mark
             (5, "k5", "steer"),            # a route row, not the marker
             (6, "old", "not_top_level")],  # an earlier process on pid 6
        )
        raw.executemany(
            "INSERT INTO push_delivery (pid, process_key, declared_at, last_push_at,"
            " oldest_unreceipted_at) VALUES (?, ?, 't', ?, ?)",
            [(1, "k1", ago(60), ago(60)),   # owed past the grace: stalled
             (2, "k2", ago(2), ago(2)),     # owed within the grace
             (3, "k3", ago(60), ago(60)),   # owed, nothing waiting
             (4, "k4", ago(30), None),      # receipted 30 minutes ago
             (7, "k7", None, None),         # declared, never sent
             (6, "old", ago(60), ago(60)),  # an earlier process on pid 6
             (gone, "kg", ago(60), ago(60))],
        )
        raw.executemany(
            "INSERT INTO messages (sender, recipient, body, created_at) VALUES ('x', ?, 'b', ?)",
            [("codex:a", ago(1)), ("codex:a", ago(1)),  # the whole backlog waits on the receipt
             ("codex:b", ago(60)),
             ("codex:c", ago(60)),  # before the last notice: deferred, not unannounced
             ("codex:c", ago(20)),  # after it, past the grace
             ("codex:c", ago(2)),   # after it, within the grace
             ("codex:d", ago(60)),
             ("codex:e", ago(60)), ("codex:e", ago(2)),
             ("codex:f", ago(60)),
             ("codex:h", ago(60)), ("codex:h", ago(2)),
             ("codex:g", ago(60))],
        )
    raw.close()

    got = report.report(db)["stalled_wakes"]
    listed = lambda kind: [(e["pid"], e["unread"]) for e in got.get(kind, [])]  # noqa: E731
    assert listed("stalled") == [(1, {"codex:a": 2})], got
    assert listed("unannounced") == [(4, {"codex:c": 1}), (7, {"codex:d": 1})], got
    assert listed("not_top_level") == [(8, {"codex:h": 1})], got
    assert listed("unwatched") == [(5, {"codex:e": 1}), (6, {"codex:f": 1})], got
    assert got["unannounced"][0]["since"] == ago(20)
    assert (got.get("servers_with_facts"), got.get("servers_without_facts")) == (5, 3), got


def test_unread_mail_lists_are_bounded_but_totals_are_whole(tmp_path):
    db = tmp_path / "mb.db"
    for i in range(report.SAMPLE + 3):
        mailbox.send("a", f"claude:l{i:02}", "c", db_path=db)
    mail = report.report(db)["unread_mail"]
    assert mail["total_unread"] == report.SAMPLE + 3
    assert len(mail["lanes"]) == report.SAMPLE and mail["lanes_not_shown"] == 3


def test_unread_mail_is_unavailable_without_the_session_table(tmp_path):
    db = tmp_path / "mb.db"
    mailbox.send("a", "claude:b", "c", db_path=db)
    raw = sqlite3.connect(db)
    with raw:
        raw.execute("DROP TABLE agent_sessions")
    raw.close()
    assert "no such table: agent_sessions" in report.report(db)["unread_mail"]["unavailable"]


def test_the_cli_prints_the_audit_of_the_named_store(tmp_path):
    db = tmp_path / "mb.db"
    mailbox.send("a", "b", "c", db_path=db)
    run = subprocess.run(
        [sys.executable, "-m", "hardline_mcp.cli", "report", "--db", str(db)],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "HARDLINE_DB": str(tmp_path / "elsewhere.db")},
    )
    assert run.returncode == 0, run.stderr
    assert '"db_path": ' in run.stdout and "mb.db" in run.stdout
    assert not (tmp_path / "elsewhere.db").exists()
