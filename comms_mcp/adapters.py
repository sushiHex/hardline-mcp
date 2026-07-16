"""Native push/query adapters — reach each agent the way it was built to be
reached.

- hermes -> ``hermes chat -q <prompt>``   (one-shot query to the running agent)
- codex  -> ``codex exec <prompt>``        (non-interactive execution)
- claude -> ``claude -p <prompt>``         (headless print mode)

``ask()`` runs the command and returns the reply synchronously; ``deliver()``
pushes a one-shot notice through the same dispatch. Both are pure subprocess
wrappers (no ``mcp`` import) so the server layer can run them off-thread.

Each agent's command is overridable via env var so a binary that isn't on
PATH can still be reached — notably ``hermes`` on this project's machine,
which lives inside its bundled venv:

    COMMS_HERMES_CMD, COMMS_CODEX_CMD, COMMS_CLAUDE_CMD

Each may contain a full path and leading fixed args, space-split (e.g.
``COMMS_HERMES_CMD="C:/.../venv/Scripts/hermes.exe"``).
"""

from __future__ import annotations

import os
import shlex
import subprocess

# prompt is appended as the final argv element(s); this is the invocation
# *prefix* per agent. Env override replaces the prefix entirely.
_DISPATCH = {
    "hermes": (["hermes", "chat", "-q"], "COMMS_HERMES_CMD"),
    "codex": (["codex", "exec"], "COMMS_CODEX_CMD"),
    "claude": (["claude", "-p"], "COMMS_CLAUDE_CMD"),
}

# ask()/deliver() spawn a whole agent session — generous ceiling, but bounded
# so a hung target can never wedge the caller forever.
_TIMEOUT_S = 180


def _prefix_for(agent: str) -> list[str]:
    _default, env_var = _DISPATCH[agent]
    override = os.environ.get(env_var)
    if override:
        # split on whitespace but honor quoting, so a Windows path with
        # spaces can be quoted in the env var
        return shlex.split(override, posix=False)
    return list(_default)


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


def ask(agent: str, prompt: str) -> dict:
    """Live synchronous query to ``agent``. Returns ``{"ok", "reply"}`` on
    success or ``{"ok": False, "error"}``."""
    if agent not in _DISPATCH:
        return {"ok": False, "error": f"unknown agent {agent!r}; known: {sorted(_DISPATCH)}"}
    return _run_cmd(_prefix_for(agent) + [prompt])


def deliver(agent: str, notice: str) -> dict:
    """Push a one-shot notice to ``agent`` via its native mechanism (same
    dispatch as ``ask``). Used by the server's ``deliver`` flag on send."""
    if agent not in _DISPATCH:
        return {"ok": False, "error": f"unknown agent {agent!r}; known: {sorted(_DISPATCH)}"}
    return _run_cmd(_prefix_for(agent) + [notice])
