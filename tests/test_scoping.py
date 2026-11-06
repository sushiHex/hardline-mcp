"""Scoping by thread and by process identity, and UNKNOWN liveness.

Each test here fails against one named regression of the rule it pins (the
catalog holds the mutation): a name is scoped to its thread, a row to its
(pid, process_key), and a probe that could not answer is never a death.
"""

import sqlite3
from contextlib import closing

from hardline_mcp import bindings, delivery, jobs, mailbox, procid, sessions
from test_codex_queue import OTHER, THREAD
from test_thread_names import GONE, bind, live, me, rows, store  # noqa: F401 - fixtures


def test_a_dismissal_leaves_another_threads_binding_of_the_same_name(store):
    bind(store, "codex:role", 1, thread=OTHER)
    assert bindings.unbind("codex:role", THREAD, db_path=store) is False
    assert rows(store).get("codex:role") == (OTHER, 1, GONE, "gone")
    assert bindings.unbind("codex:role", OTHER, db_path=store) is True
    assert "codex:role" not in rows(store)


def _page(store, **kw):
    with closing(sqlite3.connect(store)) as raw:
        got = bindings.page(raw, thread=THREAD, prefix="codex:", limit=10, **kw)
    return [r["lane"] for r in got]


def test_a_page_of_bindings_is_below_its_cursor_and_under_its_prefix(store):
    bind(store, "codex:a", 1)
    bind(store, "codex:b", 2)
    bind(store, "codex:c", 3)
    bind(store, "claude:x", 4)
    assert _page(store, before=None) == ["codex:c", "codex:b", "codex:a"]
    assert _page(store, before=3) == ["codex:b", "codex:a"], "strictly below the cursor"


def test_unheld_names_are_the_threads_own_under_the_prefix_and_not_held(store):
    bind(store, "codex:a", 1)
    bind(store, "codex:b", 2)
    bind(store, "claude:x", 3)
    bind(store, "codex:z", 4, thread=OTHER)
    got = bindings.unheld(THREAD, "codex:", {"codex:a"}, db_path=store)
    assert [r["lane"] for r in got] == ["codex:b"]


def test_dropping_a_lane_leaves_another_processs_hold_of_it(store, live):
    sessions.register(agent="codex", lane="codex:role", db_path=store)
    with closing(sqlite3.connect(store)) as raw, raw:  # a conflicting row an older revision left
        raw.execute(
            "INSERT INTO agent_sessions (pid, lane, agent, process_key, started_at,"
            " last_seen, claimed_at) VALUES (?, 'codex:role', 'codex', ?, 't', 't', 't')",
            live,
        )
    assert sessions.drop_lane("codex:role", db_path=store) is True
    with closing(sqlite3.connect(store)) as raw:
        held = [r[0] for r in raw.execute(
            "SELECT pid FROM agent_sessions WHERE lane = ?", ("codex:role",)
        )]
    assert held == [live[0]]


def test_a_binding_left_by_a_dead_predecessor_of_this_pid_is_not_held(store):
    """The predecessor's pid is this process's; its creation key is not."""
    bind(store, "codex:a", 1, pid=me()[0], key="a-dead-predecessor")
    got = sessions.survey(
        THREAD, "codex:", before=None, limit=10,
        listed=["codex:a"], held=["codex:a"], pending=[], db_path=store,
    )
    assert [r["outcome"] for r in got] == ["candidate"], got


def test_work_owed_to_a_lane_by_an_unprobeable_owner_still_holds_it(tmp_path, monkeypatch):
    """UNKNOWN is not death: the owner may still be coming back for the mail."""
    db = tmp_path / "mb.db"
    job_id = jobs.create(
        agent="codex", requester="codex:construction", label=None,
        request={"prompt_chars": 1}, db_path=db,
    )
    with mailbox._connect(db) as conn, conn:
        conn.execute(
            "UPDATE jobs SET owner_pid = ?, owner_key = ? WHERE job_id = ?",
            (GONE, "somebody", job_id),
        )
    monkeypatch.setattr(sessions, "instance_state", lambda pid, key: procid.UNKNOWN)
    refused = sessions.claim(agent="codex", label="construction", db_path=db)
    assert refused["ok"] is False and refused["outstanding_jobs"], refused


def test_a_prune_keeps_what_it_could_not_probe_and_deletes_by_identity(tmp_path, monkeypatch):
    db = tmp_path / "mb.db"
    mailbox.send("a", "b", "c", db_path=db)
    for pid, key in ((7, "gone"), (8, "unprobeable"), (9, "alive")):
        delivery.record_routes(pid, key, db_path=db, last_route="queue", queued=1)
    # A reborn process sharing the dead one's pid, with a key of its own.
    delivery.record_routes(7, "reborn", db_path=db, last_route="queue", queued=1)
    state = {"gone": procid.DEAD, "unprobeable": procid.UNKNOWN}
    monkeypatch.setattr(
        delivery, "instance_state", lambda pid, key: state.get(key, procid.ALIVE)
    )
    delivery.prune(db_path=db)
    with closing(sqlite3.connect(db)) as raw:
        left = sorted(raw.execute("SELECT pid, process_key FROM codex_routes"))
    assert left == [(7, "reborn"), (8, "unprobeable"), (9, "alive")]
