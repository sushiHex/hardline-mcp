"""Server wiring smoke tests — import, tool registration, send/deliver glue."""

import json

import pytest

from hardline_mcp import server


def _immediate_submit(fn, *args, **kwargs):
    """Stand-in for _async_executor.submit that runs fn synchronously instead
    of on a pool thread — makes ask_*_async's background dispatch
    deterministic to test instead of racing a real worker thread."""
    fn(*args, **kwargs)


def test_async_dispatch_is_bounded_not_unbounded():
    # A raw threading.Thread-per-call has no ceiling; ask_*_async must go
    # through a fixed-size pool so repeated dispatches queue instead of
    # spawning unlimited concurrent agent subprocesses.
    assert server._async_executor._max_workers == server._ASYNC_MAX_WORKERS
    assert server._ASYNC_MAX_WORKERS > 0


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
    assert dispatched == {"ok": True, "dispatched": True, "label": "task-1"}

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
    assert dispatched == {"ok": True, "dispatched": True, "label": None}

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
    assert dispatched == {"ok": True, "dispatched": True, "label": "task-1"}

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
