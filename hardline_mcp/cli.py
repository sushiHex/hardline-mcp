"""Select the command before importing the MCP server or optional transports."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        from .server import main as serve

        serve()
    elif args[0] == "watch":
        from .watch import main as watch

        raise SystemExit(watch(args[1:]))
    elif args[0] == "watch-codex":
        from .wake_codex import main as wake

        raise SystemExit(wake(args[1:]))
    elif args in (["-h"], ["--help"]):
        print(
            "Usage: hardline-mcp [watch | watch-codex] [OPTIONS]\n\n"
            "Omit COMMAND to serve MCP over stdio. Use COMMAND --help for options."
        )
    else:
        print("hardline-mcp: unknown command; use --help", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
