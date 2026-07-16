"""Native push/query adapters — reach each agent the way it was built to be
reached.

- hermes -> ``hermes chat -Q -q <prompt>``  (quiet one-shot query; -Q strips
                                             the banner/box-chrome, -q = query)
- codex  -> ``codex exec <prompt>``          (non-interactive execution)
- claude -> ``claude -p <prompt>``           (headless print mode)

``ask()`` runs the command and returns the reply synchronously; ``deliver()``
pushes a one-shot notice through the same dispatch. Both are pure subprocess
wrappers (no ``mcp`` import) so the server layer can run them off-thread.

Executable resolution, in precedence order, per agent:

1. ``HARDLINE_{HERMES,CODEX,CLAUDE}_CMD`` env var — an explicit path override,
   for a binary that isn't on PATH (e.g. ``hermes`` in its bundled venv).
2. A per-agent discovery hook (only ``codex`` has one — its install dir is
   hash-named and rotates on every Codex update, so a pinned path rots;
   discovery finds the newest ``codex.exe`` so the tool self-heals).
3. The bare command name, resolved on PATH (the normal case for ``claude``).

Only the executable is resolved this way; the fixed subcommand
(``chat -Q -q`` / ``exec`` / ``-p``) is always appended.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional


def _codex_bin_root() -> Path:
    """Directory holding Codex's hash-named install subdirs
    (``%LOCALAPPDATA%\\OpenAI\\Codex\\bin`` on Windows). Split out so tests can
    point discovery at a temp tree."""
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "OpenAI" / "Codex" / "bin"


def _discover_codex() -> Optional[str]:
    """Newest ``codex.exe`` under the hash-named install dirs, or None.

    Codex installs to ``.../Codex/bin/<hash>/codex.exe`` and the ``<hash>``
    changes on every update, so pinning one path breaks on the next update.
    Picking the most-recently-modified binary tracks the current install."""
    try:
        candidates = list(_codex_bin_root().glob("*/codex.exe"))
    except OSError:
        return None
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    return str(newest)


# (default executable, fixed subcommand args, env var overriding the executable).
# The prompt is appended after the subcommand args.
#   hermes: -Q (quiet) suppresses banner/spinner/tool-previews/box-chrome so
#           the reply is just the final message; -q takes the query. Order
#           matters — -Q before -q, since -q consumes the next arg as the query.
#           (Without -Q the reply is ~940 chars of ANSI box art per call.)
#   codex:  exec output carries a small preamble/token-count footer around the
#           answer; usable as-is. --output-last-message <FILE> is the fully-
#           clean path if this ever proves too noisy. Resolved via discovery
#           (see _prefix_for) because its install dir is hash-named.
#   claude: -p headless print mode is already clean; normally on PATH.
_DISPATCH = {
    "hermes": ("hermes", ["chat", "-Q", "-q"], "HARDLINE_HERMES_CMD"),
    "codex": ("codex", ["exec"], "HARDLINE_CODEX_CMD"),
    "claude": ("claude", ["-p"], "HARDLINE_CLAUDE_CMD"),
}

# ask()/deliver() spawn a whole agent session — generous ceiling, but bounded
# so a hung target can never wedge the caller forever.
_TIMEOUT_S = 180


def _prefix_for(agent: str) -> list[str]:
    default_exe, subcmd, env_var = _DISPATCH[agent]
    # Precedence: explicit env override > per-agent discovery > bare name (PATH).
    # Only codex needs discovery — its install dir is hash-named and rotates on
    # every update, so a pinned path rots.
    exe = os.environ.get(env_var)
    if not exe and agent == "codex":
        exe = _discover_codex()
    return [exe or default_exe, *subcmd]


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
