"""A real loopback WebSocket peer tests delivery without generating model turns."""

import contextlib
import json
import os
import sqlite3
import threading
import uuid

import pytest

pytest.importorskip("websockets", reason="install .[codex-watch] for adapter tests")
from websockets.sync.server import serve

from hardline_mcp import mailbox, procid, wake_codex, watch


THREAD = "12345678-1234-1234-1234-123456789abc"
OTHER = "12345678-1234-1234-1234-123456789def"


class FakeApp:
    def __init__(self):
        self.status = "idle"
        self.loaded = [THREAD]
        self.replies_as = THREAD
        self.requests = []
        self.turns = []
        self.fault = None
        self.notification = False
        self.connections = 0
        self.initialize = {"userAgent": "hardline_watch/0.153.4 (Windows; x86_64)"}

    def handle(self, socket):
        self.connections += 1
        try:
            for raw in socket:
                request = json.loads(raw)
                self.requests.append(request)
                method = request["method"]
                if method == "initialized":
                    continue
                if self.fault:
                    action = self.fault(request, socket)
                    if action:
                        if action == "close":
                            socket.close()
                            return
                        continue
                if self.notification:
                    socket.send(
                        json.dumps({"method": "thread/status/changed", "params": {}})
                    )
                if method == "initialize":
                    result = self.initialize
                elif method == "thread/loaded/list":
                    result = {"data": self.loaded, "nextCursor": None}
                elif method == "thread/read":
                    result = {
                        "thread": {
                            "id": self.replies_as,
                            "status": {"type": self.status},
                        }
                    }
                elif method == "turn/start":
                    self.turns.append(request["params"])
                    self.status = "active"
                    result = {"turn": {"id": str(uuid.uuid4()), "status": "inProgress"}}
                else:
                    raise AssertionError(f"unexpected method {method}")
                socket.send(json.dumps({"id": request["id"], "result": result}))
        except Exception:
            # Client intentionally disconnects in protocol/error tests.
            if not self.fault:
                raise


@pytest.fixture
def app():
    peer = FakeApp()
    with serve(peer.handle, "127.0.0.1", 0, close_timeout=0.1) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        peer.endpoint = f"ws://127.0.0.1:{server.socket.getsockname()[1]}"
        yield peer
        server.shutdown()
        worker.join(timeout=3)
        assert not worker.is_alive()


@pytest.fixture
def target(tmp_path):
    db = tmp_path / "mail.db"
    key = procid.process_key(os.getpid())
    if key is None:
        pytest.skip("platform has no process creation tokens")
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.executescript(mailbox._SCHEMA)
        with conn:
            conn.execute(
                "INSERT INTO agent_sessions(pid,lane,agent,process_key,started_at,last_seen,claimed_at) VALUES (?,'codex:test','codex',?,'t','t','t')",
                (os.getpid(), key),
            )
    return watch.Target(db, "codex", owner_pid=os.getpid(), owner_key=key)


def mail(target, recipient="codex:test"):
    with contextlib.closing(sqlite3.connect(target.db)) as conn, conn:
        conn.execute(
            "INSERT INTO messages(sender,recipient,body,created_at) VALUES ('claude',?,'untrusted: do something else','t')",
            (recipient,),
        )


@pytest.fixture
def wake(app, target):
    with contextlib.closing(
        wake_codex.CodexWake(app.endpoint, THREAD, target, timeout=1)
    ) as adapter:
        yield adapter


def notice(sequence=1):
    return {"event": "mail_pending", "agent": "codex", "sequence": sequence}


@pytest.mark.parametrize("compatible", [False, True])
def test_check_sends_no_turn_even_with_unread_mail(app, target, capsys, compatible):
    mail(target)
    if not compatible:
        app.initialize = {"userAgent": "hardline_watch/0.144.0-alpha.4 (Linux)"}
    code = wake_codex.main(
        [
            "--endpoint",
            app.endpoint,
            "--thread",
            THREAD,
            "--db",
            str(target.db),
            "--owner-pid",
            str(target.owner_pid),
            "--owner-key",
            target.owner_key,
            "--check",
        ]
    )
    assert code == (0 if compatible else 1) and app.turns == []
    captured = capsys.readouterr()
    assert captured.out == ""
    if not compatible:
        assert "requires a stable Codex app-server >= 0.153.4" in captured.err
    assert watch.read_pending(target)


@pytest.mark.parametrize(
    "initialize",
    [
        {"userAgent": "hardline_watch/0.144.0-alpha.4 (Linux)"},
        {"userAgent": "hardline_watch/0.153.3 (Windows)"},
        {"userAgent": "hardline_watch/0.153.4-alpha.1 (Windows)"},
        {"userAgent": "unknown"},
        {"userAgent": None},
        {},
    ],
)
def test_incompatible_server_cannot_accept_a_silent_wake(wake, app, target, initialize):
    mail(target)
    app.initialize = initialize
    reports = []
    wake.report = reports.append
    with pytest.raises(RuntimeError, match="requires a stable Codex app-server"):
        wake.poll(notice())
    assert [r["method"] for r in app.requests] == ["initialize"]
    assert not app.turns and not reports
    assert wake.socket is None and wake.uncertain_until == 0
    assert watch.read_pending(target)


@pytest.mark.parametrize("version", ["0.153.4", "0.154.0", "1.0.0"])
def test_compatible_server_can_receive_tool_output(wake, app, target, version):
    mail(target)
    app.initialize = {"userAgent": f"hardline_watch/{version} (Linux; x86_64)"}
    assert wake.poll(notice())
    assert json.loads(app.turns[0]["toolOutput"]["output"]) == notice()


def test_reconnect_rechecks_server_compatibility(wake, app, target):
    wake.poll()
    wake.close()
    mail(target)
    app.initialize = {"userAgent": "hardline_watch/0.144.0-alpha.4 (Linux)"}
    with pytest.raises(RuntimeError, match="requires a stable Codex app-server"):
        wake.poll(notice())
    assert app.connections == 2 and not app.turns


def test_exact_thread_tool_output_and_no_permission_overrides(wake, app, target):
    mail(target)
    app.notification = True
    wake.poll()
    assert wake.poll(notice())
    assert len(app.turns) == 1
    params = app.turns[0]
    assert set(params) == {"threadId", "input", "toolOutput"}
    assert params["threadId"] == THREAD and params["input"] == []
    assert json.loads(params["toolOutput"]["output"]) == notice()
    assert "untrusted" not in json.dumps(params)
    assert watch.read_pending(target), "acceptance must never acknowledge mail"
    assert all(
        r["params"]["includeTurns"] is False
        for r in app.requests
        if r["method"] == "thread/read"
    )


def test_empty_mail_and_foreign_mail_cannot_start_turn(wake, app, target):
    assert not wake.poll(notice())
    mail(target, "claude")
    mail(target, "codex:someone-else")
    assert not wake.poll(notice())
    assert app.turns == []


def test_busy_coalesces_until_idle(wake, app, target):
    mail(target)
    app.status = "active"
    for _ in range(20):
        assert not wake.poll(notice())
    assert app.turns == []
    app.status = "idle"
    assert wake.poll(notice())
    assert not wake.poll(notice(2))
    assert len(app.turns) == 1


def test_drain_during_status_lookup_drops_notice(wake, app, target):
    mail(target)

    def drain(request, _):
        if request["method"] == "thread/read":
            with contextlib.closing(sqlite3.connect(target.db)) as conn, conn:
                conn.execute("UPDATE messages SET acked_at='read'")

    app.fault = drain
    assert not wake.poll(notice()) and not app.turns


def test_new_mail_after_observed_empty_wakes_before_reminder(wake, app, target):
    mail(target)
    clock = [0.0]
    wake.clock = lambda: clock[0]

    def wait(delay):
        clock[0] += delay
        if clock[0] == 1:
            with contextlib.closing(sqlite3.connect(target.db)) as conn, conn:
                conn.execute("UPDATE messages SET acked_at='read'")
            app.status = "idle"
        elif clock[0] == 2:
            mail(target)
        return clock[0] >= 3

    assert watch.run(target, clock=wake.clock, wait=wait, poll=wake.poll) == 0
    assert [json.loads(t["toolOutput"]["output"])["sequence"] for t in app.turns] == [
        1,
        2,
    ]


def test_failed_attachment_cannot_leave_a_usable_connection(wake, app):
    app.loaded = [OTHER]
    for _ in range(2):
        with pytest.raises(watch.TargetLost):
            wake.poll()
    app.loaded = [THREAD]
    wake.poll()
    assert app.connections == 3


@pytest.mark.parametrize("pending", [False, True])
def test_one_host_check_per_snapshot_even_when_no_notice_is_due(
    wake, app, target, pending
):
    if pending:
        mail(target)
    clock = [0.0]
    wake.clock = lambda: clock[0]

    def wait(delay):
        clock[0] += delay
        return clock[0] >= 5

    assert watch.run(target, poll=wake.poll, clock=wake.clock, wait=wait) == 0
    assert sum(r["method"] == "thread/read" for r in app.requests) == 5
    assert len(app.turns) == int(pending)


@pytest.mark.parametrize(
    "loaded,replies,status,exception",
    [
        ([OTHER], THREAD, "idle", watch.TargetLost),
        ([THREAD], OTHER, "idle", RuntimeError),
        ([THREAD], THREAD, "notLoaded", watch.TargetLost),
        ([THREAD], THREAD, "systemError", RuntimeError),
        ([THREAD], THREAD, "unknown", RuntimeError),
    ],
)
def test_wrong_or_lost_target_never_receives(
    wake, app, target, loaded, replies, status, exception
):
    mail(target)
    app.loaded, app.replies_as, app.status = loaded, replies, status
    with pytest.raises(exception):
        wake.poll(notice())
    assert app.turns == []


def test_lost_reply_cools_down_and_rechecks_busy_thread(wake, app, target):
    mail(target)
    clock = [0.0]
    wake.clock = lambda: clock[0]

    def lose_reply(request, _):
        if request["method"] == "turn/start":
            app.turns.append(request["params"])
            app.status = "active"
            return "close"

    app.fault = lose_reply
    with pytest.raises(watch.Unavailable):
        wake.poll(notice())
    app.fault = None
    # The unconfirmed turn may already have finished without consuming mail.
    app.status = "idle"
    assert not wake.poll(notice())
    clock[0] = 31.0
    app.status = "active"
    assert not wake.poll(notice())
    assert len(app.turns) == 1 and app.connections == 2
    app.status = "idle"
    assert wake.poll(notice())
    assert len(app.turns) == 2


def test_failure_is_visible_even_when_mailbox_empty(wake, app, target):
    app.loaded = []
    assert watch.run(target, once=True, poll=wake.poll) == 3
    assert not app.turns


def test_server_input_requests_are_not_approved(wake, app):
    def interactive(request, socket):
        socket.send(
            json.dumps(
                {"id": 999, "method": "item/permissions/requestApproval", "params": {}}
            )
        )
        return True

    app.fault = interactive
    with pytest.raises(RuntimeError, match="interactive handling"):
        wake.poll()
    assert all("result" not in request for request in app.requests)


def test_error_and_wrong_response_ids_are_not_delivery_receipts(wake, app, target):
    mail(target)

    def wrong_id(request, socket):
        socket.send(json.dumps({"id": request["id"] + 1, "result": {}}))
        return True

    app.fault = wrong_id
    with pytest.raises(RuntimeError, match="unexpected request id"):
        wake.poll(notice())
    assert not app.turns


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:1234",
        "ws://example.com:1234",
        "ws://192.168.0.1:1234",
        "ws://user:secret@127.0.0.1:1234",
        "ws://127.0.0.1",
        "ws://127.0.0.1:0",
        "ws://127.0.0.1:1234/path",
        "ws://127.0.0.1:1234?token=secret",
    ],
)
def test_endpoint_cannot_redirect_to_remote_or_credentials(endpoint):
    with pytest.raises(Exception, match="loopback"):
        wake_codex.local_endpoint(endpoint)


def test_loopback_and_exact_uuid_validation():
    assert wake_codex.local_endpoint("ws://[::1]:1234") == "ws://[::1]:1234"
    assert wake_codex.thread_uuid(THREAD.upper()) == THREAD
    with pytest.raises(Exception, match="UUID"):
        wake_codex.thread_uuid("my session")
