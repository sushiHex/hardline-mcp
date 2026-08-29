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


def _enable_quota_decision(monkeypatch, selected_provider, *, reason="quota policy"):
    monkeypatch.setattr(server.adapters, "_quota_router_configured", lambda: True)
    monkeypatch.setattr(
        server.adapters,
        "_claude_quota_route",
        lambda **kwargs: {
            "requested_provider": "claude",
            "selected_provider": selected_provider,
            "reserve_percent": 5.0,
            "reserve_enforced": selected_provider != "claude",
            "quota_status": "available",
            "claude_remaining_percent": 2.0,
            "chatgpt_remaining_percent": 86.0,
            "reason": reason,
        },
    )


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
    shared_msgs, _ = server.mailbox.inbox(
        "claude", auto_ack=False, db_path=tmp_path / "mb.db"
    )
    assert shared_msgs == []
    lane_msgs, _ = server.mailbox.inbox(
        f"claude:{in_session}", auto_ack=False, db_path=tmp_path / "mb.db"
    )
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

    still_unread, _ = server.mailbox.inbox(
        "claude:other.9999zzzz", auto_ack=False, db_path=db
    )
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

    # The worker blocks on an event the TEST releases, rather than sleeping a
    # fixed duration. The old version slept 1.0s and asserted `started.is_set()`
    # immediately after dispatch, which required the pool to have SCHEDULED the
    # thread inside the 0.2s early-failure window - scheduler timing, not the
    # property under test. It failed on a loaded CI runner (ubuntu 3.13) while
    # passing everywhere else, which is the worst way for a test to be wrong.
    started = threading.Event()
    finished = threading.Event()
    release = threading.Event()

    def slow_ask(prompt, **kwargs):
        started.set()
        release.wait(timeout=30)  # cannot finish until the test says so
        finished.set()
        return {"ok": True, "reply": "eventually"}

    monkeypatch.setattr(server.adapters, "ask_codex", slow_ask)

    began = time.monotonic()
    dispatched = await server.ask_codex_async(prompt="go", from_agent="claude")
    elapsed = time.monotonic() - began

    try:
        assert dispatched["ok"] is True
        assert dispatched["dispatched"] is True
        # Wait for the worker instead of assuming it was scheduled promptly.
        assert started.wait(timeout=30), "the task never started"
        # The real property: the dispatch returned while the task was STILL
        # running. Deterministic - it cannot have finished, we never released it.
        assert not finished.is_set(), "waited the task out instead of dispatching"
        assert elapsed < 10, "returned far too slowly to be a dispatch"
    finally:
        release.set()
        # Wait for the worker to DELIVER before this test returns.
        # mailbox.send resolves _DEFAULT_PATH at call time and monkeypatch
        # restores it the instant the test ends, so a worker that delivers
        # late writes into the operator's REAL ~/.cache mailbox. That leak had
        # been dropping a stray {"reply": "eventually"} into production on
        # every run since 2026-07-30 - 71 of them by the time it was noticed.
        deadline = time.monotonic() + 30
        delivered = []
        while time.monotonic() < deadline:
            delivered, _ = server.mailbox.inbox(
                "claude", auto_ack=False, db_path=tmp_path / "mb.db"
            )
            if delivered:
                break
            time.sleep(0.02)
    assert delivered, "worker never delivered into the test mailbox"


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
    unacked, _ = server.mailbox.inbox(
        "claude:fonts.1a2b3c4d", auto_ack=False, db_path=db
    )
    assert len(unacked) == 1


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
async def test_tool_roster_is_exactly_this():
    # Named for the roster rather than a count: the count changed three times
    # and the name went stale each time while still asserting the right thing.
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "send",
        "inbox",
        "peek",
        "ack",
        "history",
        "list_agents",
        "server_info",
        "job_status",
        "job_result",
        "job_cancel",
        "list_jobs",
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


@pytest.mark.anyio
async def test_ask_claude_forwards_explicit_claude_requirement(monkeypatch):
    captured = {}

    _enable_quota_decision(monkeypatch, "claude")

    def fake_ask_claude(prompt, **kwargs):
        captured.update(prompt=prompt, **kwargs)
        return {"ok": True, "reply": "Claude-specific response"}

    monkeypatch.setattr(server.adapters, "ask_claude", fake_ask_claude)

    result = await server.ask_claude(
        prompt="Claude-specific review",
        require_claude=True,
    )

    assert result["ok"] is True
    assert "require_claude" not in captured
    assert result["routing"]["executed_agent"] == "claude"
    assert result["routing"]["decision"] == "executed"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("override", "reason", "error_fragment"),
    [
        (True, None, "non-empty override_reason"),
        (False, "orphan reason", "requires override_claude_reserve=true"),
        (True, "x" * 501, "500-character"),
    ],
)
async def test_claude_reserve_override_requires_a_visible_reason(
    monkeypatch, override, reason, error_fragment
):
    monkeypatch.setattr(server.adapters, "_quota_router_configured", lambda: True)
    monkeypatch.setattr(
        server.adapters,
        "_claude_quota_route",
        lambda **kwargs: pytest.fail("invalid override must fail before quota lookup"),
    )

    result = await server.ask_claude(
        prompt="review this",
        require_claude=True,
        override_claude_reserve=override,
        override_reason=reason,
    )

    assert result["ok"] is False
    assert error_fragment in result["error"]
    assert result["routing"]["decision"] == "blocked"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("override", "reason", "expected_invocation", "expected_reason", "truncated"),
    [
        (
            False,
            "orphan reason",
            {"override_reason": "orphan reason"},
            "orphan reason",
            False,
        ),
        (
            True,
            "x" * 501,
            {
                "claude_weekly_reserve": {
                    "bypass": True,
                    "reason": "x" * 500,
                    "authority": "caller_asserted",
                    "reason_truncated": True,
                    "supplied_reason_chars": 501,
                }
            },
            "x" * 500,
            True,
        ),
    ],
)
async def test_invalid_override_attempt_remains_in_bounded_audit(
    monkeypatch,
    override,
    reason,
    expected_invocation,
    expected_reason,
    truncated,
):
    monkeypatch.setattr(server.adapters, "_quota_router_configured", lambda: True)
    monkeypatch.setattr(
        server.adapters,
        "_claude_quota_route",
        lambda **kwargs: pytest.fail("invalid override must fail before quota lookup"),
    )

    result = await server.ask_claude(
        prompt="review this",
        require_claude=True,
        override_claude_reserve=override,
        override_reason=reason,
    )

    routing = result["routing"]
    assert routing["decision"] == "blocked"
    assert routing["invocation_overrides"] == {
        "require_claude": True,
        **expected_invocation,
    }
    assert routing["reserve_override"]["reason"] == expected_reason
    assert routing["reserve_override"].get("reason_truncated", False) is truncated
    if truncated:
        assert routing["reserve_override"]["supplied_reason_chars"] == 501


@pytest.mark.anyio
async def test_claude_reserve_override_is_rechecked_and_audited(monkeypatch):
    calls = []

    monkeypatch.setattr(server.adapters, "_quota_router_configured", lambda: True)

    def fake_route(**kwargs):
        calls.append(kwargs)
        return {
            "requested_provider": "claude",
            "selected_provider": "claude",
            "quota_status": "available",
            "reserve_percent": 100.0,
            "reserve_enforced": False,
            "reserve_override": {
                "requested": True,
                "applied": True,
                "reason": "Owner approved review A2 in this task.",
                "authority": "caller_asserted",
            },
            "reason": "Explicit one-call reserve override applied.",
        }

    monkeypatch.setattr(server.adapters, "_claude_quota_route", fake_route)
    monkeypatch.setattr(
        server.adapters,
        "ask_claude",
        lambda prompt, **kwargs: {"ok": True, "reply": "reviewed"},
    )

    result = await server.ask_claude(
        prompt="review this",
        model="opus",
        effort="max",
        require_claude=True,
        override_claude_reserve=True,
        override_reason="Owner approved review A2 in this task.",
    )

    assert result["ok"] is True
    assert calls == [
        {
            "require_claude": True,
            "override_claude_reserve": True,
            "override_reason": "Owner approved review A2 in this task.",
        },
        {
            "require_claude": True,
            "override_claude_reserve": True,
            "override_reason": "Owner approved review A2 in this task.",
        },
    ]
    assert result["routing"]["decision"] == "executed"
    assert result["routing"]["reserve_override"]["applied"] is True
    assert result["routing"]["invocation_overrides"] == {
        "model": "opus",
        "effort": "max",
        "require_claude": True,
        "claude_weekly_reserve": {
            "bypass": True,
            "reason": "Owner approved review A2 in this task.",
            "authority": "caller_asserted",
        },
    }


def test_claude_invocation_audit_lists_every_non_default_override():
    assert server._claude_invocation_overrides(
        model="opus",
        effort="max",
        mode="advisory",
        workdir="C:/review",
        write=True,
        require_claude=True,
        override_claude_reserve=True,
        override_reason="Owner approved one run.",
    ) == {
        "model": "opus",
        "effort": "max",
        "mode": "advisory",
        "workdir": "C:/review",
        "write": True,
        "require_claude": True,
        "claude_weekly_reserve": {
            "bypass": True,
            "reason": "Owner approved one run.",
            "authority": "caller_asserted",
        },
    }
    assert server._claude_invocation_overrides(
        model=None,
        effort="default",
        mode="default",
        workdir=None,
        write=False,
        require_claude=False,
        override_claude_reserve=False,
        override_reason=None,
    ) == {}


@pytest.mark.anyio
async def test_async_reserve_override_reaches_final_guard_and_job_audit(
    monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", tmp_path / "state.db")
    monkeypatch.setattr(server._async_executor, "submit", _immediate_submit)
    monkeypatch.setattr(server.adapters, "_quota_router_configured", lambda: True)

    def fake_route(**kwargs):
        calls.append(kwargs)
        return {
            "requested_provider": "claude",
            "selected_provider": "claude",
            "quota_status": "available",
            "reserve_percent": 100.0,
            "reserve_enforced": False,
            "reserve_override": {
                "requested": True,
                "applied": True,
                "reason": "Owner approved async review.",
                "authority": "caller_asserted",
            },
            "reason": "Explicit one-call reserve override applied.",
        }

    monkeypatch.setattr(server.adapters, "_claude_quota_route", fake_route)
    monkeypatch.setattr(
        server.adapters,
        "ask_claude",
        lambda prompt, **kwargs: {"ok": True, "reply": "reviewed"},
    )

    dispatched = await server.ask_claude_async(
        prompt="review this",
        from_agent="hermes",
        require_claude=True,
        override_claude_reserve=True,
        override_reason="Owner approved async review.",
    )

    assert dispatched["ok"] is True
    assert calls == [
        {
            "require_claude": True,
            "override_claude_reserve": True,
            "override_reason": "Owner approved async review.",
        },
        {
            "require_claude": True,
            "override_claude_reserve": True,
            "override_reason": "Owner approved async review.",
        },
    ]
    completed = server.jobs.get(dispatched["job_id"], db_path=tmp_path / "state.db")
    assert completed["request"]["routing"]["invocation_overrides"][
        "claude_weekly_reserve"
    ]["reason"] == "Owner approved async review."
    assert completed["result"]["routing"]["decision"] == "executed"
    assert completed["result"]["routing"]["reserve_override"]["applied"] is True


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
        mode="default",
        workdir=None,
        write=False,
    )
    assert r["ok"] is False and "unknown" in r["error"].lower()


@pytest.mark.anyio
async def test_ask_codex_async_delivers_result_via_mailbox(monkeypatch, tmp_path):
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")
    monkeypatch.setattr(server._async_executor, "submit", _immediate_submit)

    def fake_ask_codex(
        prompt,
        *,
        model=None,
        effort="default",
        mode="default",
        workdir=None,
        write=False,
        on_spawn=None,
    ):
        return {"ok": True, "reply": f"handled: {prompt}"}

    monkeypatch.setattr(server.adapters, "ask_codex", fake_ask_codex)

    dispatched = await server.ask_codex_async(
        prompt="review the diff", from_agent="claude", label="task-1"
    )
    assert dispatched["ok"] is True and dispatched["dispatched"] is True
    assert dispatched["label"] == "task-1" and dispatched["lane"] == "claude"
    assert dispatched["job_id"].startswith("job_")

    inb = await server.inbox(agent="claude")
    assert inb["count"] == 1
    assert inb["messages"][0]["sender"] == "codex"
    body = json.loads(inb["messages"][0]["body"])
    assert body["ok"] is True
    assert body["reply"] == "handled: review the diff"
    assert body["label"] == "task-1"
    # The delivered message carries the same durable handle as the receipt,
    # so a result read from the mailbox can still be traced to its job.
    assert body["job_id"] == dispatched["job_id"]


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
        prompt,
        *,
        model=None,
        effort="default",
        mode="default",
        workdir=None,
        write=False,
        on_spawn=None,
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
    assert "label" not in body  # omitted entirely, not set to None
    assert body["ok"] is True and body["reply"] == "done"
    assert body["job_id"].startswith("job_")  # durable handle always present


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
        mode="default",
        workdir=None,
        write=False,
    )
    assert r["ok"] is False and "unknown" in r["error"].lower()


@pytest.mark.anyio
async def test_ask_claude_async_delivers_result_via_mailbox(monkeypatch, tmp_path):
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")
    monkeypatch.setattr(server._async_executor, "submit", _immediate_submit)

    def fake_ask_claude(
        prompt,
        *,
        model=None,
        effort="default",
        mode="default",
        workdir=None,
        write=False,
        on_spawn=None,
    ):
        return {"ok": True, "reply": f"handled: {prompt}"}

    monkeypatch.setattr(server.adapters, "ask_claude", fake_ask_claude)

    dispatched = await server.ask_claude_async(
        prompt="refactor the retry loop", from_agent="codex", label="task-1"
    )
    assert dispatched["ok"] is True and dispatched["dispatched"] is True
    assert dispatched["label"] == "task-1" and dispatched["lane"] == "codex"
    assert dispatched["job_id"].startswith("job_")

    inb = await server.inbox(agent="codex")
    assert inb["count"] == 1
    assert inb["messages"][0]["sender"] == "claude"
    body = json.loads(inb["messages"][0]["body"])
    assert body["ok"] is True
    assert body["reply"] == "handled: refactor the retry loop"
    assert body["label"] == "task-1"
    assert body["job_id"] == dispatched["job_id"]


@pytest.mark.anyio
async def test_ask_claude_async_reports_chatgpt_as_sender_after_redirect(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")
    monkeypatch.setattr(server._async_executor, "submit", _immediate_submit)
    _enable_quota_decision(monkeypatch, "chatgpt", reason="ChatGPT has more headroom")
    codex_calls = []
    monkeypatch.setattr(
        server.adapters,
        "ask_codex",
        lambda prompt, **kwargs: codex_calls.append((prompt, kwargs)) or {
            "ok": True,
            "reply": "handled by ChatGPT",
        },
    )

    dispatched = await server.ask_claude_async(
        prompt="route this", from_agent="hermes"
    )

    message = (await server.inbox(agent="hermes"))["messages"][0]
    assert message["sender"] == "codex"
    body = json.loads(message["body"])
    assert body["routing"]["selected_provider"] == "chatgpt"
    assert body["routing"]["executed_agent"] == "codex"
    assert body["routing"]["option_mapping"]["mode"]["applied"] == "advisory"
    assert dispatched["routing"] == body["routing"]
    status = await server.job_status(job_id=dispatched["job_id"])
    assert status["job"]["agent"] == "codex"
    assert status["job"]["request"]["routing"]["decision"] == "redirected"
    assert codex_calls[0][1]["mode"] == "advisory"
    assert codex_calls[0][1]["model"] is None
    assert codex_calls[0][1]["workdir"] is None
    assert codex_calls[0][1]["write"] is False


@pytest.mark.anyio
async def test_claude_model_pin_blocks_instead_of_being_relabelled(monkeypatch):
    _enable_quota_decision(monkeypatch, "chatgpt")
    monkeypatch.setattr(
        server.adapters,
        "ask_claude",
        lambda *args, **kwargs: pytest.fail("Claude must not spawn below reserve"),
    )
    monkeypatch.setattr(
        server.adapters,
        "ask_codex",
        lambda *args, **kwargs: pytest.fail("Claude model pin is not portable"),
    )

    result = await server.ask_claude(prompt="review", model="opus")

    assert result["ok"] is False
    assert result["routing"]["decision"] == "blocked"
    assert result["routing"]["executed_agent"] is None
    assert "model pin" in result["error"].lower()


@pytest.mark.anyio
async def test_quota_policy_block_creates_no_async_job(monkeypatch):
    _enable_quota_decision(monkeypatch, None, reason="quota telemetry unavailable")
    monkeypatch.setattr(
        server._async_executor,
        "submit",
        lambda *args, **kwargs: pytest.fail("blocked work must not create a task"),
    )
    monkeypatch.setattr(
        server.adapters,
        "ask_claude",
        lambda *args, **kwargs: pytest.fail("blocked work must not spawn Claude"),
    )
    monkeypatch.setattr(
        server.adapters,
        "ask_codex",
        lambda *args, **kwargs: pytest.fail("unknown telemetry must not guess Codex"),
    )

    result = await server.ask_claude_async(prompt="review", from_agent="hermes")

    assert result["ok"] is False
    assert result["dispatched"] is False
    assert "job_id" not in result
    assert result["routing"]["decision"] == "blocked"


@pytest.mark.anyio
async def test_ask_claude_async_omits_label_when_not_supplied(monkeypatch, tmp_path):
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")
    monkeypatch.setattr(server._async_executor, "submit", _immediate_submit)
    monkeypatch.setattr(
        server.adapters, "ask_claude", lambda prompt, **k: {"ok": True, "reply": "done"}
    )

    await server.ask_claude_async(prompt="go", from_agent="hermes")

    body = json.loads((await server.inbox(agent="hermes"))["messages"][0]["body"])
    assert "label" not in body  # omitted entirely, not set to None
    assert body["ok"] is True and body["reply"] == "done"
    assert body["job_id"].startswith("job_")  # durable handle always present


@pytest.mark.anyio
async def test_auto_ack_cannot_consume_another_sessions_lane(
    monkeypatch, tmp_path, in_session
):
    """auto_ack must honour the SAME lane guard as the explicit ack tool.

    ``ack`` passes this process's lane suffix and refuses a message qualified
    to a different lane. A consuming read that skipped that guard would let
    any process call inbox(agent="claude:other") and silently drain another
    session's results - exactly the failure lanes were introduced to stop,
    and reachable through the plain default now that reads consume.
    """
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    server.mailbox.send("codex", "claude:other.9999zzzz", "theirs", db_path=db)

    got = await server.inbox(agent="claude:other.9999zzzz")
    assert [m["body"] for m in got["messages"]] == ["theirs"]  # visible

    # ...but NOT consumed: it is still unread for its real owner.
    still_unread, _ = server.mailbox.inbox(
        "claude:other.9999zzzz", auto_ack=False, db_path=db
    )
    assert len(still_unread) == 1
    assert still_unread[0]["acked_at"] is None


@pytest.mark.anyio
async def test_inbox_truncates_oversized_bodies_and_peek_returns_them_whole(
    monkeypatch, tmp_path
):
    """Bounding the message COUNT is not enough on its own: one async result
    in the real mailbox reached 35k characters, so a small batch of them still
    lands tens of thousands of tokens in the caller's context."""
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    big = "x" * (server._MAX_BODY_CHARS + 5000)
    sent = server.mailbox.send("codex", "hermes", big, db_path=db)
    server.mailbox.send("codex", "hermes", "small", db_path=db)

    got = await server.inbox(agent="hermes")
    assert got["truncated"] == 1
    shortened = got["messages"][0]
    assert len(shortened["body"]) < len(big)
    assert shortened["body_truncated"] is True
    assert shortened["body_length"] == len(big)
    assert f"peek(message_id={sent['message_id']})" in shortened["body"]
    assert got["messages"][1]["body"] == "small"  # untouched
    assert "body_truncated" not in got["messages"][1]

    # The escape hatch returns the row whole - and the stored body was never
    # modified, only the batched view of it.
    full = await server.peek(message_id=sent["message_id"])
    assert full["ok"] is True
    assert full["message"]["body"] == big


@pytest.mark.anyio
async def test_peek_reports_a_missing_message(monkeypatch, tmp_path):
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")
    got = await server.peek(message_id=999999)
    assert got["ok"] is False and "999999" in got["error"]


@pytest.mark.anyio
async def test_whole_inbox_response_is_bounded_not_just_each_body(monkeypatch, tmp_path):
    """Per-body capping alone still let a full batch reach ~200KB, which the
    host then truncated itself - so the response was cut anyway and the
    `truncated` flag understated it."""
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    for i in range(25):
        server.mailbox.send("codex", "hermes", f"{i}:" + "x" * 20_000, db_path=db)

    got = await server.inbox(agent="hermes")
    total = sum(len(m["body"]) for m in got["messages"])
    assert got["count"] == 25  # a consuming read must return everything it took
    # The real contract, with no fudge factor: the truncation note is budgeted
    # for rather than added on top, so a default-sized batch fits the target.
    # A 1.1x allowance here was hiding exactly that overshoot.
    assert total <= server._MAX_RESPONSE_CHARS
    assert got["truncated"] == 25


@pytest.mark.anyio
async def test_a_maximal_batch_stays_within_the_advertised_worst_case(
    monkeypatch, tmp_path
):
    """A consuming read may not drop what it consumed, so at the per-body
    floor a maximal batch necessarily exceeds the target budget. That ceiling
    is real and advertised rather than hidden behind the target."""
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    for i in range(server.mailbox.MAX_INBOX_LIMIT):
        server.mailbox.send("codex", "hermes", f"{i}:" + "z" * 5_000, db_path=db)

    got = await server.inbox(agent="hermes", limit=server.mailbox.MAX_INBOX_LIMIT)
    total = sum(len(m["body"]) for m in got["messages"])
    worst_case = server.mailbox.MAX_INBOX_LIMIT * (
        server._MIN_BODY_CHARS + server._NOTE_OVERHEAD
    )
    assert got["count"] == server.mailbox.MAX_INBOX_LIMIT
    assert total <= worst_case
    assert (await server.server_info())["limits"]["inbox_worst_case_chars"] == worst_case


@pytest.mark.anyio
async def test_consuming_read_returns_a_recovery_cursor(monkeypatch, tmp_path):
    """The ack commits before the response can reach the caller, so the ids
    of what was consumed are what make a lost response recoverable."""
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    ids = [
        server.mailbox.send("codex", "hermes", f"m{i}", db_path=db)["message_id"]
        for i in range(4)
    ]

    got = await server.inbox(agent="hermes", limit=3)
    assert got["first_message_id"] == ids[0]
    assert got["last_message_id"] == ids[2]
    assert "history(" in got["recover_with"]

    # The cursor really does re-fetch the consumed batch: history ignores acks.
    back = await server.history(agent="hermes", before_id=got["last_message_id"] + 1)
    assert [m["message_id"] for m in back["messages"]] == list(reversed(ids[:3]))


@pytest.mark.anyio
async def test_history_caps_the_whole_page_and_hands_back_a_cursor(monkeypatch, tmp_path):
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    for i in range(60):
        server.mailbox.send("codex", "hermes", f"{i}:" + "y" * 8_000, db_path=db)

    page = await server.history(agent="hermes", limit=50)
    total = sum(len(m["body"]) for m in page["messages"])
    assert total <= server._MAX_RESPONSE_CHARS
    assert page["dropped"] > 0          # the page really was shortened
    assert page["has_more"] is True
    assert page["next_before_id"] == page["messages"][-1]["message_id"]

    # Paging with the returned cursor continues without overlap.
    nxt = await server.history(
        agent="hermes", limit=50, before_id=page["next_before_id"]
    )
    first_ids = {m["message_id"] for m in page["messages"]}
    next_ids = {m["message_id"] for m in nxt["messages"]}
    assert not (first_ids & next_ids)


@pytest.mark.anyio
async def test_history_explains_an_unknown_agent_instead_of_returning_silence(
    monkeypatch, tmp_path
):
    """An empty page for a filtered query read as 'no messages' when it
    actually meant 'wrong name' - an agent searched its display name for a
    while before learning its mailbox identity was 'hermes'."""
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    server.mailbox.send("claude", "hermes", "real traffic", db_path=db)

    got = await server.history(agent="MrAnderson")
    assert got["count"] == 0
    assert "not a known agent" in got["hint"]
    assert "list_agents" in got["hint"]

    # A known agent with genuinely no traffic gets no misleading hint.
    quiet = await server.history(agent="codex")
    assert quiet["count"] == 0 and "hint" not in quiet


@pytest.mark.anyio
async def test_list_agents_reports_roster_observed_names_and_own_identity(
    monkeypatch, tmp_path, in_session
):
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    server.mailbox.send("claude", "hermes", "a", db_path=db)
    server.mailbox.send("codex", f"claude:{in_session}", "b", db_path=db)

    got = await server.list_agents()
    assert set(got["agents"]) == set(server.adapters.known_agents())
    observed = {r["recipient"] for r in got["observed_recipients"]}
    assert observed == {"hermes", f"claude:{in_session}"}
    assert got["you"]["lane_suffix"] == in_session
    assert got["you"]["lane_for_claude"] == f"claude:{in_session}"
    assert "codex" in got["observed_senders"]


@pytest.mark.anyio
async def test_server_info_reports_version_limits_and_timeouts(monkeypatch, tmp_path):
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")
    monkeypatch.setenv("HARDLINE_QUOTA_ROUTER_COMMAND_JSON", '["quota-router"]')
    monkeypatch.setenv("HARDLINE_CLAUDE_WEEKLY_RESERVE_PERCENT", "7")
    got = await server.server_info()
    assert got["limits"]["inbox_default"] == server.mailbox.DEFAULT_INBOX_LIMIT
    assert got["limits"]["max_response_chars"] == server._MAX_RESPONSE_CHARS
    assert set(got["timeouts_s"]) == set(server.adapters.known_agents())
    assert got["module_path"].endswith("mailbox.py")
    assert isinstance(got["write_enabled"], bool)
    assert got["quota_routing"]["configured"] is True
    assert got["quota_routing"]["claude_weekly_reserve_percent"] == 7


@pytest.mark.anyio
async def test_dispatch_returns_a_durable_handle_and_records_the_job(
    monkeypatch, tmp_path
):
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    monkeypatch.setattr(server._async_executor, "submit", _immediate_submit)
    monkeypatch.setattr(
        server.adapters, "ask_codex", lambda prompt, **k: {"ok": True, "reply": "done"}
    )

    got = await server.ask_codex_async(
        prompt="review", from_agent="claude", label="rev-1"
    )
    status = await server.job_status(job_id=got["job_id"])

    assert status["ok"] is True
    assert status["job"]["state"] == server.jobs.COMPLETED
    assert status["job"]["label"] == "rev-1"
    assert status["job"]["agent"] == "codex"
    # The prompt itself is NOT stored - only its size. A job row is metadata,
    # not a second copy of every prompt ever dispatched.
    assert status["job"]["request"]["prompt_chars"] == len("review")
    assert "prompt" not in status["job"]["request"]


@pytest.mark.anyio
async def test_job_result_survives_the_mailbox_being_consumed(monkeypatch, tmp_path):
    """The durability claim: a result recorded against the job is retrievable
    even after the delivered message has been consumed by a reading session."""
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    monkeypatch.setattr(server._async_executor, "submit", _immediate_submit)
    monkeypatch.setattr(
        server.adapters,
        "ask_claude",
        lambda prompt, **k: {"ok": True, "reply": "the review"},
    )

    got = await server.ask_claude_async(prompt="go", from_agent="hermes")
    drained = await server.inbox(agent="hermes")  # consumes the delivery
    assert drained["count"] == 1
    assert (await server.inbox(agent="hermes"))["count"] == 0  # gone from inbox

    result = await server.job_result(job_id=got["job_id"])
    assert result["ok"] is True
    assert result["state"] == server.jobs.COMPLETED
    assert result["result"]["reply"] == "the review"


@pytest.mark.anyio
async def test_job_result_refuses_an_unfinished_job(monkeypatch, tmp_path):
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    job_id = server.jobs.create(
        agent="codex", requester="claude", label=None, request={}, db_path=db
    )
    out = await server.job_result(job_id=job_id)
    assert out["ok"] is False and out["state"] == server.jobs.QUEUED


@pytest.mark.anyio
async def test_a_job_orphaned_by_a_restart_is_visible_as_lost(monkeypatch, tmp_path):
    """What a restart used to do silently: the task vanished with no record.
    Now the row survives and reports why it never finished."""
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    job_id = server.jobs.create(
        agent="claude", requester="hermes", label="long-review", request={}, db_path=db
    )
    server.jobs.mark_running(job_id, child_pid=999999, db_path=db)
    with server.mailbox._connect(db) as conn:  # simulate the owner dying
        conn.execute(
            "UPDATE jobs SET owner_pid = ? WHERE job_id = ?", (0x7FFFFFFE, job_id)
        )
        conn.commit()

    status = await server.job_status(job_id=job_id)
    assert status["job"]["state"] == server.jobs.LOST
    listed = await server.list_jobs(state=server.jobs.LOST)
    assert job_id in {j["job_id"] for j in listed["jobs"]}


@pytest.mark.anyio
async def test_job_status_and_result_report_an_unknown_id(monkeypatch, tmp_path):
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")
    assert (await server.job_status(job_id="job_nope"))["ok"] is False
    assert (await server.job_result(job_id="job_nope"))["ok"] is False


@pytest.mark.anyio
async def test_list_jobs_summarizes_and_filters(monkeypatch, tmp_path):
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    monkeypatch.setattr(server._async_executor, "submit", _immediate_submit)
    monkeypatch.setattr(
        server.adapters, "ask_codex", lambda prompt, **k: {"ok": True, "reply": "a"}
    )
    monkeypatch.setattr(
        server.adapters,
        "ask_claude",
        lambda prompt, **k: {"ok": False, "error": "nope"},
    )
    await server.ask_codex_async(prompt="x", from_agent="claude")
    await server.ask_claude_async(prompt="y", from_agent="claude")

    everything = await server.list_jobs()
    assert everything["count"] == 2
    assert everything["counts_by_state"] == {
        server.jobs.COMPLETED: 1,
        server.jobs.FAILED: 1,
    }
    assert (await server.list_jobs(agent="codex"))["count"] == 1
    assert (await server.list_jobs(active_only=True))["count"] == 0


@pytest.mark.anyio
async def test_a_job_cancelled_before_start_never_spawns(monkeypatch, tmp_path):
    """End to end at the tool boundary: a cancel that lands before the worker
    claims the job must stop the agent from being invoked at all."""
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)

    spawned = []

    def never_should_run(prompt, **k):
        spawned.append(prompt)
        return {"ok": True, "reply": "should not have happened"}

    monkeypatch.setattr(server.adapters, "ask_codex", never_should_run)

    # Cancel the job between creation and the worker claiming it, which is
    # exactly the window the conditional claim exists to close.
    real_create = server.jobs.create

    def create_then_cancel(**kwargs):
        job_id = real_create(**kwargs)
        server.jobs.request_cancel(job_id, db_path=db)
        return job_id

    monkeypatch.setattr(server.jobs, "create", create_then_cancel)
    monkeypatch.setattr(server._async_executor, "submit", _immediate_submit)

    got = await server.ask_codex_async(prompt="expensive", from_agent="claude")

    assert spawned == [], "a cancelled job still invoked the agent"
    status = await server.job_status(job_id=got["job_id"])
    assert status["job"]["state"] == server.jobs.CANCELLED


@pytest.mark.anyio
async def test_a_child_spawned_into_a_cancelled_job_is_killed_locally(
    monkeypatch, tmp_path
):
    """The window between Popen creating the child and its pid being recorded.

    A cancel landing there sees child_pid NULL, reports "cancelled before
    start", and signals nothing - so nobody else can kill it. The spawning
    process holds the handle and is the only one that can, which is why
    on_spawn's return value has to be honoured.
    """
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    monkeypatch.setattr(server._async_executor, "submit", _immediate_submit)

    killed = []
    monkeypatch.setattr(server.adapters, "_kill_tree", lambda proc: killed.append(proc))
    monkeypatch.setattr(server.adapters, "_reap", lambda proc: True)
    monkeypatch.setattr(server.jobs, "process_key", lambda pid: "tok")

    ran = []

    class _FakeChild:
        pid = 4242

        def communicate(self, timeout=None):
            # Recorded rather than raised: raising would land in the cleanup
            # path, which ALSO kills the tree, so the assertion below would
            # pass even when the failed claim was ignored.
            ran.append(timeout)
            return "", ""

        returncode = 0

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(
        server.adapters.subprocess, "Popen", lambda cmd, **kw: _FakeChild()
    )
    # Cancel between the claim and the pid being recorded.
    real_set = server.jobs.set_child_pid
    monkeypatch.setattr(
        server.jobs,
        "set_child_pid",
        lambda job_id, pid, **kw: (
            server.jobs.request_cancel(job_id, db_path=db),
            real_set(job_id, pid, **kw),
        )[1],
    )

    got = await server.ask_codex_async(prompt="expensive", from_agent="claude")
    assert ran == [], "the cancelled job's child was allowed to run"
    assert killed, "the spawning process did not kill the child it could not claim"

    # The record must stay CANCELLED. The abort returns ok:false, and finish()
    # would otherwise write `failed` over it - reporting a deliberate cancel as
    # a failure, which is a different and misleading thing.
    status = await server.job_status(job_id=got["job_id"])
    assert status["job"]["state"] == server.jobs.CANCELLED
    # ...and the requester is told, rather than left polling an inbox forever.
    delivered = json.loads(
        (await server.inbox(agent="claude"))["messages"][0]["body"]
    )
    assert delivered["ok"] is False
    assert delivered["cancelled"] is True


@pytest.mark.anyio
async def test_list_jobs_summarizes_a_huge_result_instead_of_inlining_it(
    monkeypatch, tmp_path
):
    """A listing that embedded 200 full agent replies would be the same
    context flood this package spent a release bounding everywhere else."""
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    job_id = server.jobs.create(
        agent="codex", requester="claude", label=None, request={}, db_path=db
    )
    server.jobs.mark_running(job_id, db_path=db)  # finish() is a CAS from running
    server.jobs.finish(job_id, result={"ok": True, "reply": "q" * 50_000}, db_path=db)

    listed = await server.list_jobs()
    row = listed["jobs"][0]
    assert row["result"]["truncated"] is True
    assert row["result_chars"] > 50_000
    assert f"job_result(job_id={job_id!r})" == row["result"]["full_via"]

    # ...and the escape hatch still returns it whole.
    full = await server.job_result(job_id=job_id)
    assert len(full["result"]["reply"]) == 50_000


@pytest.mark.anyio
async def test_job_cancel_stops_a_running_job(monkeypatch, tmp_path):
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    job_id = server.jobs.create(
        agent="codex", requester="claude", label=None, request={}, db_path=db
    )
    server.jobs.mark_running(job_id, child_pid=31337, db_path=db)
    killed = []
    monkeypatch.setattr(
        server.jobs,
        "kill_process_tree",
        lambda pid, expect_key=None: (killed.append(pid) or (True, None)),
    )

    out = await server.job_cancel(job_id=job_id)
    assert out["ok"] is True and killed == [31337]
    assert (await server.job_status(job_id=job_id))["job"]["state"] == (
        server.jobs.CANCELLED
    )


@pytest.mark.anyio
async def test_inbox_drains_a_backlog_across_polls(monkeypatch, tmp_path):
    """End-to-end at the tool boundary: the backlog that overflowed a real
    agent's context must drain in bounded batches rather than arrive at once."""
    db = tmp_path / "mb.db"
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", db)
    for i in range(30):
        server.mailbox.send("codex", "hermes", f"m{i}", db_path=db)

    first = await server.inbox(agent="hermes", limit=10)
    assert first["count"] == 10 and first["remaining"] == 20
    assert [m["body"] for m in first["messages"]] == [f"m{i}" for i in range(10)]

    second = await server.inbox(agent="hermes", limit=10)
    assert [m["body"] for m in second["messages"]] == [f"m{i}" for i in range(10, 20)]
    assert second["remaining"] == 10

    third = await server.inbox(agent="hermes", limit=10)
    assert third["remaining"] == 0
    assert (await server.inbox(agent="hermes"))["count"] == 0

    # Nothing was destroyed - history ignores acked_at and stays the recovery path.
    hist = await server.history(agent="hermes", limit=100)
    assert hist["count"] == 30


@pytest.mark.anyio
async def test_async_tools_round_trip(monkeypatch, tmp_path):
    # Exercise the actual async MCP tool wrappers (through _in_thread), not just
    # the sync _send_impl: send -> inbox -> ack -> inbox -> history end to end.
    monkeypatch.setattr(server.mailbox, "_DEFAULT_PATH", tmp_path / "mb.db")

    sent = await server.send(from_agent="claude", to_agent="hermes", message="hi there")
    assert sent["ok"] is True and isinstance(sent["message_id"], int)

    # auto_ack=False keeps the EXPLICIT ack below meaningful - a consuming
    # read would ack the message first and turn `acked["ok"] is True` into a
    # false assertion about a step that no longer does anything.
    inb = await server.inbox(agent="hermes", auto_ack=False)
    assert inb["count"] == 1 and inb["messages"][0]["body"] == "hi there"
    assert inb["remaining"] == 1  # nothing consumed

    acked = await server.ack(message_id=sent["message_id"])
    assert acked["ok"] is True
    assert (await server.inbox(agent="hermes"))["count"] == 0  # now read

    hist = await server.history(agent="hermes")
    assert hist["count"] == 1 and hist["messages"][0]["body"] == "hi there"
