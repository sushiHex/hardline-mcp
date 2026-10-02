"""Channel push (#38), driven through the real serving loop on memory streams.

The host side was verified live (Claude Code 2.1.287): pushes wake an idle
session, are dropped silently without the launch flag, and are lost after a
move to the background. What these tests pin is the server side - what is
pushed, to whom, when again, and what counts as proof it arrived.

Python's ``ClientSession`` logs and drops notification methods it does not
know, so the tests read the server's raw output stream instead.
"""

import json
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

from hardline_mcp import channel, delivery, mailbox

LANE = "claude:fonts.1a2b3c4d"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def store(monkeypatch, tmp_path, in_session):
    db = tmp_path / "mb.db"
    monkeypatch.setenv("HARDLINE_DB", str(db))
    from hardline_mcp import server

    assert server._announce_self() == LANE
    yield db
    channel._pusher = None


class Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class Wire:
    """A raw JSON-RPC client on the server's own streams."""

    def __init__(self, to_server, from_server):
        self.to_server = to_server
        self.from_server = from_server
        self.seen: list[dict] = []

    async def send(self, obj):
        await self.to_server.send(SessionMessage(message=JSONRPCMessage.model_validate(obj)))

    async def next(self, pred, timeout=5.0):
        for i, msg in enumerate(self.seen):
            if pred(msg):
                return self.seen.pop(i)
        try:
            with anyio.fail_after(timeout):
                while True:
                    msg = (await self.from_server.receive()).message.root.model_dump(
                        by_alias=True, exclude_none=True
                    )
                    if pred(msg):
                        return msg
                    self.seen.append(msg)
        except TimeoutError:
            raise AssertionError(f"no matching message within {timeout}s") from None

    async def none(self, pred, wait=0.3):
        with anyio.move_on_after(wait):
            await self.next(pred, timeout=wait + 1)
            raise AssertionError("unexpected message")
        assert not any(pred(m) for m in self.seen), "unexpected message"

    async def handshake(self, client="claude-code"):
        await self.send({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": client, "version": "test"}},
        })
        result = (await self.next(lambda m: m.get("id") == 0))["result"]
        await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result


def is_push(msg):
    return msg.get("method") == channel.METHOD


async def serving(tg, pusher, buffer=32):
    from hardline_mcp import server

    c2s_send, c2s_recv = anyio.create_memory_object_stream(32)
    s2c_send, s2c_recv = anyio.create_memory_object_stream(buffer)
    tg.start_soon(lambda: channel.run(server.mcp, c2s_recv, s2c_send, pusher=pusher))
    return Wire(c2s_send, s2c_recv)


def _acked(db, message_id):
    with mailbox._connect(db) as conn:
        return conn.execute(
            "SELECT acked_at FROM messages WHERE id = ?", (message_id,)
        ).fetchone()[0]


@pytest.mark.anyio
async def test_initialize_declares_the_channel_and_the_standing_rule(store):
    async with anyio.create_task_group() as tg:
        wire = await serving(tg, channel.Pusher(poll_s=0.02))
        result = await wire.handshake(client="anything")
        assert result["capabilities"]["experimental"] == {channel.CAPABILITY: {}}
        assert "receipt" in result["instructions"]
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_lane_mail_is_pushed_once_with_a_receipt_and_bare_mail_never(store):
    async with anyio.create_task_group() as tg:
        wire = await serving(tg, channel.Pusher(poll_s=0.02))
        await wire.handshake()
        bare = mailbox.send("codex", "claude", "for whoever", db_path=store)
        mine = mailbox.send("codex", LANE, "for you " + "x" * 400, db_path=store)

        push = await wire.next(is_push)
        meta = push["params"]["meta"]
        assert meta["message_ids"] == str(mine["message_id"])
        assert meta["lanes"] == LANE
        assert meta["receipt"] in push["params"]["content"]
        first_line = push["params"]["content"].splitlines()[0]
        assert len(first_line) < 300, "a preview, not the whole body"
        assert str(bare["message_id"]) not in meta["message_ids"].split(",")

        await wire.none(is_push)  # pushed once, not every poll
        assert _acked(store, mine["message_id"]) is None, "pushing never acks"
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_client_that_is_not_claude_code_is_never_pushed_to(store):
    async with anyio.create_task_group() as tg:
        wire = await serving(tg, channel.Pusher(poll_s=0.02))
        await wire.handshake(client="codex-mcp-client")
        mailbox.send("codex", LANE, "hello", db_path=store)
        await wire.none(is_push)
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_unread_pushed_mail_is_reminded_on_its_own_schedule(store):
    clock = Clock()
    pusher = channel.Pusher(poll_s=0.02, clock=clock)
    async with anyio.create_task_group() as tg:
        wire = await serving(tg, pusher)
        await wire.handshake()
        old = mailbox.send("codex", LANE, "old", db_path=store)
        await wire.next(is_push)

        clock.advance(minutes=4)
        new = mailbox.send("codex", LANE, "new", db_path=store)
        push = await wire.next(is_push)
        assert push["params"]["meta"]["message_ids"] == str(new["message_id"])

        # The old message's reminder is due at 5 minutes, regardless of the
        # newer arrival - which is not due yet and must not ride along.
        clock.advance(minutes=1, seconds=1)
        push = await wire.next(is_push)
        assert push["params"]["meta"]["message_ids"] == str(old["message_id"])

        # Read by anyone: nothing left to remind.
        from hardline_mcp import server

        await server.ack(message_id=new["message_id"])
        clock.advance(minutes=20)
        push = await wire.next(is_push)
        assert push["params"]["meta"]["message_ids"] == str(old["message_id"])
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_only_a_receipt_proves_a_push_arrived(store):
    from hardline_mcp import server

    clock = Clock()
    pusher = channel.Pusher(poll_s=0.02, clock=clock)
    async with anyio.create_task_group() as tg:
        wire = await serving(tg, pusher)
        await wire.handshake()
        with anyio.fail_after(5):
            while not pusher.active:  # declared off the event loop after init
                await anyio.sleep(0.01)
        listing = await server.list_agents()
        assert listing["you"]["delivery"] == "declared"

        mailbox.send("codex", LANE, "one", db_path=store)
        receipt = (await wire.next(is_push))["params"]["meta"]["receipt"]
        assert pusher.state() == "awaiting_receipt"

        # Consuming the mail through another path proves nothing about the push.
        await server.inbox(agent="claude")
        clock.advance(minutes=11)
        assert pusher.state() == "unreceipted"

        bogus = await server.inbox(agent="claude", receipt="not-a-receipt")
        assert bogus["receipt"] == "unknown"
        assert pusher.state() == "unreceipted"

        good = await server.inbox(agent="claude", receipt=receipt)
        assert good["receipt"] == "accepted"
        assert pusher.state() == "receipted"
        # A receipt is single-use.
        again = await server.inbox(agent="claude", receipt=receipt)
        assert again["receipt"] == "unknown"
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_newer_receipt_covers_earlier_unreceipted_pushes(store):
    """Proof the channel works up to a push covers everything sent before it."""
    from hardline_mcp import server

    clock = Clock()
    pusher = channel.Pusher(poll_s=0.02, clock=clock)
    async with anyio.create_task_group() as tg:
        wire = await serving(tg, pusher)
        await wire.handshake()
        mailbox.send("codex", LANE, "one", db_path=store)
        await wire.next(is_push)
        clock.advance(minutes=1)
        mailbox.send("codex", LANE, "two", db_path=store)
        second = (await wire.next(is_push))["params"]["meta"]["receipt"]

        await server.inbox(agent="claude", auto_ack=False, receipt=second)
        assert pusher.state() == "receipted"
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_pusher_fault_never_stops_the_tools(store, monkeypatch):
    real = channel.unread
    calls = {"n": 0}

    def flaky(recipients):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("store hiccup")
        return real(recipients)

    monkeypatch.setattr(channel, "unread", flaky)
    try:
        async with anyio.create_task_group() as tg:
            wire = await serving(tg, channel.Pusher(poll_s=0.02))
            await wire.handshake()
            await wire.send({
                "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "server_info", "arguments": {}},
            })
            assert "result" in await wire.next(lambda m: m.get("id") == 7)
            mailbox.send("codex", LANE, "after the fault", db_path=store)
            await wire.next(is_push)
            assert calls["n"] > 1
            tg.cancel_scope.cancel()
    except Exception as exc:  # an escaped fault tears down the whole server
        raise AssertionError(f"a pusher fault escaped into the server: {exc!r}") from exc


@pytest.mark.anyio
async def test_mail_for_a_name_claimed_later_is_pushed(store):
    from hardline_mcp import server

    async with anyio.create_task_group() as tg:
        wire = await serving(tg, channel.Pusher(poll_s=0.02))
        await wire.handshake()
        assert (await server.register_session(label="construction"))["ok"] is True
        sent = mailbox.send("codex", "claude:construction", "renamed", db_path=store)
        push = await wire.next(is_push)
        assert push["params"]["meta"]["message_ids"] == str(sent["message_id"])
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_lane_this_process_lists_but_does_not_hold_is_not_pushed(store):
    """Grants are revalidated per batch, as consumption revalidates them.

    Local state can name a lane whose durable grant belongs to someone else -
    lost to a contest, or held by an older process. A push would advertise mail
    this session cannot read.
    """
    import subprocess
    import sys

    from hardline_mcp import adapters, sessions

    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL
    )
    try:
        sessions.register(
            agent="claude", lane="claude:construction", pid=holder.pid, db_path=store
        )
        assert adapters.claim_lane("construction") is None  # local only, no grant
        async with anyio.create_task_group() as tg:
            wire = await serving(tg, channel.Pusher(poll_s=0.02))
            await wire.handshake()
            mailbox.send("codex", "claude:construction", "not yours", db_path=store)
            await wire.none(is_push)
            tg.cancel_scope.cancel()
    finally:
        holder.kill()
        holder.wait()


@pytest.mark.anyio
async def test_the_pusher_completes_a_waiting_claim_and_says_so(store, monkeypatch):
    """A moved conversation regains its name within seconds of the old window closing."""
    import subprocess
    import sys

    from hardline_mcp import adapters, server, sessions

    monkeypatch.setattr(channel, "FULFIL_S", 0.0)
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL
    )
    try:
        sessions.register(
            agent="claude", lane="claude:fonts.0ld0ld00", pid=holder.pid, db_path=store
        )
        mailbox.send("codex", "claude:fonts.0ld0ld00", "sent before the move", db_path=store)
        waiting = await server.register_session(label="fonts.0ld0ld00", wait=True)
        assert waiting["status"] == "pending"
        async with anyio.create_task_group() as tg:
            pusher = channel.Pusher(poll_s=0.02, fulfil=server._fulfil_pending)
            wire = await serving(tg, pusher)
            await wire.handshake()
            await wire.none(is_push)
            holder.kill()
            holder.wait()
            push = await wire.next(is_push)
            content = push["params"]["content"]
            assert "now holds claude:fonts.0ld0ld00" in content
            assert "sent before the move" in content
            assert "fonts.0ld0ld00" in adapters.held_lanes()
            tg.cancel_scope.cancel()
    finally:
        holder.kill()
        holder.wait()


@pytest.mark.anyio
async def test_a_sender_sees_whether_the_recipient_is_receiving_pushes(store):
    from hardline_mcp import server

    async with anyio.create_task_group() as tg:
        wire = await serving(tg, channel.Pusher(poll_s=0.02))
        await wire.handshake()
        mailbox.send("codex", LANE, "first", db_path=store)
        await wire.next(is_push)
        result = await server.send(from_agent="codex", to_agent=LANE, message="second")
        assert result["recipient_delivery"] == "awaiting_receipt"
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_startup_fault_never_stops_the_tools(store, monkeypatch):
    real = channel.Pusher.declare
    calls = {"n": 0}

    def flaky(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("store locked at startup")
        return real(self)

    monkeypatch.setattr(channel.Pusher, "declare", flaky)
    try:
        async with anyio.create_task_group() as tg:
            wire = await serving(tg, channel.Pusher(poll_s=0.02))
            await wire.handshake()
            mailbox.send("codex", LANE, "after a bad start", db_path=store)
            await wire.next(is_push)
            assert calls["n"] == 2
            tg.cancel_scope.cancel()
    except Exception as exc:
        raise AssertionError(f"a startup fault escaped into the server: {exc!r}") from exc


@pytest.mark.anyio
async def test_a_malformed_initialize_is_answered_not_fatal(store):
    """The tap only observes. The SDK must still be the one to reject bad input."""
    try:
        async with anyio.create_task_group() as tg:
            wire = await serving(tg, channel.Pusher(poll_s=0.02))
            await wire.send({
                "jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": "oops"},
            })
            reply = await wire.next(lambda m: m.get("id") == 0)
            assert "error" in reply
            result = await wire.handshake()
            assert result["capabilities"]["experimental"] == {channel.CAPABILITY: {}}
            tg.cancel_scope.cancel()
    except Exception as exc:
        raise AssertionError(f"a malformed message stopped serving: {exc!r}") from exc


@pytest.mark.anyio
async def test_a_failed_write_is_taken_back_and_retried(store, monkeypatch):
    """A push the host never read must not be covered by a later receipt."""
    monkeypatch.setattr(channel, "SEND_TIMEOUT_S", 0.1)
    failures = []
    monkeypatch.setattr(channel, "_log", failures.append)
    pusher = channel.Pusher(poll_s=0.02)
    async with anyio.create_task_group() as tg:
        wire = await serving(tg, pusher, buffer=0)
        await wire.handshake()
        sent = mailbox.send("codex", LANE, "while nobody reads", db_path=store)
        # Nobody reads the server's output, so every attempt times out. Between
        # attempts, nothing of the unwritten push may remain on the books.
        settled = False
        with anyio.move_on_after(3):
            while not settled:
                await anyio.sleep(0.005)
                settled = (
                    any("TimeoutError" in f for f in failures)
                    and pusher.receipts == {}
                    and pusher.state() == "declared"
                )
        assert settled, f"unwritten push left on the books: {pusher.receipts}, {pusher.state()}"
        push = await wire.next(is_push)  # reading again: it goes out now
        assert push["params"]["meta"]["message_ids"] == str(sent["message_id"])
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_mail_left_unread_at_the_front_never_starves_later_mail(store, monkeypatch):
    monkeypatch.setattr(channel, "_SCAN", 2)
    async with anyio.create_task_group() as tg:
        wire = await serving(tg, channel.Pusher(poll_s=0.02))
        await wire.handshake()
        mailbox.send("codex", LANE, "one", db_path=store)
        mailbox.send("codex", LANE, "two", db_path=store)
        await wire.next(is_push)
        third = mailbox.send("codex", LANE, "three", db_path=store)
        push = await wire.next(is_push)
        assert push["params"]["meta"]["message_ids"] == str(third["message_id"])
        tg.cancel_scope.cancel()


def test_reading_delivery_state_never_waits_on_a_fact_write(store, monkeypatch):
    """``state()`` runs on the event loop (list_agents); a slow write must not
    freeze protocol handling, pings included."""
    import threading
    import time as _time

    release = threading.Event()
    monkeypatch.setattr(channel.delivery, "record", lambda *a, **k: release.wait(5))
    pusher = channel.Pusher()
    pusher.active = True
    pusher.facts = {"declared_at": datetime.now(timezone.utc)}
    writer = threading.Thread(
        target=pusher.sent, args=([1], "n", datetime.now(timezone.utc))
    )
    writer.start()
    try:
        _time.sleep(0.1)  # the write is now blocked inside delivery.record
        started = _time.monotonic()
        assert pusher.state() == "awaiting_receipt"
        assert _time.monotonic() - started < 0.5, "state() waited on the write"
    finally:
        release.set()
        writer.join(5)


@pytest.mark.anyio
async def test_ack_accepts_a_batch(store):
    from hardline_mcp import server

    a = mailbox.send("codex", LANE, "a", db_path=store)["message_id"]
    b = mailbox.send("codex", LANE, "b", db_path=store)["message_id"]
    result = await server.ack(message_ids=[a, b])
    assert result["ok"] is True
    assert set(result["results"]) == {a, b}
    assert _acked(store, a) and _acked(store, b)
    assert (await server.ack())["ok"] is False, "exactly one of the two arguments"


def test_a_missing_record_is_unknown_not_none():
    now = datetime.now(timezone.utc)
    assert delivery.derive(None, now) is None
    assert delivery.derive({"declared_at": "x"}, now) == "declared"
    old = (now - timedelta(minutes=11)).isoformat().replace("+00:00", "Z")
    row = {"last_push_at": old, "oldest_unreceipted_at": old}
    assert delivery.derive(row, now) == "unreceipted"
    assert delivery.derive({**row, "oldest_unreceipted_at": None}, now) == "receipted"
