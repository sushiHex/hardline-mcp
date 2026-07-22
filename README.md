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
| `ask_claude(prompt, model=None, effort="default", mode="default")` | Live query → `claude -p --model sonnet`; optionally pins a different model/effort and returns actual-model/fallback telemetry. |

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

Live queries are bounded so a hung CLI cannot wedge its MCP caller. Hermes and
Codex retain a 180-second default. Claude defaults to 900 seconds because
high-effort review and reasoning calls routinely exceed three minutes. Override
the Claude ceiling with a positive integer number of seconds:

```text
HARDLINE_CLAUDE_TIMEOUT_S=1200
```

An invalid or non-positive value fails the tool call before spawning Claude.

### Claude model and effort selection

`ask_claude` remains backward compatible: a prompt with no additional options
uses a plain `claude -p` path and returns `ok`/`reply`. That path *always*
explicitly pins `--model sonnet` rather than omitting `--model` — a bare
`claude -p` inherits whatever the installed Claude CLI's own global settings
currently default to, which is ambient, mutable state (an interactive
`/model` switch changes it for every unflagged invocation, including
hardline's). `sonnet` is Claude Code's own tier alias, not a versioned model
id, so it tracks whichever model Claude Code itself currently resolves
`sonnet` to — the same mechanism `model="fable"`/`model="opus"` already rely
on — and never needs bumping in code when a new Sonnet ships. This pin
applies uniformly to `ask_claude(prompt)` and to `send(..., to_agent="claude",
deliver=true)`'s push-notice path — both spawn `claude` the same way. Passing
`model="sonnet"` explicitly resolves to the same model but is a *different*
caller intent than omitting it, so it takes the full telemetry path below
(returning `actual_model`, usage, etc.) instead of the plain `ok`/`reply`
shortcut — the same as any other explicit `model=`.

For model-aware calls, set `model`, `effort`, or `mode`:

```text
ask_claude(
  prompt="Review this design and identify the highest-risk assumption.",
  model="fable",
  effort="high",
  mode="advisory",
)
```

Supported Claude effort values are `default`, `low`, `medium`, `high`, `xhigh`,
and `max`. `default` omits Claude Code's `--effort` flag. Unsupported values
fail before spawning Claude; there is no silent downgrade.

Optioned calls use Claude Code's `stream-json` output and add:

- `requested_model` and the `actual_model` from the final assistant event;
- `requested_effort` (`effective_effort` is `null`, because Claude Code does
  not echo the provider's effective effort);
- `api_key_source`, usage, model-usage, and rate-limit metadata;
- `subscription_verified`, which is `true` only when advisory telemetry reports
  `apiKeySource: none` and confirms that overage is not being used;
- a parsed `fallback` object when Fable emits `model_refusal_fallback` and the
  request continues on another model.

`mode="advisory"` is intended for read-only model panels. It disables tools,
slash commands, project customizations, and session persistence; runs in a
fresh neutral directory with a fixed minimal system prompt; and removes
Anthropic API-key/base-URL plus Bedrock/Vertex/Foundry overrides from the child
environment. This reduces accidental API-provider routing, but trusted command
wrappers and admin-managed Claude settings remain outside Hardline's control.
After execution, advisory calls therefore fail closed unless runtime telemetry
verifies first-party account auth with no overage. This is post-call evidence;
it cannot undo a request already made by a misconfigured trusted wrapper.

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

The headless suite includes a deterministic MCP-to-executable E2E that captures
the actual Claude argv and proves model/effort flags survive the full transport.
The live module additionally launches Hardline over stdio, requests Fable at
`low` effort in advisory mode, and verifies subscription/fallback telemetry.
Claude does not echo effective effort, so provider-side effort cannot be
asserted independently of the accepted CLI invocation. Live tests remain
opt-in and consume plan tokens.

## License

MIT — see [LICENSE](LICENSE).
