"""Deliver mailbox observations to an explicitly bound Codex app-server thread."""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import re
import time
import uuid
from urllib.parse import urlsplit

from . import watch


def local_endpoint(value: str) -> str:
    """Limit the standalone adapter to explicit local addresses, without DNS."""
    try:
        url = urlsplit(value)
        if (
            url.scheme != "ws"
            or not ipaddress.ip_address(url.hostname or "").is_loopback
            or not url.port
            or url.username is not None
            or url.password is not None
            or url.path not in ("", "/")
            or url.query
            or url.fragment
        ):
            raise ValueError()
    except ValueError:
        raise argparse.ArgumentTypeError(
            "endpoint must be ws://<loopback IP>:<port> without credentials or a path"
        ) from None
    return value


def thread_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise argparse.ArgumentTypeError(
            "--thread must be an exact thread UUID, not a name"
        ) from None


class CodexWake:
    """One bounded RPC connection; no thread creation, resumption or overrides.

    The owning host must retain its approval/UI handling. A request for user
    input on this observer connection is reported, never answered for the user.
    """

    def __init__(
        self,
        endpoint: str,
        thread: str,
        target: watch.Target,
        *,
        remind_after: float = 30,
        timeout: float = 5,
        clock=time.monotonic,
        connect=None,
        report=watch.diagnostic,
    ):
        self.endpoint = local_endpoint(endpoint)
        self.thread = thread_uuid(thread)
        self.target = target
        self.remind_after = remind_after
        self.timeout = timeout
        self.clock = clock
        self.connect = connect
        self.report = report
        self.socket = None
        self._connections = contextlib.ExitStack()
        self.request_id = 0
        self.uncertain_until = 0.0

    def close(self) -> None:
        self.socket = None
        connections, self._connections = self._connections, contextlib.ExitStack()
        connections.close()

    def _rpc(self, method: str, params: dict) -> dict:
        self.request_id += 1
        request_id = self.request_id
        self.socket.send(
            json.dumps({"id": request_id, "method": method, "params": params})
        )
        deadline = self.clock() + self.timeout
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError(f"Codex did not answer {method}")
            message = json.loads(self.socket.recv(timeout=remaining))
            if not isinstance(message, dict):
                raise RuntimeError("invalid app-server response")
            if "method" in message:
                if "id" in message:
                    raise RuntimeError(
                        f"Codex requires interactive handling of {message['method']}; "
                        "use an adapter integrated with the owning host"
                    )
                continue  # Notifications are not replies or delivery receipts.
            if message.get("id") != request_id:
                raise RuntimeError("app-server reply has an unexpected request id")
            if "error" in message:
                error = message["error"]
                if (
                    not isinstance(error, dict)
                    or not isinstance(error.get("code"), int)
                    or not isinstance(error.get("message"), str)
                ):
                    raise RuntimeError("invalid app-server error response")
                if method == "turn/start":
                    # A matching rejection proves this submission did not run.
                    self.uncertain_until = 0.0
                if error.get("code") == -32001:
                    raise watch.Unavailable("Codex app-server is overloaded")
                raise RuntimeError(
                    f"Codex rejected {method}: {error.get('message', error)}"
                )
            result = message.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("app-server response is missing an object result")
            return result

    def _open(self) -> None:
        if self.socket is not None:
            return
        if self.connect is None:
            try:
                from websockets.sync.client import connect
            except ImportError:
                raise RuntimeError(
                    "install hardline-mcp[codex-watch] to use watch-codex"
                ) from None
        else:
            connect = self.connect
        self.socket = self._connections.enter_context(
            connect(
                self.endpoint,
                open_timeout=self.timeout,
                close_timeout=0.25,
                max_size=1_048_576,
                proxy=None,
            )
        )
        initialized = self._rpc(
            "initialize",
            {
                "clientInfo": {"name": "hardline_watch", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        # Older servers silently ignore toolOutput. Use the connected runtime's
        # version, not a CLI on PATH; 0.153.4 is our verified compatibility floor.
        agent = initialized.get("userAgent")
        version = (
            re.match(r"^[^/\s]+/(\d+)\.(\d+)\.(\d+)(?:\s|$)", agent)
            if isinstance(agent, str)
            else None
        )
        if version is None or tuple(map(int, version.groups())) < (0, 153, 4):
            raise RuntimeError(
                "watch-codex requires a stable Codex app-server >= 0.153.4 "
                "with toolOutput support; upgrade the server at --endpoint"
            )
        self.socket.send(json.dumps({"method": "initialized", "params": {}}))
        cursor = None
        visited = set()
        while True:
            page = self._rpc("thread/loaded/list", {"cursor": cursor, "limit": 100})
            if self.thread in page.get("data", []):
                break
            cursor = page.get("nextCursor")
            if cursor is None:
                raise watch.TargetLost(
                    "Codex thread is not loaded at this endpoint; re-arm in its owning host"
                )
            if cursor in visited:
                raise RuntimeError("app-server repeated a pagination cursor")
            visited.add(cursor)
        self.report(
            "wake_connected: exact Codex thread found; live wake remains unverified"
        )

    def _status(self) -> str:
        self._open()
        thread = self._rpc(
            "thread/read", {"threadId": self.thread, "includeTurns": False}
        ).get("thread", {})
        if thread.get("id") != self.thread:
            raise RuntimeError("app-server returned a different thread")
        status = thread.get("status", {}).get("type")
        if status == "notLoaded":
            raise watch.TargetLost("Codex thread was unloaded; re-arm")
        if status == "systemError":
            raise RuntimeError("Codex thread is in systemError")
        if status not in ("idle", "active"):
            raise RuntimeError(f"unrecognized Codex thread status: {status!r}")
        return status

    @contextlib.contextmanager
    def _transport(self):
        """A failed exchange must never leave a partially bound connection usable."""
        # Imported only when this optional adapter is actually used.
        try:
            from websockets.exceptions import ConnectionClosed, InvalidHandshake
        except ImportError:
            raise RuntimeError(
                "install hardline-mcp[codex-watch] to use watch-codex"
            ) from None
        try:
            yield
        except (OSError, TimeoutError, ConnectionClosed) as exc:
            self.close()
            raise watch.Unavailable(f"Codex transport: {exc}") from exc
        except InvalidHandshake as exc:
            self.close()
            raise RuntimeError(
                f"Codex endpoint rejected the connection: {exc}"
            ) from exc
        except BaseException:
            self.close()
            raise

    def poll(self, notice: dict | None = None, pending: bool = True) -> bool:
        """Validate once, then offer a due notice if the thread can receive it.

        None checks attachment without waking; it does not imply an empty inbox.
        An explicit empty snapshot resolves any earlier submission uncertainty,
        even if the host is busy or unavailable. Quiet preflights preserve it.
        """
        if not pending:
            self.uncertain_until = 0.0
        with self._transport():
            status = self._status()
            if (
                notice is None
                or not pending
                or status == "active"
                or self.clock() < self.uncertain_until
            ):
                return False
            # Mail may have been consumed while the host status was fetched.
            if not watch.read_pending(self.target):
                return False
            # A lost acknowledgement may still have started a turn. Reserve a
            # cooldown BEFORE sending so reconnect cannot immediately duplicate it.
            self.uncertain_until = self.clock() + self.remind_after
            result = self._rpc(
                "turn/start",
                {
                    "threadId": self.thread,
                    "input": [],
                    "toolOutput": {
                        "name": "hardline_watch",
                        "namespace": None,
                        "output": json.dumps(notice, separators=(",", ":")),
                    },
                },
            )
            turn = result.get("turn", {})
            if not turn.get("id") or turn.get("status") not in (
                "inProgress",
                "completed",
            ):
                raise RuntimeError("Codex did not confirm a turn for the notice")
            self.uncertain_until = 0.0
            self.report(
                f"wake accepted: sequence={notice['sequence']} turn={turn['id']}"
            )
            return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    watch.add_arguments(parser, agent="codex")
    parser.add_argument("--endpoint", type=local_endpoint, required=True)
    parser.add_argument("--thread", type=thread_uuid, required=True)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the binding without starting a turn",
    )
    args = parser.parse_args(argv)
    target = watch.target_from_args(parser, args)
    wake = CodexWake(args.endpoint, args.thread, target, remind_after=args.remind_after)
    try:
        with watch.stopping() as wait:
            return watch.run(
                target,
                interval=args.interval,
                remind_after=args.remind_after,
                once=args.check,
                poll=(lambda _, pending: wake.poll(pending=pending))
                if args.check
                else wake.poll,
                wait=wait,
            )
    finally:
        wake.close()


if __name__ == "__main__":
    raise SystemExit(main())
