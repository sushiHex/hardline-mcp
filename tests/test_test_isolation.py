import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys


def test_collection_and_late_workers_use_temporary_store(tmp_path):
    """A nested pytest run must ignore even an explicitly configured live store."""
    live = tmp_path / "operator.db"
    with sqlite3.connect(live) as conn:
        conn.execute("CREATE TABLE sentinel (value)")
        conn.execute("INSERT INTO sentinel VALUES ('untouched')")
    suite = tmp_path / "suite"
    suite.mkdir()
    shutil.copy2(Path(__file__).with_name("conftest.py"), suite / "conftest.py")
    (suite / "test_store.py").write_text(
        """
import os
import time
from pathlib import Path
from hardline_mcp import mailbox, server

assert mailbox._resolve_db(None) != Path(os.environ["OPERATOR_DB"])
assert mailbox._DEFAULT_PATH != Path(os.environ["OPERATOR_DB"])

def test_late_delivery(monkeypatch, tmp_path):
    monkeypatch.setenv("HARDLINE_DB", str(tmp_path / "short-lived.db"))
    def deliver():
        time.sleep(0.1)
        mailbox.send("codex", "claude", "late")
    server._async_executor.submit(deliver)
""",
        encoding="utf-8",
    )
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(suite)],
        cwd=tmp_path,
        env={
            **os.environ,
            "HARDLINE_DB": str(live),
            "OPERATOR_DB": str(live),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    with sqlite3.connect(live) as conn:
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == [
            ("sentinel",)
        ]
