"""A Codex server called only from a thread that is not top-level says so (#109).

Seen 2026-10-19: a subagent's server held ``codex:hermes-realtime.c2b09a28``.
Its wake never pins, so two messages sent to that lane were never announced,
and when the subagent ended they were stranded; the Claude sender had been
told only ``recipient_delivery: "unknown"``.
"""

import sqlite3
from contextlib import closing

import anyio
import pytest

from hardline_mcp import codex_queue, delivery, mailbox, procid, server
from test_codex_queue import OTHER, Queue, Steer, call, codex, lane, meta  # noqa: F401 - fixture


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _route_row() -> dict:
    """This process's ``codex_routes`` row as stored, or nothing."""
    pid, key = procid.current_identity()
    with closing(sqlite3.connect(mailbox._resolve_db(None))) as conn:
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM codex_routes WHERE pid = ? AND process_key = ?", (pid, key)
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
    return dict(row) if row is not None else {}


async def _until(check, timeout=5.0):
    with anyio.move_on_after(timeout):
        while not check():
            await anyio.sleep(0.02)
    assert check()


@pytest.mark.anyio
async def test_a_sender_is_told_a_subagents_wake_is_not_armed(lane):
    async with anyio.create_task_group() as tg:
        wire, wake = await codex(tg, Queue())
        await call(wire, thread=OTHER, source="subagent")
        await _until(lambda: _route_row().get("last_route") == delivery.NOT_TOP_LEVEL)
        assert _route_row().get("last_route_reason") == "thread_source 'subagent'"
        sent = await server.send(from_agent="claude", to_agent=lane, message="hi")
        assert sent["recipient_delivery"] == "not_top_level"
        note = sent.get("recipient_note") or ""
        assert "currently unarmed" in note and "can still read it with inbox" in note, note
        listing = await server.list_agents()
        (me,) = [s for s in listing["live_sessions"] if lane in s["lanes"]]
        assert me["delivery"] == listing["you"]["delivery"] == "not_top_level"
        assert listing["you"]["transport"] is None, "an unarmed wake sends by no transport"
        got = await server.inbox(agent="codex", auto_ack=False)
        assert [m["body"] for m in got["messages"]] == ["hi"], "the holder still reads its mail"
        tg.cancel_scope.cancel()


@pytest.mark.parametrize(
    "source, marked",
    [("guardian_review", True), (None, False), ("", False), (5, False)],
    ids=["named-source", "missing", "empty", "malformed"],
)
def test_only_a_named_source_marks_the_server(source, marked):
    """Absence is not evidence: a missing or malformed source marks nothing."""
    wake = codex_queue.CodexWake(queue=Queue(), steer=Steer())
    wake.observe({"_meta": meta(thread=OTHER, source=source)})
    assert wake.address is None
    expected = (delivery.NOT_TOP_LEVEL, f"thread_source {source!r}") if marked else (None, None)
    assert (wake.routes.get("last_route"), wake.routes.get("last_route_reason")) == expected


@pytest.mark.anyio
async def test_a_server_not_yet_called_is_unknown(lane):
    """A top-level thread may simply not have called yet."""
    async with anyio.create_task_group() as tg:
        await codex(tg, Queue())
        await anyio.sleep(0.2)  # many polls
        sent = await server.send(from_agent="claude", to_agent=lane, message="hi")
        assert sent["recipient_delivery"] == "unknown"
        assert "recipient_note" not in sent
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_top_level_call_after_a_subagents_clears_the_stored_mark(lane):
    queue = Queue()
    async with anyio.create_task_group() as tg:
        wire, wake = await codex(tg, queue)
        await call(wire, thread=OTHER, source="subagent")
        await _until(lambda: _route_row().get("last_route") == delivery.NOT_TOP_LEVEL)
        await call(wire)
        # Before any notice could write a route over it: the pin itself clears it.
        await _until(lambda: _route_row() and _route_row().get("last_route") is None)
        assert _route_row().get("last_route_reason") is None
        mailbox.send("claude", lane, "one")
        await queue.wait(1)
        tg.cancel_scope.cancel()


def test_push_facts_outrank_the_mark(tmp_path):
    db = tmp_path / "mb.db"
    now = mailbox._default_now()
    delivery.record(1, "k1", declared_at=now, db_path=db)
    for pid, key in ((1, "k1"), (2, "k2"), (3, "old")):
        delivery.record_routes(pid, key, last_route=delivery.NOT_TOP_LEVEL, db_path=db)
    delivery.record_routes(4, "k4", last_route="steer", db_path=db)
    got = delivery.states_for([(1, "k1"), (2, "k2"), (3, "k3"), (4, "k4"), (5, None)], now, db_path=db)
    assert got == {(1, "k1"): "declared", (2, "k2"): "not_top_level"}


def test_a_holder_without_a_record_keeps_the_lane_unknown(tmp_path, monkeypatch):
    """Two holders (older revisions can leave them): one marked, one silent."""
    monkeypatch.setenv("HARDLINE_DB", str(tmp_path / "mb.db"))
    for pid, key in ((1, "k1"), (2, "k2")):
        delivery.record_routes(pid, key, last_route=delivery.NOT_TOP_LEVEL)
    now = mailbox._default_now()
    for pid in (4, 5):  # receipted: pushed, nothing owed
        delivery.record(pid, f"k{pid}", declared_at=now, last_push_at=now, db_path=None)
    holder = lambda pid: {"pid": pid, "process_key": f"k{pid}"}  # noqa: E731
    assert server._delivery_of([holder(1), holder(2)]) == "not_top_level"
    assert server._delivery_of([holder(1), holder(3)]) == "unknown"
    assert server._delivery_of([holder(4), holder(5)]) == "receipted"
    assert server._delivery_of([holder(4), holder(3)]) == "unknown", "a silent holder is not receipted"
    assert server._delivery_of([holder(4), holder(1)]) == "unknown", "disagreeing holders"
