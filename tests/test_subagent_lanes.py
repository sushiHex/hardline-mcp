"""A Codex server whose thread is not top-level says so (#109).

Seen 2026-10-19: a subagent's server held ``codex:hermes-realtime.c2b09a28``.
Its wake never pins, so two messages sent to that lane were never announced,
and when the subagent ended they were stranded; the Claude sender had been
told only ``recipient_delivery: "unknown"``.
"""

import anyio
import pytest

from hardline_mcp import delivery, mailbox, procid, server
from test_codex_queue import OTHER, Queue, call, codex, lane  # noqa: F401 - fixture


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _recorded(state):
    """Wait for this process's route row to put it in ``state``."""
    me = procid.current_identity()
    got = lambda: delivery.states_for([me], mailbox._default_now()).get(me)  # noqa: E731
    with anyio.move_on_after(5):
        while got() != state:
            await anyio.sleep(0.02)
    assert got() == state


@pytest.mark.anyio
async def test_a_sender_is_told_a_subagents_lane_is_never_announced(lane):
    async with anyio.create_task_group() as tg:
        wire, wake = await codex(tg, Queue())
        await call(wire, thread=OTHER, source="subagent")
        await _recorded(delivery.NEVER_ANNOUNCED)
        assert wake.routes["last_route_reason"] == "thread_source 'subagent'"
        sent = await server.send(from_agent="claude", to_agent=lane, message="hi")
        assert sent["recipient_delivery"] == "never_announced"
        assert "never announces" in (sent.get("recipient_note") or "")
        (me,) = [s for s in (await server.list_agents())["live_sessions"] if lane in s["lanes"]]
        assert me["delivery"] == "never_announced"
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_server_not_yet_called_is_unknown_not_never_announced(lane):
    """Absence is not evidence: a top-level thread may simply not have called."""
    async with anyio.create_task_group() as tg:
        await codex(tg, Queue())
        await anyio.sleep(0.2)  # many polls
        sent = await server.send(from_agent="claude", to_agent=lane, message="hi")
        assert sent["recipient_delivery"] == "unknown"
        assert "recipient_note" not in sent
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_top_level_call_after_a_subagents_replaces_the_marker(lane):
    queue = Queue()
    async with anyio.create_task_group() as tg:
        wire, wake = await codex(tg, queue)
        await call(wire, thread=OTHER, source="subagent")
        await _recorded(delivery.NEVER_ANNOUNCED)
        await call(wire)
        await _recorded("declared")
        assert wake.routes.get("last_route") is None, "the route row still says not top-level"
        mailbox.send("claude", lane, "one")
        await queue.wait(1)
        assert wake.routes.get("last_route") == "queue"
        tg.cancel_scope.cancel()


def test_facts_outrank_a_route_marker(tmp_path):
    """A server with push facts is reported by them, whatever its route row says."""
    db = tmp_path / "mb.db"
    now = mailbox._default_now()
    delivery.record(1, "k1", declared_at=now, db_path=db)
    for pid, key in ((1, "k1"), (2, "k2"), (3, "old")):
        delivery.record_routes(pid, key, last_route=delivery.NOT_TOP_LEVEL, db_path=db)
    delivery.record_routes(4, "k4", last_route="steer", db_path=db)
    got = delivery.states_for([(1, "k1"), (2, "k2"), (3, "k3"), (4, "k4"), (5, None)], now, db_path=db)
    assert got == {(1, "k1"): "declared", (2, "k2"): "never_announced"}
