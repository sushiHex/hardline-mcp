import os
import sqlite3

import pytest

from hardline_mcp import adapters, jobs, mailbox, procid, server, sessions, watch


def create(db):
    return jobs.create(
        agent="codex", requester="claude:role", label=None, request={}, db_path=db
    )


@pytest.mark.parametrize("listing", [False, True])
def test_reused_owner_pid_is_lost_without_affecting_new_instance(tmp_path, listing):
    db = tmp_path / "mb.db"
    old, new = create(db), create(db)
    assert jobs.get(new, db_path=db)["owner_key"] == procid.process_key(os.getpid())
    with mailbox._connect(db) as conn:
        conn.execute(
            "UPDATE jobs SET owner_key = 'previous-instance' WHERE job_id = ?", (old,)
        )
        conn.commit()
    rows = (
        jobs.listing(db_path=db)
        if listing
        else [jobs.get(old, db_path=db), jobs.get(new, db_path=db)]
    )
    states = {row["job_id"]: row["state"] for row in rows}
    assert states == {old: jobs.LOST, new: jobs.QUEUED}


def test_previous_owner_cannot_start_attach_or_finish(tmp_path):
    db = tmp_path / "mb.db"
    job_id = create(db)
    with mailbox._connect(db) as conn:
        conn.execute(
            "UPDATE jobs SET owner_key = 'previous-instance' WHERE job_id = ?",
            (job_id,),
        )
        conn.commit()
    assert jobs.mark_running(job_id, db_path=db) is False
    with mailbox._connect(db) as conn:
        conn.execute("UPDATE jobs SET state = 'running' WHERE job_id = ?", (job_id,))
        conn.commit()
    assert jobs.set_child_pid(job_id, 123, started_key="child", db_path=db) is False
    assert jobs.finish(job_id, result={"ok": True}, db_path=db) is False
    assert mailbox.inbox("claude:role", auto_ack=False, db_path=db)[0] == []


def test_work_owned_by_reused_pid_does_not_block_role(tmp_path):
    db = tmp_path / "mb.db"
    job_id = create(db)
    with mailbox._connect(db) as conn:
        conn.execute(
            "UPDATE jobs SET owner_key = 'previous-instance' WHERE job_id = ?",
            (job_id,),
        )
        conn.commit()
    claimed = sessions.claim(
        agent="claude", label="role", pid=os.getpid() + 10000, db_path=db
    )
    assert claimed["ok"] is True


@pytest.mark.parametrize("host_state", [procid.DEAD, procid.UNKNOWN])
def test_orphan_server_lifetime_follows_host(tmp_path, monkeypatch, host_state):
    db = tmp_path / "mb.db"
    host_pid = os.getpid() + 10000
    state = [procid.ALIVE]
    monkeypatch.setattr(
        sessions,
        "instance_state",
        lambda pid, key: state[0] if pid == host_pid else procid.ALIVE,
    )
    sessions.register(
        agent="codex", lane="codex:role", host_pid=host_pid, host_key="host", db_path=db
    )
    mailbox.send("claude", "codex:role", "waiting", db_path=db)
    state[0] = host_state
    target = watch.Target(
        db, "codex", owner_pid=os.getpid(), owner_key=procid.process_key(os.getpid())
    )
    error = watch.TargetLost if host_state == procid.DEAD else watch.Unavailable
    with pytest.raises(error):
        watch.read_pending(target)
    holders = sessions.holders("codex:role", db_path=db)
    if host_state == procid.DEAD:
        assert holders == []
        assert sessions.granted(["codex:role"], db_path=db) == ()
        assert (
            sessions.register(
                agent="codex",
                lane="codex:role",
                host_pid=host_pid,
                host_key="host",
                db_path=db,
            )["ok"]
            is False
        )
        assert sessions.live(db_path=db) == []
    else:
        assert holders[0]["liveness"] == procid.UNKNOWN
        assert (
            sessions.claim(agent="codex", label="role", pid=host_pid + 1, db_path=db)[
                "ok"
            ]
            is False
        )


def test_prune_preserves_replaced_host_identity(tmp_path, monkeypatch):
    db = tmp_path / "mb.db"
    sessions.register(agent="codex", lane="codex:role", db_path=db)

    def probe_and_replace(row):
        with mailbox._connect(db) as conn:
            conn.execute("UPDATE agent_sessions SET host_pid = 456, host_key = 'new'")
            conn.commit()
        return procid.DEAD

    monkeypatch.setattr(sessions, "_state", probe_and_replace)
    sessions.live(db_path=db)
    with mailbox._connect(db) as conn:
        rows = conn.execute("SELECT host_key FROM agent_sessions").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "new"


@pytest.mark.parametrize("listing", [False, True])
def test_lost_resolution_only_updates_probed_identity(tmp_path, monkeypatch, listing):
    db = tmp_path / "mb.db"
    job_id = create(db)

    def probe_and_replace(pid, key):
        with mailbox._connect(db) as conn:
            conn.execute(
                "UPDATE jobs SET owner_key = 'replacement' WHERE job_id = ?", (job_id,)
            )
            conn.commit()
        return procid.DEAD

    monkeypatch.setattr(jobs, "instance_state", probe_and_replace)
    row = jobs.listing(db_path=db)[0] if listing else jobs.get(job_id, db_path=db)
    assert row["state"] == jobs.QUEUED
    assert row["owner_key"] == "replacement"


def test_legacy_refresh_keeps_host_binding(tmp_path):
    db = tmp_path / "mb.db"
    pid, key = os.getpid(), procid.process_key(os.getpid())
    sessions.register(
        agent="codex", lane="codex:role", host_pid=pid, host_key=key, db_path=db
    )
    sessions.register(agent="codex", lane="codex:role", db_path=db)
    row = sessions.live(db_path=db)[0]
    assert row["host_pid"] == pid and row["host_key"] == key


def test_registration_retains_captured_host_after_reparenting(monkeypatch, tmp_path):
    monkeypatch.setenv("HARDLINE_DB", str(tmp_path / "mb.db"))
    monkeypatch.setenv("HARDLINE_AGENT", "codex")
    monkeypatch.setenv("HARDLINE_AGENT_LABEL", "role")
    monkeypatch.setattr(adapters, "_session_anchor", [])
    monkeypatch.setattr(adapters.os, "getppid", lambda: 10001)
    monkeypatch.setattr(
        procid, "ancestry", lambda *args, **kwargs: ["wrapper", "codex.exe"]
    )
    monkeypatch.setattr(procid, "parent_pid_of", lambda pid: 10002)
    monkeypatch.setattr(procid, "session_token", lambda pid: "token")
    monkeypatch.setattr(procid, "process_key", lambda pid: f"key-{pid}")
    monkeypatch.setattr(sessions, "instance_state", lambda *args: procid.ALIVE)
    assert adapters.host_identity() == {"host_pid": 10002, "host_key": "key-10002"}
    monkeypatch.setattr(adapters.os, "getppid", lambda: 10003)
    server._announce_self()
    row = sessions.live()[0]
    assert row["host_pid"] == 10002 and row["host_key"] == "key-10002"


def test_additive_identity_migration_keeps_old_writers_compatible(tmp_path):
    db = tmp_path / "old.db"
    old_schema = mailbox._SCHEMA.replace("    owner_key    TEXT,\n", "").replace(
        "    host_pid    INTEGER,\n    host_key    TEXT,\n", ""
    )
    with sqlite3.connect(db) as conn:
        conn.executescript(old_schema)
        conn.execute(
            "INSERT INTO jobs(job_id,agent,requester,state,request,owner_pid,created_at) "
            "VALUES('old','codex','claude','queued','{}',?,'t')",
            (os.getpid(),),
        )
        conn.commit()
    assert jobs.get("old", db_path=db)["state"] == jobs.QUEUED
    assert jobs.get("old", db_path=db)["owner_key"] is None
    assert jobs.mark_running("old", db_path=db)
    assert jobs.get("old", db_path=db)["owner_key"] == procid.process_key(os.getpid())
    with sqlite3.connect(db) as conn:
        # An already running older revision can still insert using its old column list.
        conn.execute(
            "INSERT INTO agent_sessions(pid,lane,agent,process_key,started_at,last_seen,claimed_at) "
            "VALUES(?,'codex:legacy','codex',?,'t','t','t')",
            (os.getpid(), procid.process_key(os.getpid())),
        )
        conn.commit()
    target = watch.Target(
        db, "codex", owner_pid=os.getpid(), owner_key=procid.process_key(os.getpid())
    )
    assert watch.read_pending(target) is False
    assert sessions.holders("codex:legacy", db_path=db)[0]["host_pid"] is None
