"""Command selection, including through a launcher an older install generated."""

import sys

import pytest

from hardline_mcp import cli, mailbox, server


@pytest.fixture
def served(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "serve", lambda: calls.append("served"))
    return calls


def test_a_launcher_generated_for_server_main_still_selects_the_command(
    tmp_path, monkeypatch, capsys, served
):
    """A stale launcher calls server.main with the whole command line."""
    db = tmp_path / "mb.db"
    mailbox.send("a", "b", "c", db_path=db)
    monkeypatch.setattr(sys, "argv", ["hardline-mcp", "report", "--db", str(db)])
    with pytest.raises(SystemExit) as exited:
        server.main()
    assert exited.value.code == 0 and not served
    assert '"db_path": ' in capsys.readouterr().out


@pytest.mark.parametrize("main", [cli.main, server.main], ids=["cli", "server"])
def test_no_command_serves(monkeypatch, served, main):
    monkeypatch.setattr(sys, "argv", ["hardline-mcp"])
    main()
    assert served == ["served"]
