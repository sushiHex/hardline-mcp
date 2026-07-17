# hardline-mcp

A single-purpose [MCP](https://modelcontextprotocol.io) server that lets local
AI coding agents — [Claude Code](https://claude.com/claude-code),
[Hermes](https://github.com/NousResearch/hermes-agent), and
[Codex](https://developers.openai.com/codex) — **message each other on one
machine**. A durable SQLite *mailbox* is the backbone; thin *live-ask* tools let
one agent get an answer from another right now.

> *hardline* — in **The Matrix**, the hardwired lines a crew uses to reach agents
> in the field; in telecom, a dedicated direct circuit. This is that line,
> between your agents.

Companion to [vram-mcp](https://github.com/sushiHex/vram-mcp): same
single-purpose, per-machine, install-everywhere shape.

## Why

Local agents have mismatched lifecycles — a Claude Code or Codex session is
ephemeral, a Hermes gateway is always-on — so a naive "just call each other"
bridge drops messages the moment the other side isn't running. hardline-mcp
splits the problem:

- **Mailbox (durable, async).** `send` records every message to SQLite (WAL
  mode → safe concurrent writes from every agent's own subprocess). Recipients
  `inbox` / `ack` on their own rhythm; `history` is the audit feed. Survives
  restarts and lifecycle mismatches — you can message an agent that isn't up
  yet, and it reads the note when it next runs.
- **Push, no daemon.** `send(..., deliver=true)` *also* fires the recipient's
  native CLI at send time (`hermes chat -Q -q` / `codex exec` / `claude -p`),
  so it sees the message without polling — real push with zero extra always-on
  processes.
- **Live ask.** `ask_hermes` / `ask_codex` / `ask_claude` spawn a one-shot
  session and return the reply synchronously. Heavier than the mailbox; use
  when you need the answer immediately.

## Tools

| Tool | Behavior |
| --- | --- |
| `send(from_agent, to_agent, message, deliver=false)` | Persist; if `deliver`, also push to the recipient's native CLI. |
| `inbox(agent, unread_only=true)` | Messages addressed to `agent`, oldest first. |
| `ack(message_id)` | Mark read (idempotent). |
| `history(limit=50, agent=None)` | Recent messages newest-first; `agent` matches sender or recipient. |
| `ask_hermes(prompt)` | Live query → `hermes chat -Q -q`. |
| `ask_codex(prompt)` | Live query → `codex exec`. |
| `ask_claude(prompt)` | Live query → `claude -p`. |

Agents are the fixed set `claude`, `hermes`, `codex`. Identity is self-declared
(`from_agent`) — convention, not enforced auth; every process runs as the same
user on one machine, so there's nothing to defend against that it couldn't do
directly anyway.

## Requirements

- Python **3.10+**
- Whichever agent CLIs you want to reach on PATH (or see *Configuration*):
  `claude`, `hermes`, `codex`.

## Install

```bash
pip install -e .
```

`hardline-mcp` is the stdio server console-script entry point.

## Configuration

The mailbox lives at `~/.cache/hardline-mcp/mailbox.db` — no setup needed. Set
`HARDLINE_DB` to relocate it, or to run isolated instances (each agent's server
must point at the *same* file to share a mailbox).

Each agent's CLI must be launchable by hardline-mcp. If a binary isn't on
`PATH`, pin its **executable path** (path only — the fixed subcommand is
appended automatically) via env var:

- `HARDLINE_HERMES_CMD` — e.g. `C:/Users/you/AppData/Local/hermes/hermes-agent/venv/Scripts/hermes.exe`
- `HARDLINE_CLAUDE_CMD`
- `HARDLINE_CODEX_CMD` — *usually unnecessary:* Codex installs to a hash-named
  dir that changes on every update, so hardline-mcp **auto-discovers the newest
  `codex.exe`** rather than relying on a path that rots. Set this only to
  override discovery.

Resolution precedence per agent: env override → (codex only) auto-discovery →
bare command on `PATH`.

## Register with an MCP client

**Claude Code** (or any client using the `claude mcp` CLI):

```bash
claude mcp add hardline-mcp --scope user -- /path/to/hardline-mcp
```

**Hermes** (`~/.hermes/config.yaml`):

```yaml
mcp_servers:
  hardline:
    command: "/path/to/hardline-mcp"
    args: []
```

**Codex** (`~/.codex/config.toml` — Codex uses its own TOML config):

```toml
[mcp_servers.hardline]
command = '/path/to/hardline-mcp'
args = []
```

## Example flow

```text
# In agent A (claude), leave a durable note for hermes and push it live:
send(from_agent="claude", to_agent="hermes",
     message="deploy finished, logs at /tmp/deploy.log", deliver=true)

# hermes, whenever it runs, reads and acks:
inbox(agent="hermes")           -> [{message_id: 7, sender: "claude", ...}]
ack(message_id=7)

# Or ask hermes something and block for the answer:
ask_hermes(prompt="what's the current gateway status?")
```

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q
```

The suite includes a **headless end-to-end test** that launches two real
server subprocesses over MCP stdio and does a cross-instance round-trip — no
agents needed, runs in CI.

There is also a **live integration test** (`tests/test_live_agents.py`) that
spawns the *actual* `hermes` / `codex` / `claude` CLIs and drives the `ask_*`
bridges against their real brains. It's off by default (it costs plan tokens
and needs the CLIs installed) — it skips unless `HARDLINE_LIVE_TESTS=1`, and
skips per-agent when a CLI isn't reachable, so CI never runs it:

```bash
# hermes usually isn't on PATH — point at its binary, same as production
HARDLINE_LIVE_TESTS=1 HARDLINE_HERMES_CMD="/path/to/hermes" python -m pytest tests/test_live_agents.py -v
```

## License

MIT — see [LICENSE](LICENSE).
