"""Server wiring smoke tests — import, tool registration, send/deliver glue."""

import json
import threading

import pytest

from hardline_mcp import server


class _ImmediateFuture:
    """Minimal Future stand-in: the work already ran, so result() is instant."""

    def __init__(self, value):
        self._value = value

    def result(self, timeout=None):
        return self._value


def _immediate_submit(fn, *args, **kwargs):
    """Stand-in for _async_executor.submit that runs fn synchronously instead
    of on a pool thread — makes ask_*_async's background dispatch
    deterministic to test instead of racing a real worker thread.

    Returns a Future-like so the early-failure check has something to await;
    a bare None here meant every async test exercised a code path the real
    executor never takes.
    """
    return _ImmediateFuture(fn(*args, **kwargs))


@pytest.mark.anyio
async def test_async_result_goes_to_this_sessions_lane(
    monkeypatch, tmp_path, in_session
):
    """The defect this exists for: two sessions dispatched work, results were
    addressed to the shared "claude", and one session acked the other's."""
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")
    monkeypatch.setattr(server._async_executor, "submit", _immediate_submit)
    monkeypatch.setattr(
        server.adapters, "ask_codex", lambda prompt, **k: {"ok": True, "reply": "done"}
    )

    dispatched = await server.ask_codex_async(prompt="go", from_agent="claude")
    assert dispatched["lane"] == f"claude:{in_session}"

    # Addressed to the lane, not the shared name.
    assert server.mailbox.inbox("claude", db_path=tmp_path / "mb.db") == []
    lane_msgs = server.mailbox.inbox(f"claude:{in_session}", db_path=tmp_path / "mb.db")
    assert len(lane_msgs) == 1

    # ...but the owning session still finds it through the plain call it
    # already makes - no new convention to learn.
    inb = await server.inbox(agent="claude")
    assert inb["count"] == 1


@pytest.mark.anyio
async def test_inbox_sees_own_lane_and_broadcasts_but_not_other_sessions(
    monkeypatch, tmp_path, in_session
):
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    server.mailbox.send("codex", f"claude:{in_session}", "mine", db_path=db)
    server.mailbox.send("codex", "claude:other.9999zzzz", "not mine", db_path=db)
    server.mailbox.send("hermes", "claude", "broadcast", db_path=db)

    bodies = {m["body"] for m in (await server.inbox(agent="claude"))["messages"]}
    assert bodies == {"mine", "broadcast"}


@pytest.mark.anyio
async def test_ack_refuses_another_sessions_message(monkeypatch, tmp_path, in_session):
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    theirs = server.mailbox.send("codex", "claude:other.9999zzzz", "x", db_path=db)
    mine = server.mailbox.send("codex", f"claude:{in_session}", "y", db_path=db)
    shared = server.mailbox.send("codex", "claude", "z", db_path=db)

    assert (await server.ack(message_id=theirs["message_id"]))["ok"] is False
    assert (await server.ack(message_id=mine["message_id"]))["ok"] is True
    # Unqualified messages stay shared - cross-agent messaging is unaffected.
    assert (await server.ack(message_id=shared["message_id"]))["ok"] is True

    still_unread = server.mailbox.inbox("claude:other.9999zzzz", db_path=db)
    assert len(still_unread) == 1


@pytest.mark.anyio
async def test_history_still_finds_lane_messages(monkeypatch, tmp_path, in_session):
    """history() is the ack-proof recovery path - ack only sets acked_at and
    history ignores it. Filtering by exact equality silently stopped showing
    lane-addressed async results the moment lanes shipped, breaking the one
    way to retrieve a result another session had already acked."""
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    server.mailbox.send("codex", f"claude:{in_session}", "lane result", db_path=db)
    server.mailbox.send("hermes", "claude", "broadcast", db_path=db)

    bodies = {m["body"] for m in (await server.history(agent="claude"))["messages"]}
    assert bodies == {"lane result", "broadcast"}


@pytest.mark.anyio
async def test_history_survives_an_ack_by_another_session(
    monkeypatch, tmp_path, in_session
):
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    # A message to the SHARED name, which any session may ack - this is the
    # real incident: five results were addressed to bare "claude" and another
    # session acked them before their owner read them.
    msg = server.mailbox.send("codex", "claude", "shared result", db_path=db)
    # The OTHER session acks it, using ITS suffix, not ours. (Previously this
    # test passed in_session here - its own lane - so it never exercised
    # another session at all, despite the name.)
    server.mailbox.ack(msg["message_id"], lane_suffix="other.9999zzzz", db_path=db)

    assert (await server.inbox(agent="claude"))["count"] == 0  # acked, hidden
    hist = await server.history(agent="claude")
    assert [m["body"] for m in hist["messages"]] == ["shared result"]  # recoverable


@pytest.mark.anyio
async def test_explicit_lane_reads_only_that_lane(monkeypatch, tmp_path, in_session):
    """Documented as reading only that lane; it also unioned in the caller's
    OWN lane, so inbox("claude:other") leaked this session's messages."""
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    server.mailbox.send("codex", f"claude:{in_session}", "mine", db_path=db)
    server.mailbox.send("codex", "claude:other.9999zzzz", "theirs", db_path=db)

    got = await server.inbox(agent="claude:other.9999zzzz")
    assert [m["body"] for m in got["messages"]] == ["theirs"]


@pytest.mark.anyio
async def test_async_delivery_failure_falls_back_to_a_minimal_payload(
    monkeypatch, tmp_path
):
    """The delivery itself sat outside the exception backstop, so a failure
    there discarded an expensive completed run inside an unobserved future
    while the caller polled an inbox that would never fill."""
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")
    monkeypatch.setattr(server._async_executor, "submit", _immediate_submit)
    monkeypatch.setattr(
        server.adapters, "ask_codex", lambda prompt, **k: {"ok": True, "reply": "done"}
    )

    real_send = server.mailbox.send
    calls = {"n": 0}

    def flaky_send(sender, recipient, body, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("mailbox write failed")  # e.g. lock/disk/serialization
        return real_send(sender, recipient, body, **kw)

    monkeypatch.setattr(server.mailbox, "send", flaky_send)

    await server.ask_codex_async(prompt="go", from_agent="claude", label="t")

    monkeypatch.setattr(server.mailbox, "send", real_send)
    inb = await server.inbox(agent="claude")
    assert inb["count"] == 1, "a failed delivery must still reach the caller"
    body = json.loads(inb["messages"][0]["body"])
    assert body["ok"] is False
    assert "could not be delivered" in body["error"]
    assert body["label"] == "t"


@pytest.mark.anyio
async def test_slow_dispatch_still_reports_dispatched(monkeypatch, tmp_path):
    """The early-failure check must not turn a genuinely running dispatch into
    a failure, or ask_*_async stops being async at all. Uses the REAL executor
    with a task slower than the wait - the immediate-submit stub always
    completes instantly, so it cannot exercise this branch."""
    import time

    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")
    monkeypatch.setattr(server, "_ASYNC_EARLY_FAILURE_S", 0.2)

    started = threading.Event()

    def slow_ask(prompt, **kwargs):
        started.set()
        time.sleep(1.0)  # outlives the early-failure window
        return {"ok": True, "reply": "eventually"}

    monkeypatch.setattr(server.adapters, "ask_codex", slow_ask)

    began = time.monotonic()
    dispatched = await server.ask_codex_async(prompt="go", from_agent="claude")
    elapsed = time.monotonic() - began

    assert dispatched["ok"] is True
    assert dispatched["dispatched"] is True
    assert started.is_set(), "the task should actually be running"
    assert elapsed < 0.9, "must return while the task runs, not wait it out"


def test_unlaned_process_cannot_ack_a_lane(monkeypatch, tmp_path):
    """An unlaned process (Hermes, Codex, or a session missing the env)
    previously applied NO guard at all and could ack every session's mail -
    reachable by exactly the callers most likely to poll a shared mailbox."""
    db = tmp_path / "mb.db"
    laned = server.mailbox.send("codex", "claude:fonts.1a2b3c4d", "theirs", db_path=db)
    bare = server.mailbox.send("codex", "claude", "shared", db_path=db)

    assert (
        server.mailbox.ack(laned["message_id"], lane_suffix="", db_path=db)["ok"]
        is False
    )
    assert (
        server.mailbox.ack(bare["message_id"], lane_suffix="", db_path=db)["ok"] is True
    )
    assert len(server.mailbox.inbox("claude:fonts.1a2b3c4d", db_path=db)) == 1


@pytest.mark.anyio
async def test_deliver_to_a_lane_pushes_to_the_real_cli(monkeypatch, tmp_path):
    """send(to_agent="claude:lane", deliver=True) persisted but then failed
    delivery: the full lane name was handed to a dispatcher that matches the
    roster exactly, so the send half-succeeded."""
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")
    seen = {}

    def fake_deliver(agent, notice):
        seen["agent"] = agent
        seen["notice"] = notice
        return {"ok": True}

    monkeypatch.setattr(server.adapters, "deliver", fake_deliver)

    r = server._send_impl("codex", "claude:fonts.1a2b3c4d", "hi", deliver=True)

    assert r["ok"] is True
    assert r["delivery"] == {"ok": True}
    assert seen["agent"] == "claude"  # the CLI, not the lane
    # ...but the notice must still point at the lane, or the reader polls the
    # wrong inbox and never sees it.
    assert "claude:fonts.1a2b3c4d" in seen["notice"]


@pytest.mark.anyio
async def test_send_accepts_lane_qualified_names_but_still_rejects_typos(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")
    ok = server._send_impl("claude", "claude:fonts.1a2b3c4d", "hi", deliver=False)
    assert ok["ok"] is True
    typo = server._send_impl("claude", "clod:fonts.1a2b3c4d", "hi", deliver=False)
    assert typo["ok"] is False and "unknown" in typo["error"].lower()


def test_async_dispatch_is_bounded_not_unbounded():
    # A raw threading.Thread-per-call has no ceiling; ask_*_async must go
    # through a fixed-size pool so repeated dispatches queue instead of
    # spawning unlimited concurrent agent subprocesses. Asserted by
    # submitting more work than the pool can run at once and observing that
    # the excess waits, rather than by reading the pool's private
    # _max_workers back (which only restates the constructor argument).
    import threading

    workers = server._ASYNC_MAX_WORKERS
    assert workers > 0

    running = threading.Semaphore(0)
    release = threading.Event()
    try:
        for _ in range(workers + 1):
            server._async_executor.submit(lambda: (running.release(), release.wait(5)))
        # Exactly `workers` tasks can be in flight; the extra one must still
        # be queued, so a further acquire times out until the others finish.
        for _ in range(workers):
            assert running.acquire(timeout=5)
        assert not running.acquire(timeout=0.2), (
            "pool ran more than max_workers at once"
        )
    finally:
        release.set()


def test_async_executor_shutdown_is_registered_so_teardown_is_bounded():
    """ThreadPoolExecutor joins its non-daemon workers at interpreter exit,
    so without an explicit drain a full queue runs to completion before the
    process can exit. Verified separately that an `atexit` registration is a
    no-op here (threading's shutdown runs first), hence threading's own
    registry."""
    assert server._register_threading_atexit is not None
    assert callable(server._drain_async_executor_at_exit)


@pytest.mark.anyio
async def test_all_ten_tools_registered():
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "send",
        "inbox",
        "ack",
        "history",
        "ask_hermes",
        "ask_codex",
        "ask_codex_async",
        "ask_claude",
        "ask_claude_async",
    }


@pytest.mark.anyio
async def test_ask_codex_forwards_model_effort_mode_workdir_and_write(monkeypatch):
    captured = {}

    def fake_ask_codex(
        prompt,
        *,
        model=None,
        effort="default",
        mode="default",
        workdir=None,
        write=False,
    ):
        captured.update(
            prompt=prompt,
            model=model,
            effort=effort,
            mode=mode,
            workdir=workdir,
            write=write,
        )
        return {"ok": True, "reply": "reviewed", "usage": {"input_tokens": 10}}

    monkeypatch.setattr(server.adapters, "ask_codex", fake_ask_codex)

    result = await server.ask_codex(
        prompt="review this",
        model="gpt-5.6-terra",
        effort="xhigh",
        mode="advisory",
    )

    assert result["ok"] is True
    assert captured == {
        "prompt": "review this",
        "model": "gpt-5.6-terra",
        "effort": "xhigh",
        "mode": "advisory",
        "workdir": None,
        "write": False,
    }


@pytest.mark.anyio
async def test_ask_claude_forwards_model_effort_and_mode(monkeypatch):
    captured = {}

    def fake_ask_claude(
        prompt,
        *,
        model=None,
        effort="default",
        mode="default",
        workdir=None,
        write=False,
    ):
        captured.update(
            prompt=prompt,
            model=model,
            effort=effort,
            mode=mode,
            workdir=workdir,
            write=write,
        )
        return {"ok": True, "reply": "reviewed", "actual_model": "claude-fable-5"}

    monkeypatch.setattr(server.adapters, "ask_claude", fake_ask_claude)

    result = await server.ask_claude(
        prompt="review this", model="fable", effort="xhigh", mode="advisory"
    )

    assert result["ok"] is True
    assert result["actual_model"] == "claude-fable-5"
    assert captured == {
        "prompt": "review this",
        "model": "fable",
        "effort": "xhigh",
        "mode": "advisory",
        "workdir": None,
        "write": False,
    }


@pytest.mark.anyio
async def test_ask_claude_defaults_remain_backward_compatible(monkeypatch):
    captured = {}

    def fake_ask_claude(
        prompt,
        *,
        model=None,
        effort="default",
        mode="default",
        workdir=None,
        write=False,
    ):
        captured.update(
            prompt=prompt,
            model=model,
            effort=effort,
            mode=mode,
            workdir=workdir,
            write=write,
        )
        return {"ok": True, "reply": "old shape still works"}

    monkeypatch.setattr(server.adapters, "ask_claude", fake_ask_claude)

    result = await server.ask_claude(prompt="hello")

    assert result == {"ok": True, "reply": "old shape still works"}
    assert captured == {
        "prompt": "hello",
        "model": None,
        "effort": "default",
        "mode": "default",
        "workdir": None,
        "write": False,
    }


def test_send_impl_persists_without_deliver(monkeypatch, tmp_path):
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    called = []
    monkeypatch.setattr(
        server.adapters, "deliver", lambda *a, **k: called.append(a) or {"ok": True}
    )

    r = server._send_impl("claude", "hermes", "hi", deliver=False)
    assert isinstance(r["message_id"], int)
    assert "delivery" not in r
    assert called == []  # deliver adapter never invoked


def test_send_impl_invokes_deliver_when_flagged(monkeypatch, tmp_path):
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    captured = {}

    def fake_deliver(agent, notice):
        captured["agent"] = agent
        captured["notice"] = notice
        return {"ok": True}

    monkeypatch.setattr(server.adapters, "deliver", fake_deliver)
    r = server._send_impl("claude", "hermes", "hi", deliver=True)
    assert r["delivery"] == {"ok": True}
    assert captured["agent"] == "hermes"
    assert f"#{r['message_id']}" in captured["notice"]
    assert "inbox(agent='hermes')" in captured["notice"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_send_impl_rejects_unknown_agent(monkeypatch, tmp_path):
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    r = server._send_impl("claude", "bob", "hi", deliver=False)
    assert r["ok"] is False and "unknown" in r["error"].lower()
    assert server.mailbox.history(db_path=db) == []  # nothing persisted


def test_send_impl_success_has_ok_true(monkeypatch, tmp_path):
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    r = server._send_impl("claude", "hermes", "hi", deliver=False)
    assert r["ok"] is True and isinstance(r["message_id"], int)


def test_ask_codex_async_rejects_unknown_from_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")
    r = server._ask_async_impl(
        "codex",
        server.adapters.ask_codex,
        "do it",
        "bob",
        label=None,
        model=None,
        effort="default",
        workdir=None,
        write=False,
    )
    assert r["ok"] is False and "unknown" in r["error"].lower()


@pytest.mark.anyio
async def test_ask_codex_async_delivers_result_via_mailbox(monkeypatch, tmp_path):
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")
    monkeypatch.setattr(server._async_executor, "submit", _immediate_submit)

    def fake_ask_codex(
        prompt, *, model=None, effort="default", workdir=None, write=False
    ):
        return {"ok": True, "reply": f"handled: {prompt}"}

    monkeypatch.setattr(server.adapters, "ask_codex", fake_ask_codex)

    dispatched = await server.ask_codex_async(
        prompt="review the diff", from_agent="claude", label="task-1"
    )
    assert dispatched == {
        "ok": True,
        "dispatched": True,
        "label": "task-1",
        "lane": "claude",
    }

    inb = await server.inbox(agent="claude")
    assert inb["count"] == 1
    assert inb["messages"][0]["sender"] == "codex"
    body = json.loads(inb["messages"][0]["body"])
    assert body == {"ok": True, "reply": "handled: review the diff", "label": "task-1"}


@pytest.mark.anyio
async def test_ask_codex_async_survives_adapter_exception(monkeypatch, tmp_path):
    # ask_codex/ask_claude are designed to never raise (every recognized
    # failure already comes back as {"ok": False, "error"}), but if one ever
    # did, the dispatch must still notify the caller via mailbox rather than
    # dying silently on the pool thread - a caller polling inbox() would
    # otherwise wait forever with no error surfaced anywhere.
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")
    monkeypatch.setattr(server._async_executor, "submit", _immediate_submit)

    def raising_ask_codex(
        prompt, *, model=None, effort="default", workdir=None, write=False
    ):
        raise RuntimeError("unexpected adapter failure")

    monkeypatch.setattr(server.adapters, "ask_codex", raising_ask_codex)

    dispatched = await server.ask_codex_async(
        prompt="review the diff", from_agent="claude"
    )
    # A dispatch that died on arrival must NOT report itself as dispatched.
    # Returning {"ok": true, "dispatched": true} here is what convinced two
    # sessions that work was running when nothing was; one believed five
    # agents were in flight and waited on results that never existed.
    assert dispatched["ok"] is False
    assert dispatched["dispatched"] is False
    assert "unexpected adapter failure" in dispatched["error"]

    # ...and the mailbox copy is still written, so the record survives too.
    inb = await server.inbox(agent="claude")
    assert inb["count"] == 1
    body = json.loads(inb["messages"][0]["body"])
    assert body["ok"] is False
    assert "unexpected adapter failure" in body["error"]
    assert "RuntimeError" in body["error"]
    assert "traceback" in body


@pytest.mark.anyio
async def test_ask_codex_async_omits_label_when_not_supplied(monkeypatch, tmp_path):
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")
    monkeypatch.setattr(server._async_executor, "submit", _immediate_submit)
    monkeypatch.setattr(
        server.adapters, "ask_codex", lambda prompt, **k: {"ok": True, "reply": "done"}
    )

    await server.ask_codex_async(prompt="go", from_agent="hermes")

    body = json.loads((await server.inbox(agent="hermes"))["messages"][0]["body"])
    assert body == {"ok": True, "reply": "done"}  # no "label" key at all


def test_ask_claude_async_rejects_unknown_from_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")
    r = server._ask_async_impl(
        "claude",
        server.adapters.ask_claude,
        "do it",
        "bob",
        label=None,
        model=None,
        effort="default",
        workdir=None,
        write=False,
    )
    assert r["ok"] is False and "unknown" in r["error"].lower()


@pytest.mark.anyio
async def test_ask_claude_async_delivers_result_via_mailbox(monkeypatch, tmp_path):
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")
    monkeypatch.setattr(server._async_executor, "submit", _immediate_submit)

    def fake_ask_claude(
        prompt, *, model=None, effort="default", workdir=None, write=False
    ):
        return {"ok": True, "reply": f"handled: {prompt}"}

    monkeypatch.setattr(server.adapters, "ask_claude", fake_ask_claude)

    dispatched = await server.ask_claude_async(
        prompt="refactor the retry loop", from_agent="codex", label="task-1"
    )
    assert dispatched == {
        "ok": True,
        "dispatched": True,
        "label": "task-1",
        "lane": "codex",
    }

    inb = await server.inbox(agent="codex")
    assert inb["count"] == 1
    assert inb["messages"][0]["sender"] == "claude"
    body = json.loads(inb["messages"][0]["body"])
    assert body == {
        "ok": True,
        "reply": "handled: refactor the retry loop",
        "label": "task-1",
    }


@pytest.mark.anyio
async def test_ask_claude_async_omits_label_when_not_supplied(monkeypatch, tmp_path):
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")
    monkeypatch.setattr(server._async_executor, "submit", _immediate_submit)
    monkeypatch.setattr(
        server.adapters, "ask_claude", lambda prompt, **k: {"ok": True, "reply": "done"}
    )

    await server.ask_claude_async(prompt="go", from_agent="hermes")

    body = json.loads((await server.inbox(agent="hermes"))["messages"][0]["body"])
    assert body == {"ok": True, "reply": "done"}  # no "label" key at all


@pytest.mark.anyio
async def test_async_tools_round_trip(monkeypatch, tmp_path):
    # Exercise the actual async MCP tool wrappers (through _in_thread), not just
    # the sync _send_impl: send -> inbox -> ack -> inbox -> history end to end.
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")

    sent = await server.send(from_agent="claude", to_agent="hermes", message="hi there")
    assert sent["ok"] is True and isinstance(sent["message_id"], int)

    inb = await server.inbox(agent="hermes")
    assert inb["count"] == 1 and inb["messages"][0]["body"] == "hi there"

    acked = await server.ack(message_id=sent["message_id"])
    assert acked["ok"] is True
    assert (await server.inbox(agent="hermes"))["count"] == 0  # now read

    hist = await server.history(agent="hermes")
    assert hist["count"] == 1 and hist["messages"][0]["body"] == "hi there"
