"""Which module each subcommand reaches, and how an unknown one ends."""

import importlib

import pytest

from hardline_mcp import cli


@pytest.mark.parametrize(
    "command,module",
    [("watch", "watch"), ("watch-codex", "wake_codex"), ("report", "report")],
)
def test_each_subcommand_reaches_its_own_module(monkeypatch, command, module):
    reached = []
    for name in ("watch", "wake_codex", "report"):
        monkeypatch.setattr(
            importlib.import_module(f"hardline_mcp.{name}"),
            "main",
            lambda argv, name=name: reached.append((name, argv)) or 0,
        )
    with pytest.raises(SystemExit) as exited:
        cli.main([command, "--flag"])
    assert exited.value.code == 0
    assert reached == [(module, ["--flag"])]


def test_an_unknown_command_exits_2(capsys):
    with pytest.raises(SystemExit) as exited:
        cli.main(["no-such-command"])
    assert exited.value.code == 2
    assert "unknown command" in capsys.readouterr().err
