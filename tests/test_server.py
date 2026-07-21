"""Server wiring smoke tests — import, tool registration, send/deliver glue."""

import pytest

from hardline_mcp import server


@pytest.mark.anyio
async def test_all_seven_tools_registered():
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "send",
        "inbox",
        "ack",
        "history",
        "ask_hermes",
        "ask_codex",
        "ask_claude",
    }


@pytest.mark.anyio
async def test_ask_claude_forwards_model_effort_and_mode(monkeypatch):
    captured = {}

    def fake_ask_claude(prompt, *, model=None, effort="default", mode="default"):
        captured.update(prompt=prompt, model=model, effort=effort, mode=mode)
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
    }


@pytest.mark.anyio
async def test_ask_claude_defaults_remain_backward_compatible(monkeypatch):
    captured = {}

    def fake_ask_claude(prompt, *, model=None, effort="default", mode="default"):
        captured.update(prompt=prompt, model=model, effort=effort, mode=mode)
        return {"ok": True, "reply": "old shape still works"}

    monkeypatch.setattr(server.adapters, "ask_claude", fake_ask_claude)

    result = await server.ask_claude(prompt="hello")

    assert result == {"ok": True, "reply": "old shape still works"}
    assert captured == {
        "prompt": "hello",
        "model": None,
        "effort": "default",
        "mode": "default",
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
