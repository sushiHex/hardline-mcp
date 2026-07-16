"""Native push/query adapters — reach each agent the way it was built to be
reached.

- hermes -> ``hermes chat -q <prompt>``   (one-shot query to the running agent)
- codex  -> ``codex exec <prompt>``        (non-interactive execution)
- claude -> ``claude -p <prompt>``         (headless print mode)

``ask()`` runs the command and returns the reply synchronously; ``deliver()``
pushes a one-shot notice through the same dispatch. Both are pure subprocess
wrappers (no ``mcp`` import) so the server layer can run them off-thread.

Only the BINARY location is overridable via env var (the subcommand args are
intrinsic to each tool and never change). This lets a binary that isn't on
PATH still be reached — notably ``hermes`` (bundled venv) and ``codex``
(hashed install dir) on this project's machine:

    COMMS_HERMES_CMD, COMMS_CODEX_CMD, COMMS_CLAUDE_CMD

Each is a path to the executable only, e.g.
``COMMS_HERMES_CMD="C:/.../venv/Scripts/hermes.exe"``. The fixed subcommand
(``chat -q`` / ``exec`` / ``-p``) is still appended automatically.
"""

from __future__ import annotations

import os
import subprocess

# (default executable, fixed subcommand args, env var overriding the executable).
# The prompt is appended after the subcommand args.
#   hermes: -Q (quiet) suppresses banner/spinner/tool-previews/box-chrome so
#           the reply is just the final message; -q takes the query. Order
#           matters — -Q before -q, since -q consumes the next arg as the query.
#           (Without -Q the reply is ~940 chars of ANSI box art per call.)
#   codex:  exec output carries a small preamble/token-count footer around the
#           answer; usable as-is for v1. --output-last-message <FILE> is the
#           fully-clean path if this proves noisy in practice.
#   claude: -p headless print mode is already clean.
_DISPATCH = {
    "hermes": ("hermes", ["chat", "-Q", "-q"], "COMMS_HERMES_CMD"),
    "codex": ("codex", ["exec"], "COMMS_CODEX_CMD"),
    "claude": ("claude", ["-p"], "COMMS_CLAUDE_CMD"),
}

# ask()/deliver() spawn a whole agent session — generous ceiling, but bounded
# so a hung target can never wedge the caller forever.
_TIMEOUT_S = 180


def _prefix_for(agent: str) -> list[str]:
    default_exe, subcmd, env_var = _DISPATCH[agent]
    exe = os.environ.get(env_var) or default_exe
    return [exe, *subcmd]


def _run_cmd(argv: list[str]) -> dict:
    """Run argv, capturing text output. Never raises — every failure mode is
    mapped to ``{"ok": False, "error": ...}`` so one dead target can't crash
    the MCP tool call."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {_TIMEOUT_S}s"}
    except FileNotFoundError:
        return {"ok": False, "error": f"command not found / not installed: {argv[0]!r}"}
    except OSError as e:
        return {"ok": False, "error": f"spawn failed: {e}"}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return {"ok": False, "error": f"exit {proc.returncode}: {detail}"}
    return {"ok": True, "reply": (proc.stdout or "").strip()}


def ask(agent: str, text: str) -> dict:
    """Run ``text`` through ``agent``'s native CLI and return its output.

    Returns ``{"ok", "reply"}`` on success or ``{"ok": False, "error"}``.
    """
    if agent not in _DISPATCH:
        return {"ok": False, "error": f"unknown agent {agent!r}; known: {sorted(_DISPATCH)}"}
    return _run_cmd(_prefix_for(agent) + [text])


# Pushing a one-shot notice IS running text through the agent — same operation.
# ``deliver`` is kept as a named alias so intent reads clearly at call sites
# (the server's ``deliver`` flag on send) without duplicating the body/guard.
deliver = ask
