"""The live dashboard: what it shows, what it must never show, and who may ask."""

import json
import sqlite3
import threading
import urllib.error
import urllib.request

import pytest

from hardline_mcp import dash, jobs, mailbox, procid, sessions

SECRET = "sk-live-SECRET-body"


def _store(tmp_path):
    db = tmp_path / "mb.db"
    sessions.register(agent="codex", lanes=["codex:review"], db_path=db)
    mailbox.send("claude", "codex:review", SECRET, db_path=db)
    mailbox.send("claude", "codex:nobody", SECRET, db_path=db)
    job = jobs.create(
        agent="codex", requester="claude:x", label="retry-review", request={"prompt": SECRET}, db_path=db
    )
    jobs.mark_running(job, db_path=db)
    return db


def test_lanes_unread_traffic_and_jobs(tmp_path):
    db = _store(tmp_path)
    got = dash.state(db)
    by_lane = {lane["lane"]: lane for lane in got["lanes"]}
    assert by_lane["codex:review"]["liveness"] == procid.ALIVE
    assert by_lane["codex:review"]["unread"] == 1
    # Mail for a name nobody holds is shown, and says so.
    assert by_lane["codex:nobody"]["liveness"] == "unheld"
    assert by_lane["codex:nobody"]["unread"] == 1
    assert sum(got["traffic"]) == 2 and got["traffic"][-1] == 2
    assert [(j["label"], j["state"]) for j in got["jobs"]] == [("retry-review", "running")]
    assert [m["read"] for m in got["recent"]] == [False, False]
    assert got["unavailable"] == []


def test_no_body_request_or_path_leaves_the_store(tmp_path):
    db = _store(tmp_path)
    raw = sqlite3.connect(db)
    with raw:
        raw.execute("UPDATE agent_sessions SET cwd = ?", ("C:/Users/secret-user/project",))
        raw.execute("UPDATE jobs SET error = ?, result = ?", (SECRET, SECRET))
    raw.close()
    text = json.dumps(dash.state(db))
    assert SECRET not in text and "secret-user" not in text
    assert str(tmp_path) not in text  # the store is named, never located


def test_a_job_whose_owner_died_reads_lost(tmp_path):
    db = _store(tmp_path)
    got = dash.state(db, probe=lambda pid, key: procid.DEAD)
    assert got["jobs"][0]["state"] == "lost"
    # A dead holder is no holder: its lane shows only because mail still waits there.
    assert {lane["lane"]: lane["liveness"] for lane in got["lanes"]} == {
        "codex:nobody": "unheld", "codex:review": "unheld"}


def test_an_old_store_is_unavailable_not_zero(tmp_path):
    db = tmp_path / "old.db"
    raw = sqlite3.connect(db)
    with raw:
        raw.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, sender TEXT, recipient TEXT,"
                    " body TEXT, created_at TEXT, acked_at TEXT)")
    raw.close()
    got = dash.state(db)
    assert got["unavailable"] == ["jobs", "sessions"]
    assert got["lanes"] == [] and got["jobs"] == []


def test_the_dashboard_never_writes(tmp_path):
    db = _store(tmp_path)
    raw = sqlite3.connect(db)
    before = raw.execute("PRAGMA data_version").fetchone(), raw.execute(
        "SELECT COUNT(*) FROM messages").fetchone()
    raw.close()
    stat = db.stat().st_mtime_ns
    dash.state(db)
    assert db.stat().st_mtime_ns == stat
    raw = sqlite3.connect(db)
    assert raw.execute("SELECT COUNT(*) FROM messages").fetchone() == before[1]
    raw.close()
    with pytest.raises(FileNotFoundError):
        dash.state(tmp_path / "none.db")


def test_links_count_who_reached_whom_this_hour(tmp_path):
    from datetime import datetime, timedelta, timezone

    db = tmp_path / "mb.db"
    ago = lambda minutes: (lambda: datetime.now(timezone.utc) - timedelta(minutes=minutes))
    mailbox.send("claude", "codex:a", SECRET, db_path=db, now_fn=ago(20))
    mailbox.send("claude", "codex:a", SECRET, db_path=db, now_fn=ago(5))
    mailbox.send("codex:a", "claude", SECRET, db_path=db)
    mailbox.send("claude", "codex:old", SECRET, db_path=db, now_fn=ago(180))
    raw = sqlite3.connect(db)
    with raw:
        raw.execute("UPDATE messages SET acked_at = created_at WHERE id = 1")
    raw.close()
    got = dash.state(db)["links"]
    by = {(link["from"], link["to"]): link for link in got}
    assert {pair: (link["n"], link["unread"]) for pair, link in by.items()} == {
        ("claude", "codex:a"): (2, 1),
        ("codex:a", "claude"): (1, 1),
    }
    assert 250 <= by["claude", "codex:a"]["last_s"] <= 400  # the newer of its two calls
    assert by["codex:a", "claude"]["last_s"] < 60
    assert SECRET not in json.dumps(got)


def test_a_line_with_no_calls_this_hour_is_inactive(tmp_path):
    from datetime import datetime, timedelta, timezone

    db = tmp_path / "mb.db"
    sessions.register(agent="codex", lanes=["codex:idle"], db_path=db)
    sessions.register(agent="codex", lanes=["codex:talker"], db_path=db)
    sessions.register(agent="codex", lanes=["codex:was-busy"], db_path=db)
    mailbox.send("codex:talker", "claude", "hi", db_path=db)
    mailbox.send("codex:was-busy", "claude", "hi", db_path=db, now_fn=lambda: datetime.now(timezone.utc) - timedelta(hours=3))
    raw = sqlite3.connect(db)
    with raw:
        raw.execute("UPDATE messages SET acked_at = created_at")
    raw.close()
    lanes = {lane["lane"]: lane for lane in dash.state(db)["lanes"]}
    assert {name: lane["active"] for name, lane in lanes.items()} == {
        "codex:idle": False, "codex:talker": True, "codex:was-busy": False}
    # Quiet since its last call, or since it started if it never made one.
    assert 10700 <= lanes["codex:was-busy"]["idle_s"] <= 10900
    assert lanes["codex:idle"]["idle_s"] < 120


def test_a_session_hosted_by_a_spawned_cli_is_temporary(tmp_path):
    db = tmp_path / "mb.db"
    sessions.register(agent="claude", lanes=["claude:worker"], db_path=db)
    sessions.register(agent="claude", lanes=["claude:regular"], db_path=db)
    job = jobs.create(agent="claude", requester="claude:regular", label="w", request={}, db_path=db)
    jobs.mark_running(job, child_pid=4242, db_path=db)
    raw = sqlite3.connect(db)
    with raw:
        raw.execute("UPDATE agent_sessions SET host_pid = 4242 WHERE lane = 'claude:worker'")
    raw.close()
    got = dash.state(db, probe=lambda pid, key: procid.ALIVE)
    assert {lane["lane"]: lane["temp"] for lane in got["lanes"]} == {"claude:worker": True, "claude:regular": False}
    assert got["temporary"] == ["claude:worker"]


def test_playbook_jobs_are_recognised_by_their_label(tmp_path):
    db = tmp_path / "mb.db"
    for label in ("roundtable-2-astra", "swim-lanes-r1-fable", "ping-pong-3-astra-high", "retry-review", "roundtable-x-astra"):
        jobs.create(agent="codex", requester="claude:x", label=label, request={}, db_path=db)
    by_label = {j["label"]: j["play"] for j in dash.state(db)["jobs"]}
    assert by_label == {
        "roundtable-2-astra": {"name": "roundtable", "round": 2, "lane": "astra"},
        "swim-lanes-r1-fable": {"name": "swim-lanes", "round": 1, "lane": "fable"},
        "ping-pong-3-astra-high": {"name": "ping-pong", "round": 3, "lane": "astra-high"},
        "retry-review": None,
        "roundtable-x-astra": None,
    }


def test_traffic_is_split_by_the_agent_that_placed_the_call(tmp_path):
    db = tmp_path / "mb.db"
    mailbox.send("claude", "codex:a", "x", db_path=db)
    mailbox.send("codex:x", "claude", "x", db_path=db)
    mailbox.send("codex:y", "claude", "x", db_path=db)
    mailbox.send("somebody", "claude", "x", db_path=db)
    got = dash.state(db)
    by = got["traffic_by"]
    assert {agent: col[-1] for agent, col in by.items()} == {"claude": 1, "codex": 2, "other": 1}
    assert [sum(col) for col in zip(*by.values())] == got["traffic"]
    assert got["traffic_unanswered"][-1] == 4
    raw = sqlite3.connect(db)
    with raw:
        raw.execute("UPDATE messages SET acked_at = created_at WHERE id IN (1, 2)")
    raw.close()
    assert dash.state(db)["traffic_unanswered"][-1] == 2  # only the calls nobody answered


def test_the_store_is_opened_read_only(tmp_path, monkeypatch):
    opened = []
    real = sqlite3.connect

    def spy(target, *args, **kwargs):
        opened.append(str(target))
        return real(target, *args, **kwargs)

    db = _store(tmp_path)
    monkeypatch.setattr(dash.sqlite3, "connect", spy)
    dash.state(db)
    assert opened and all("mode=ro" in uri for uri in opened)


def test_a_server_whose_host_died_is_not_shown_as_held(tmp_path):
    db = _store(tmp_path)
    raw = sqlite3.connect(db)
    with raw:
        raw.execute("UPDATE agent_sessions SET host_pid = 2147483646, host_key = 'k'")
    raw.close()
    got = dash.state(db, probe=lambda pid, key: procid.DEAD if pid == 2147483646 else procid.ALIVE)
    assert "codex:review" not in {lane["lane"] for lane in got["lanes"] if lane["liveness"] != "unheld"}


def test_a_long_name_is_clipped(tmp_path):
    db = _store(tmp_path)
    mailbox.send("x" * 500, "codex:review", "hi", db_path=db)
    assert max(len(m["from"]) for m in dash.state(db)["recent"]) == dash.CLIP


def test_a_runaway_read_is_abandoned(tmp_path, monkeypatch):
    db = _store(tmp_path)
    monkeypatch.setattr(dash, "BUDGET", -1.0)
    monkeypatch.setattr(dash, "CHECK_EVERY", 1)  # a store this small never reaches the usual interval
    with pytest.raises(sqlite3.OperationalError):
        dash.state(db)


def test_tabs_share_one_read(tmp_path, monkeypatch):
    calls = []
    real = dash.state
    monkeypatch.setattr(dash, "state", lambda db, **kw: calls.append(kw) or real(db, **kw))
    shared = dash._Shared(_store(tmp_path))
    assert shared.body() == shared.body() and len(calls) == 1
    shared.body(900)  # another span is its own read, cached apart
    assert shared.body(900) and [c["window_s"] for c in calls] == [3600, 900]


def test_the_span_follows_the_request(tmp_path):
    from datetime import datetime, timedelta, timezone

    db = _store(tmp_path)
    two_hours_ago = lambda: datetime.now(timezone.utc) - timedelta(hours=2)
    mailbox.send("claude:old", "codex:review", "x", db_path=db, now_fn=two_hours_ago)
    quarter, hour, six = (dash.state(db, window_s=s) for s in (900, 3600, 21600))
    assert [(len(s["traffic"]), s["bucket_s"]) for s in (quarter, hour, six)] == [(15, 60), (60, 60), (60, 360)]
    # A call two hours old is on the six-hour board and nowhere in the last hour.
    assert ("claude:old" in {l["from"] for l in six["links"]}, "claude:old" in {l["from"] for l in hour["links"]}) == (True, False)
    assert sum(six["traffic"]) == sum(hour["traffic"]) + 1


def test_a_request_names_one_known_span():
    assert [dash._window(q) for q in ("", "window=15m", "window=6h", "window=2h", "window=1h&window=6h")] == [
        3600, 900, 21600, None, None]


@pytest.fixture
def served(tmp_path):
    server = dash.serve(_store(tmp_path), 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()
    thread.join(5)


def _get(port, path, **headers):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            return reply.status, reply.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_it_serves_the_page_and_the_state(served):
    code, page = _get(served, "/")
    assert code == 200 and "<title>hardline</title>" in page
    code, body = _get(served, "/api/state")
    assert code == 200 and SECRET not in body
    assert {lane["lane"] for lane in json.loads(body)["lanes"]} == {"codex:review", "codex:nobody"}
    assert _get(served, "/nope")[0] == 404


@pytest.mark.parametrize("path", ["/", "/api/state"])
def test_a_rebound_name_is_refused(served, path):
    assert _get(served, path, Host="evil.example")[0] == 403
    assert _get(served, path, Host=f"evil.example:{served}")[0] == 403


def test_a_cross_site_origin_is_refused(served):
    assert _get(served, "/api/state", Origin="https://evil.example")[0] == 403
    assert _get(served, "/api/state", Origin=f"http://localhost:{served}")[0] == 200


def test_only_get_is_answered(served):
    request = urllib.request.Request(f"http://127.0.0.1:{served}/api/state", data=b"x", method="POST")
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(request, timeout=10)
    assert err.value.code == 501


def test_it_listens_on_loopback_only(served):
    assert dash.HOST == "127.0.0.1"
