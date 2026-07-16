# hardline-mcp

An [MCP](https://modelcontextprotocol.io) server that gives local AI agents —
[Claude Code](https://claude.com/claude-code), [Hermes](https://github.com/NousResearch/hermes-agent)
(MrAnderson), and [Codex](https://developers.openai.com/codex) — a shared way to
message each other. A durable SQLite **mailbox** is the backbone; thin **live
"ask" tools** let one agent get an answer from another right now.

Companion to [vram-mcp](https://github.com/sushiHex/vram-mcp): same single-purpose,
per-machine, install-everywhere shape.

## Model

- **Mailbox (durable):** `send` records every message to SQLite (WAL mode →
  safe concurrent writes from all three agents' subprocesses). Recipients
  `inbox` / `ack` on their own rhythm. `history` is the audit/visibility feed.
  Survives restarts and mismatched agent lifecycles (ephemeral Claude/Codex
  sessions vs. always-on Hermes gateway).
- **Push (no daemon):** `send(..., deliver=true)` additionally fires the
  recipient's *native* mechanism at send time (`hermes chat -q` /
  `codex exec` / `claude -p`) — real push with zero extra always-on processes.
- **Live ask:** `ask_hermes` / `ask_codex` / `ask_claude` spawn a one-shot
  session and return the reply synchronously. Heavier than the mailbox; use
  when you need an answer immediately.

## Tools

| Tool | Behavior |
| --- | --- |
| `send(from_agent, to_agent, message, deliver=false)` | Persist; if `deliver`, also push natively. |
| `inbox(agent, unread_only=true)` | Messages addressed to `agent`, oldest first. |
| `ack(message_id)` | Mark read (idempotent). |
| `history(limit=50, agent=None)` | Recent messages newest-first; `agent` matches sender or recipient. |
| `ask_hermes(prompt)` | Live query → `hermes chat -q`. |
| `ask_codex(prompt)` | Live query → `codex exec`. |
| `ask_claude(prompt)` | Live query → `claude -p`. |

Agent names are the fixed set `claude`, `hermes`, `codex`. Identity is
self-declared (`from_agent`) — convention, not enforced auth; every process
runs as the same user on one machine.

## Install

```bash
pip install -e .
```

`hardline-mcp` is the stdio server entry point (console script).

## Configuration

State lives at `~/.cache/hardline-mcp/mailbox.db`.

Each agent's CLI must be reachable. If a binary isn't on `PATH`, set its full
command via env var (space-split, quotes honored):

- `HARDLINE_HERMES_CMD` — e.g. `"C:/Users/you/AppData/Local/hermes/hermes-agent/venv/Scripts/hermes.exe"`
- `HARDLINE_CODEX_CMD`
- `HARDLINE_CLAUDE_CMD`

## MCP client registration

**Claude Code / Hermes** (`config.yaml` `mcp_servers` or Claude's MCP config):
point the command at the installed `hardline-mcp` console script.

**Codex** (`~/.codex/config.toml`): register under `mcp_servers` (Codex uses
its own TOML config, separate from the other two).

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q
```
