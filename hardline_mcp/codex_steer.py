"""Put a notice into a running Codex turn (docs/codex-queue-wake.md, rev 3).

``turn/steer`` on the local app-server daemon adds input to the active turn of
a thread, and only if that turn is the one named: Codex checks the id and
enqueues under one lock, and a steer never starts a turn. The daemon listens on
a control socket; ``codex app-server proxy`` relays raw bytes to it, so this
module speaks WebSocket over the proxy's pipes with ``websockets``' sans-I/O
client (no event loop, no socket API that differs by platform), then JSON-RPC
as the daemon's own client does: no ``jsonrpc`` field, ``initialize`` first.

What a caller may do next depends on how far a steer got, so the outcome is
exact about it:
- ``ACCEPTED``: the daemon took it. Not proof the model saw it.
- ``REFUSED``: an error the daemon raises before admitting input (a turn
  mismatch, no active turn, thread not found, draining, unknown method).
  Nothing was enqueued.
- ``Unreached``: the steer request was never written. Nothing was enqueued.
- any other exception: the request was written, or its write began, and no
  refusal came back. It may have been enqueued - an internal error can follow
  a submission that landed.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from typing import Callable, NamedTuple, Optional, Protocol

from . import adapters

ACCEPTED, REFUSED = "accepted", "refused"
TIMEOUT_S = 10.0
_URI = "ws://localhost/"  # what the daemon's own client connects with
# JSON-RPC codes the app-server returns before input is admitted: invalid
# request (every steer precondition, "thread not found", draining), unknown
# method (a daemon without turn/steer), invalid params, overloaded. Internal
# errors are not here: one can follow a submission that was enqueued.
NOT_ADMITTED = frozenset({-32600, -32601, -32602, -32001})


class Unreached(Exception):
    """The steer request was never written: nothing can have been enqueued."""


class Ambiguous(Exception):
    """The steer request went out and no refusal came back: it may be enqueued."""


class Outcome(NamedTuple):
    status: str  # ACCEPTED or REFUSED
    detail: Optional[str]  # the daemon's refusal message
    server: Optional[str]  # the daemon's userAgent, for the log


class Pipe(Protocol):
    def send(self, data: bytes) -> None: ...
    def recv(self, deadline: float) -> bytes: ...  # b"" at end of stream
    def close(self) -> None: ...


class _Proxy:
    """``codex app-server proxy``, as a byte pipe with a deadline on reads."""

    def __init__(self, argv: Optional[list[str]] = None) -> None:
        self.proc = subprocess.Popen(
            argv or [adapters._prefix_for("codex")[0], "app-server", "proxy"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # never read, so never left to fill
            bufsize=0,
            env=adapters._agent_env(None),
        )
        self._chunks: "queue.Queue[bytes]" = queue.Queue()
        threading.Thread(target=self._pump, name="hardline-steer", daemon=True).start()

    def _pump(self) -> None:
        # Unbuffered, so a raw read returns whatever has arrived. End of stream
        # is always signalled, whatever ends the reading.
        try:
            while chunk := self.proc.stdout.read(65536):
                self._chunks.put(chunk)
        except Exception:  # noqa: BLE001 - a closed pipe, a killed proxy
            pass
        finally:
            self._chunks.put(b"")

    def send(self, data: bytes) -> None:
        # Frames here are a few hundred bytes; the pipe's buffer holds them.
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def recv(self, deadline: float) -> bytes:
        try:
            return self._chunks.get(timeout=max(deadline - time.monotonic(), 0))
        except queue.Empty:
            raise TimeoutError("the daemon did not answer in time") from None

    def close(self) -> None:
        """Bounded: a relay has no children, so a kill and a short wait."""
        try:
            self.proc.kill()
        except OSError:
            pass
        adapters._reap(self.proc, timeout=2.0)


class _Session:
    """One WebSocket connection carrying JSON-RPC, every read bounded by ``deadline``."""

    def __init__(self, pipe: Pipe, deadline: float) -> None:
        from websockets.client import ClientProtocol
        from websockets.uri import parse_uri

        self.ws = ClientProtocol(parse_uri(_URI))
        self.pipe, self.deadline = pipe, deadline

    def _flush(self) -> None:
        for chunk in self.ws.data_to_send():
            if chunk:
                self.pipe.send(chunk)

    def _receive(self) -> list:
        data = self.pipe.recv(self.deadline)
        if not data:
            raise ConnectionError("the proxy closed the connection")
        self.ws.receive_data(data)
        events = self.ws.events_received()
        self._flush()  # pongs, a close handshake
        return events

    def handshake(self) -> None:
        from websockets.http11 import Response

        self.ws.send_request(self.ws.connect())
        self._flush()
        while True:
            for event in self._receive():
                if isinstance(event, Response):
                    if self.ws.handshake_exc is not None:
                        raise self.ws.handshake_exc
                    return

    def write(self, message: dict) -> None:
        self.ws.send_text(json.dumps(message).encode("utf-8"))
        self._flush()

    def answer(self, rid: int) -> dict:
        """The response to request ``rid``. Server requests and notifications
        are skipped, never answered: this client approves nothing."""
        from websockets.frames import Opcode

        while True:
            for event in self._receive():
                opcode = getattr(event, "opcode", None)
                if opcode == Opcode.TEXT:
                    message = json.loads(event.data.decode("utf-8"))
                    if isinstance(message, dict) and message.get("id") == rid and "method" not in message:
                        return message
                elif opcode == Opcode.CLOSE:
                    raise ConnectionError("the daemon closed the connection")


def _version() -> str:
    """Shown in the daemon's user agent for this client."""
    try:
        from importlib.metadata import version

        return version("hardline-mcp")
    except Exception:  # noqa: BLE001 - a source checkout without metadata
        return "unknown"


def steer(
    thread: str,
    turn: str,
    text: str,
    *,
    timeout_s: float = TIMEOUT_S,
    connect: Optional[Callable[[], Pipe]] = None,
) -> Outcome:
    """Steer ``text`` into ``turn`` of ``thread``; see the module for outcomes."""
    deadline = time.monotonic() + timeout_s
    try:
        import websockets.client  # noqa: F401 - the codex-watch extra
    except ImportError as exc:
        raise Unreached(f"websockets is not installed ({exc})") from exc
    try:
        pipe = (connect or _Proxy)()
    except Exception as exc:  # noqa: BLE001 - nothing was written
        raise Unreached(f"proxy did not start: {type(exc).__name__}: {exc}") from exc
    try:
        try:
            session = _Session(pipe, deadline)
            session.handshake()
            session.write({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "hardline-mcp", "title": None, "version": _version()},
            }})
            init = session.answer(1)
            if "error" in init:
                raise RuntimeError(f"initialize refused: {init['error']}")
            server = (init.get("result") or {}).get("userAgent")
            session.write({"method": "initialized"})
        except Exception as exc:  # noqa: BLE001 - no steer was written yet
            raise Unreached(f"{type(exc).__name__}: {exc}") from exc
        # From the first byte of the steer frame on, a failure may have been
        # enqueued: the proxy copies stdin to the socket on its own.
        try:
            session.write({"id": 2, "method": "turn/steer", "params": {
                "threadId": thread,
                "expectedTurnId": turn,
                "input": [{"type": "text", "text": text, "text_elements": []}],
            }})
            reply = session.answer(2)
        except Exception as exc:  # noqa: BLE001
            raise Ambiguous(f"no answer to the steer: {type(exc).__name__}: {exc}") from exc
        error = reply.get("error")
        if error is None:
            return Outcome(ACCEPTED, None, server)
        if isinstance(error, dict) and error.get("code") in NOT_ADMITTED:
            return Outcome(REFUSED, str(error.get("message")), server)
        raise Ambiguous(f"steer answered with an error that may follow admission: {error}")
    finally:
        try:
            pipe.close()
        except Exception:  # noqa: BLE001 - cleanup must not replace the outcome
            pass
