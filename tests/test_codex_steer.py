"""codex_steer: a notice into a running Codex turn, against fake daemons.

The fakes speak real WebSocket (websockets' ServerProtocol): one driven
synchronously in-process, one a subprocess on real pipes. Verified live first
(2026-10-15, codex-cli 0.162.1, Windows): a wrong turn id refused, the right
one accepted and acted on mid-turn, the same id after the turn refused.
"""

import json
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("websockets", reason="install .[codex-watch] for steer tests")
from websockets.frames import Opcode  # noqa: E402
from websockets.http11 import Request  # noqa: E402
from websockets.server import ServerProtocol  # noqa: E402

from hardline_mcp import codex_steer  # noqa: E402

THREAD, TURN = "01a13d10-946a-71e3-afd2-09f6bbbd71be", "01a13d10-94c1-7c41-bfd8-df2eeeab32a4"
FAKE_PROXY = Path(__file__).with_name("fake_codex_proxy.py")
MISMATCH = {"code": -32600, "message": f"expected active turn id `x` but found `{TURN}`"}


class Daemon:
    """The daemon end of one connection, answering as each frame arrives."""

    def __init__(self, steer=None, init=None, hangup=None):
        self.server = ServerProtocol()
        self.steer = {"result": {"turnId": TURN}} if steer is None else steer
        self.init = init or {"result": {"userAgent": "codex_cli_rs/0.162.1"}}
        self.hangup = hangup  # "handshake": the proxy dies before upgrading
        self.pending: list[bytes] = []
        self.requests: list[dict] = []
        self.eof = False
        self.closed = False

    def send(self, data: bytes) -> None:
        if self.eof:
            raise BrokenPipeError("proxy gone")
        self.server.receive_data(data)
        for event in self.server.events_received():
            if isinstance(event, Request):
                if self.hangup == "handshake":
                    self.eof = True
                    return
                self.server.send_response(self.server.accept(event))
            elif event.opcode == Opcode.TEXT:
                message = json.loads(event.data)
                self.requests.append(message)
                for reply in self.answer(message):
                    self.server.send_text(json.dumps(reply).encode("utf-8"))
        self.pending += [chunk for chunk in self.server.data_to_send() if chunk]

    def answer(self, message: dict) -> list[dict]:
        method = message.get("method")
        if method == "initialize":
            return [{"id": message["id"], **self.init}]
        if method == "turn/steer" and self.steer != "silent":
            return [
                {"method": "thread/status/changed", "params": {}},  # skipped
                {"id": 7, "method": "item/tool/requestUserInput", "params": {}},  # never answered
                {"id": 99, "error": {"code": -32603, "message": "another request's"}},  # skipped
                {"id": message["id"], **self.steer},
            ]
        return []

    def recv(self, deadline: float) -> bytes:
        if self.pending:
            return self.pending.pop(0)
        if self.eof:
            return b""
        raise TimeoutError("nothing more from the daemon")

    def close(self) -> None:
        self.closed = True


def steer(daemon: Daemon, **kw):
    return codex_steer.steer(THREAD, TURN, "[hardline] notice", connect=lambda: daemon, **kw)


def ending(call):
    """How a steer ended, as a value to assert on: its outcome's status, or
    the class of what it raised."""
    try:
        return call().status
    except Exception as exc:  # noqa: BLE001 - the class is the answer
        return type(exc)


def test_an_accepted_steer_names_the_turn_and_the_server():
    daemon = Daemon()
    assert steer(daemon) == (codex_steer.ACCEPTED, None, "codex_cli_rs/0.162.1")
    methods = [m.get("method") for m in daemon.requests]
    assert methods == ["initialize", "initialized", "turn/steer"]
    params = daemon.requests[-1]["params"]
    assert (params["threadId"], params["expectedTurnId"]) == (THREAD, TURN)
    assert params["input"] == [{"type": "text", "text": "[hardline] notice", "text_elements": []}]
    assert "jsonrpc" not in daemon.requests[0], "the daemon's own client sends none"
    assert daemon.closed
    assert not any(r.get("id") == 7 for r in daemon.requests), "a server request is never answered"


@pytest.mark.parametrize("code", sorted(codex_steer.NOT_ADMITTED))
def test_an_error_raised_before_admission_is_a_refusal(code):
    outcome = steer(Daemon(steer={"error": {**MISMATCH, "code": code}}))
    assert (outcome.status, outcome.detail) == (codex_steer.REFUSED, MISMATCH["message"])


def test_an_internal_error_may_follow_admission_so_is_ambiguous():
    """steer_turn's own failure maps to -32603 after the submission landed."""
    daemon = Daemon(steer={"error": {"code": -32603, "message": "failed to steer turn"}})
    assert ending(lambda: steer(daemon)) is codex_steer.Ambiguous


def test_a_steer_left_unanswered_is_ambiguous():
    assert ending(lambda: steer(Daemon(steer="silent"))) is codex_steer.Ambiguous


@pytest.mark.parametrize(
    "daemon",
    [Daemon(hangup="handshake"), Daemon(init={"error": {"code": -32603, "message": "no"}})],
    ids=["no-handshake", "initialize-refused"],
)
def test_a_failure_before_the_steer_is_written_is_unreached(daemon):
    assert ending(lambda: steer(daemon)) is codex_steer.Unreached
    assert not any(m.get("method") == "turn/steer" for m in daemon.requests)
    assert daemon.closed


def test_a_proxy_that_will_not_start_is_unreached():
    def broken():
        raise FileNotFoundError("codex")

    with pytest.raises(codex_steer.Unreached):
        codex_steer.steer(THREAD, TURN, "x", connect=broken)


def test_without_websockets_nothing_is_reached(monkeypatch):
    monkeypatch.setitem(sys.modules, "websockets.client", None)
    with pytest.raises(codex_steer.Unreached, match="websockets"):
        steer(Daemon())


# ── a real proxy process ────────────────────────────────────────────────────


def proxy(mode: str, made: list):
    def connect():
        made.append(codex_steer._Proxy([sys.executable, str(FAKE_PROXY), mode]))
        return made[-1]

    return connect


def test_over_real_pipes_a_steer_is_accepted_and_the_proxy_reaped():
    made: list = []
    run = lambda: codex_steer.steer(THREAD, TURN, "x", connect=proxy("accept", made), timeout_s=60)  # noqa: E731
    assert ending(run) == codex_steer.ACCEPTED
    assert made[0].proc.poll() is not None, "the proxy outlived its steer"


def test_over_real_pipes_silence_is_bounded_and_ambiguous():
    made: list = []
    started = time.monotonic()
    # Room for a loaded runner's interpreter start; the bound is what's asserted.
    run = lambda: codex_steer.steer(THREAD, TURN, "x", connect=proxy("silent", made), timeout_s=8)  # noqa: E731
    assert ending(run) is codex_steer.Ambiguous
    assert time.monotonic() - started < 30
    assert made[0].proc.poll() is not None
