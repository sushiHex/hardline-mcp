"""Which route each Codex notice took is a delivery fact, shown by ``report``."""

import sqlite3
from contextlib import closing

import pytest

from hardline_mcp import codex_queue, codex_steer, delivery, mailbox, procid, report
from test_codex_queue import ACCEPTED, REFUSED, THREAD, Queue, Steer, armed, meta


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    """Its own store: this process's row is otherwise shared with every test."""
    monkeypatch.setenv("HARDLINE_DB", str(tmp_path / "wake.db"))


def facts() -> dict:
    """This process's ``codex_routes`` row, or nothing."""
    pid, key = procid.current_identity()
    with closing(sqlite3.connect(mailbox._resolve_db(None))) as conn:
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM codex_routes WHERE pid = ? AND process_key = ?", (pid, key)
            ).fetchone()
        except sqlite3.OperationalError:  # nothing wrote, so nothing created the table
            row = None
    return dict(row) if row is not None else {}


def test_a_steered_notice_is_recorded_as_steered():
    armed(Steer(ACCEPTED)).send(THREAD, "notice")
    row = facts()
    assert row.get("last_route") == "steer"
    assert row.get("last_route_reason") is None
    assert (row.get("steered"), row.get("queued")) == (1, None)


def test_a_refused_steer_is_recorded_as_queued_with_the_refusal():
    armed(Steer(REFUSED)).send(THREAD, "notice")
    row = facts()
    assert row.get("last_route") == "queue"
    assert "no active turn to steer" in (row.get("last_route_reason") or "")
    assert (row.get("steered"), row.get("queued")) == (None, 1)


def test_an_unreached_steer_is_recorded_with_why():
    armed(Steer(codex_steer.Unreached("no proxy"))).send(THREAD, "notice")
    assert "no proxy" in (facts().get("last_route_reason") or "")


def test_a_notice_with_no_turn_seen_says_so():
    wake = armed(Steer(ACCEPTED))
    wake.turn = None
    wake.send(THREAD, "notice")
    assert facts().get("last_route_reason") == "no turn seen"


def test_unsteered_notices_say_why_steering_is_off():
    wake = armed(Steer(ACCEPTED))
    wake.unsteered = "HARDLINE_CODEX_STEER=0"
    wake.send(THREAD, "notice")
    assert facts().get("last_route_reason") == "HARDLINE_CODEX_STEER=0"


def test_a_steer_that_may_have_landed_is_not_claimed_as_steered():
    queue = Queue()
    wake = armed(Steer(codex_steer.Ambiguous("no answer")), queue)
    with pytest.raises(codex_steer.Ambiguous):
        wake.send(THREAD, "notice")
    row = facts()
    assert row.get("last_route") == "ambiguous"
    assert "no answer" in (row.get("last_route_reason") or "")
    assert (row.get("steered"), row.get("queued"), row.get("ambiguous")) == (None, None, 1)


def test_the_counts_accumulate_and_the_last_route_is_the_latest():
    wake = armed(Steer(ACCEPTED, ACCEPTED, REFUSED))
    for _ in range(3):
        wake.send(THREAD, "notice")
    row = facts()
    assert (row.get("steered"), row.get("queued"), row.get("last_route")) == (2, 1, "queue")


def test_a_pinned_wake_writes_its_row_before_any_notice():
    """A row at zero: a wake with nothing sent yet, not a server recording none."""
    armed(Steer(ACCEPTED))._flush()
    row = facts()
    assert row, "no row until the first notice"
    assert (row.get("last_route"), row.get("steered"), row.get("queued")) == (None, None, None)


def test_a_wake_never_pinned_writes_no_row():
    """A subagent's thread is never the address, so its server writes nothing."""
    wake = codex_queue.CodexWake(queue=Queue(), steer=Steer(ACCEPTED))
    wake.observe({"_meta": meta(source="subagent")})
    wake._flush()
    assert facts() == {}


def test_a_failed_queue_records_no_route():
    wake = armed(Steer(REFUSED), Queue(ok=False))
    with pytest.raises(RuntimeError):
        wake.send(THREAD, "notice")
    assert facts().get("last_route") is None


def test_a_store_initialised_here_still_takes_an_older_revisions_positional_write(tmp_path):
    """The mixed-revision promise: main's ``INSERT ... VALUES`` of six values,
    with no column list, must keep working against a store this code built."""
    db = tmp_path / "mb.db"
    mailbox.send("a", "b", "c", db_path=db)
    delivery.record_routes(7, "k", db_path=db, last_route="steer", steered=1)
    try:
        with closing(sqlite3.connect(db)) as raw, raw:
            raw.execute(
                "INSERT OR REPLACE INTO push_delivery VALUES (?, ?, ?, ?, ?, ?)",
                (7, "k", "2026-01-01T00:00:00Z", None, None, None),
            )
        failed = None
    except sqlite3.OperationalError as exc:
        failed = str(exc)
    assert failed is None, failed
    with closing(sqlite3.connect(db)) as raw:
        assert raw.execute("SELECT steered FROM codex_routes WHERE pid = 7").fetchone() == (1,)


def test_a_failed_push_write_does_not_lose_the_route_write(monkeypatch):
    wake = armed(Steer(ACCEPTED))
    monkeypatch.setattr(delivery, "record", lambda *a, **k: 1 / 0)
    wake.send(THREAD, "notice")
    assert facts().get("steered") == 1
    assert wake._dirty, "the push facts the store refused are retried"


def test_a_failed_route_write_does_not_lose_the_push_write(monkeypatch):
    wake = armed(Steer(ACCEPTED))
    monkeypatch.setattr(delivery, "record_routes", lambda *a, **k: 1 / 0)
    wake.send(THREAD, "notice")
    assert wake._routes_dirty, "the route facts the store refused are retried"
    assert wake._dirty is False and delivery.facts_for([procid.current_identity()])


def test_routes_of_a_dead_process_are_pruned(tmp_path, monkeypatch):
    db = tmp_path / "mb.db"
    mailbox.send("a", "b", "c", db_path=db)
    delivery.record_routes(7, "gone", db_path=db, last_route="queue", queued=1)
    delivery.record_routes(8, "alive", db_path=db, last_route="queue", queued=1)
    monkeypatch.setattr(
        delivery, "instance_state", lambda pid, key: procid.DEAD if pid == 7 else procid.ALIVE
    )
    delivery.prune(db_path=db)
    with closing(sqlite3.connect(db)) as raw:
        assert raw.execute("SELECT pid FROM codex_routes").fetchall() == [(8,)]


GONE = 2147483646


def _routes(tmp_path, monkeypatch, rows, lanes=()):
    db = tmp_path / "mb.db"
    mailbox.send("x", "nobody", "creates the store", db_path=db)
    monkeypatch.setattr(
        report, "instance_state",
        lambda pid, *_a, **_k: procid.DEAD if pid == GONE else procid.ALIVE,
    )
    raw = sqlite3.connect(db)
    with raw:
        raw.executemany(
            "INSERT INTO codex_routes (pid, process_key, last_route,"
            " last_route_reason, steered, queued, ambiguous) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        raw.executemany(
            "INSERT INTO agent_sessions (pid, lane, agent, process_key, started_at,"
            " last_seen, claimed_at) VALUES (?, ?, 'codex', ?, 't', 't', 't')",
            lanes,
        )
    raw.close()
    return db


def test_report_shows_each_codex_process_route_and_totals(tmp_path, monkeypatch):
    db = _routes(tmp_path, monkeypatch, [
        (1, "a", "steer", None, 4, 1, None),
        (2, "b", "queue", "steer refused: no active turn to steer", None, 2, 1),
        (3, "c", None, None, None, None, None),  # pinned, nothing sent yet
    ])
    got = report.report(db)["codex_routes"]
    assert (got.get("steered"), got.get("queued"), got.get("ambiguous")) == (4, 3, 1)
    by_pid = {p["pid"]: p for p in got.get("processes", [])}
    assert sorted(by_pid) == [1, 2, 3]
    assert by_pid[2].get("last_route_reason") == "steer refused: no active turn to steer"
    assert by_pid[1].get("state") == procid.ALIVE


def test_report_lists_codex_lane_servers_that_wrote_no_row(tmp_path, monkeypatch):
    """Zero totals are only as wide as the rows: the uncovered servers are listed."""
    db = _routes(
        tmp_path, monkeypatch,
        [(1, "a", None, None, None, None, None)],
        [
            (1, "codex:covered", "a"),
            (4, "codex:older", "d"),
            (4, "codex:older-too", "d"),  # one server, two lanes
            (5, "claude:not-codex", "e"),
            (GONE, "codex:gone", "g"),
        ],
    )
    got = report.report(db)["codex_routes"]
    assert got.get("codex_lane_servers_without_row") == [
        {"pid": 4, "process_key": "d", "lanes": ["codex:older", "codex:older-too"]}
    ]
    assert got.get("table") is True


def test_a_registry_that_will_not_read_leaves_the_route_rows_standing(tmp_path, monkeypatch):
    db = _routes(tmp_path, monkeypatch, [(1, "a", "steer", None, 2, None, None)])
    raw = sqlite3.connect(db)
    with raw:
        raw.execute("DROP TABLE agent_sessions")
    raw.close()
    got = report.report(db)["codex_routes"]
    assert got.get("steered") == 2
    assert "no such table" in str(got.get("codex_lane_servers_without_row"))


def test_report_reads_unavailable_when_the_route_columns_are_absent(tmp_path):
    db = tmp_path / "mb.db"
    raw = sqlite3.connect(db)
    with raw:
        raw.executescript(mailbox._SCHEMA)
        raw.execute("DROP TABLE codex_routes")
        raw.execute("CREATE TABLE codex_routes (pid INTEGER, process_key TEXT)")
    raw.close()
    assert "no such column" in report.report(db).get("codex_routes", {}).get("unavailable", "")
