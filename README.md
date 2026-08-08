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
| `ask_codex(prompt, model=None, effort="default", mode="default", workdir=None, write=False)` | Ephemeral live query → `codex exec` (omitted `model` defers to Codex's own configured default); optional routing, isolation, telemetry, and opt-in write access. |
| `ask_codex_async(prompt, from_agent, label=None, model=None, effort="default", workdir=None, write=False)` | Fire-and-forget `ask_codex` dispatched on a bounded background thread pool; result is delivered through the mailbox (`sender="codex"`, `recipient=from_agent`) — poll it with `inbox`. |
| `ask_claude(prompt, model=None, effort="default", mode="default", workdir=None, write=False)` | Live query → `claude -p` (omitted `model` defers to Claude Code's own configured default); optional routing, isolation, telemetry, and opt-in write access (parity with `ask_codex`). |
| `ask_claude_async(prompt, from_agent, label=None, model=None, effort="default", workdir=None, write=False)` | Fire-and-forget `ask_claude` dispatched on a bounded background thread pool; result is delivered through the mailbox (`sender="claude"`, `recipient=from_agent`) — poll it with `inbox`. |

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

Live queries are bounded so a hung CLI cannot wedge its MCP caller. Hermes
retains a 180-second default. Claude defaults to 900 seconds; Codex defaults to
14400 seconds because deep repository reviews can legitimately run for hours.
Override either ceiling with a positive integer number of seconds:

```text
HARDLINE_CLAUDE_TIMEOUT_S=1200
HARDLINE_CODEX_TIMEOUT_S=14400
```

An invalid or non-positive value fails the tool call before spawning the agent.

`ask_*_async` dispatch through a small fixed-size background thread pool
(default 4 workers) rather than an unbounded thread per call, so repeated or
concurrent dispatches queue instead of piling up unlimited agent subprocesses.
Override the pool size with `HARDLINE_ASYNC_MAX_WORKERS` — validated the same
way as the timeouts above, except that this one is read once at startup, so an
invalid value fails the server at launch rather than a single tool call.

At shutdown, dispatches still queued are dropped rather than run; one already
in flight is awaited, since its agent subprocess can't be interrupted safely
mid-call. Without that, teardown would block until *every* queued dispatch had
run in turn — each up to its own configured timeout.

### Codex model, effort, isolation, and telemetry

`ask_codex(prompt)` preserves the original compact `ok`/`reply` response,
creates an ephemeral session, and terminates option parsing before the
prompt. Omitting `model` passes no `--model` flag at all, so Codex's own
configured default applies — the same posture `ask_hermes` already has
toward Hermes's default; hardline does not second-guess it. The same path
applies to `send(..., to_agent="codex", deliver=true)`. Hardline therefore
no longer persists one-shot review sessions or interprets a flag-shaped
prompt as a CLI option, while leaving model selection to Codex itself unless
a caller explicitly asks for a specific one.

Pass `model`, `effort`, `mode`, or `workdir` for the structured path. `model`
must be Codex's full identifier (`gpt-5.6-sol`, `gpt-5.6-terra`, ...), not a
shorthand like `"sol"` — hardline doesn't validate or expand it against any
alias table, so an unrecognized value is rejected by Codex itself at
execution time rather than silently substituted:

```text
ask_codex(
  prompt="Review the cancellation protocol.",
  model="gpt-5.6-terra",
  effort="xhigh",
  workdir="C:/src/project",
)
```

Supported efforts are `default`, `low`, `medium`, `high`, `xhigh`, `max`, and
`ultra`. `default` leaves Codex's model-specific reasoning default intact;
other values are transported as `model_reasoning_effort`. Unsupported values
and unsafe model identifiers fail before the process is spawned. Any explicit
`workdir` must already exist, is resolved once to an absolute path, and is
passed both as the child cwd and Codex `-C` (so relative paths are not applied
twice).

Structured calls use `codex exec --json` and return:

- `requested_model` and `requested_effort`;
- the final agent message, ephemeral `thread_id`, and structured token `usage`;
- `actual_model: null` and `effective_effort: null`, deliberately: Codex CLI
  0.145 JSONL does not emit either value, so Hardline does not guess;
- structured `turn.failed` errors (including nonzero process exits) instead of a
  generic subprocess error, while terminal-event ordering prevents a transient
  retry `error` from overriding a later successful `turn.completed`.

`mode="advisory"` is intended for isolated read-only model panels. It requires
the local Codex auth configuration to declare `auth_mode: chatgpt`, removes
OpenAI/Azure API-provider environment overrides, copies only `auth.json` into a
temporary `CODEX_HOME` (so global `AGENTS.md`/`AGENTS.override.md` guidance is
not inherited), ignores user configuration and rules, disables session
persistence, uses a separate fresh neutral workspace, selects the read-only
sandbox, and supplies fixed defensive developer instructions.
An explicit `workdir` is rejected in this mode because it would defeat neutral
isolation. The response reports `subscription_configured: true` after the local
preflight but leaves `subscription_verified: null`: unlike Claude, Codex JSONL
does not expose runtime auth-source or overage telemetry. This distinction is
intentional; local configuration is not post-call billing proof. Trusted binary
overrides and platform sandbox enforcement remain outside Hardline's control.

### Write access requires an explicit opt-in

`write=True` (on either `ask_codex` or `ask_claude`) is refused outright
unless **this hardline-mcp process's environment** has `HARDLINE_ALLOW_WRITE`
set to a recognized truthy value — regardless of what a caller asks for.
Write mode is unattended (stdin is `/dev/null`, so no approval prompt is ever
answered) and, once a workdir is reachable, no more restricted than what the
OS user running hardline-mcp could already do directly — a categorically
different exposure from every other hardline tool, which only ever runs
read-only or self-contained calls. Without a gate, any hardline registration
with no per-tool allow-list (unlike
[vram-mcp](https://github.com/sushiHex/vram-mcp)'s) would let any caller
reach it with zero human approval step — including an always-on registration
driven by inbound messages from an external platform. Set
`HARDLINE_ALLOW_WRITE=1` (also accepts `true`/`yes`, case-insensitive; `0`,
`false`, `no`, or unset all mean disabled — anything else fails loud naming
the bad value rather than silently staying disabled) only on registrations
where write access is actually wanted; leave it unset (the default)
everywhere else, e.g. an always-on gateway's registration.

### Codex write access and background dispatch

`ask_codex` is read-only unless `write=True` is passed explicitly (and
`HARDLINE_ALLOW_WRITE=1` is set — see above) — omit `write` and behavior is
unchanged from before this option existed. `write=True` requires an explicit
`workdir` (never an implicit cwd) and is rejected with `mode="advisory"`
(advisory is fixed read-only by design). It adds `--sandbox workspace-write
-a never`: approvals are disabled because a spawned Codex process's stdin is
`/dev/null`, so any approval prompt would just hang until timeout instead of
ever being answered — the sandbox boundary is what keeps an unattended,
un-approvable run safe.

```text
ask_codex(
  prompt="Add input validation to the login handler.",
  workdir="C:/src/project",
  write=True,
)
```

For a task that shouldn't block the caller, `ask_codex_async` dispatches the
same `ask_codex` through the bounded background thread pool (see
*Configuration*) and returns `{"ok": true, "dispatched": true, "label": ...}`
immediately. The result lands in the mailbox as a
message from `"codex"` to `from_agent` once the run finishes — poll it the
same way you'd poll for any other mailbox message:

```text
ask_codex_async(
  prompt="Refactor the retry loop; write the diff.",
  from_agent="claude",
  workdir="C:/src/project",
  write=True,
  label="retry-refactor",
)
# later:
inbox(agent="claude")
```

`label` is echoed back in the delivered message body so a caller firing
several concurrent dispatches can match each result to its request. This is
fire-and-forget, not durable: if hardline-mcp restarts before a dispatched
task finishes, that task is lost — there is no task table, only the
existing send/inbox mailbox the result is dropped into on completion.

### Claude model and effort selection

`ask_claude`'s bare `ask_claude(prompt)` response *shape* stays backward
compatible: a prompt with no additional options still returns the plain
`ok`/`reply` object, not the fuller telemetry shape. Tool access is not part
of that compatibility promise — see *Claude write access and background
dispatch* below for the one behavior change (Edit/Write/NotebookEdit are now
denied unless `write=True`). Omitting `model` passes no `--model` flag at
all, so Claude Code's own configured default applies — the same posture
`ask_hermes`/`ask_codex` already have toward their own CLI's default;
hardline does not second-guess it. This applies uniformly to
`ask_claude(prompt)` and to `send(..., to_agent="claude", deliver=true)`'s
push-notice path — both spawn `claude` the same way. Passing an *explicit*
`model=` (including `model="sonnet"`, which is Claude Code's own tier alias
and tracks whichever model it currently resolves that to) is a different
caller intent than omitting it, so it takes the full telemetry path below
(returning `actual_model`, usage, etc.) instead of the plain `ok`/`reply`
shortcut.

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

### Claude write access and background dispatch

Parity with Codex: unless `write=True` is passed, every `ask_claude` call —
including the bare `ask_claude(prompt)` path — denies Claude the
`Edit`/`Write`/`NotebookEdit` tools (`--disallowedTools Edit,Write,
NotebookEdit`). Read/Grep/Bash and the rest of the built-in toolset still
work, the same way Codex's read-only sandbox permits inspection but not
mutation — this is a closer analog than advisory mode's zero-tools
restriction, which is a separate, stricter concept for isolated opinions.

`write=True` requires an explicit `workdir` (never write into hardline-mcp's
own cwd), is rejected with `mode="advisory"`, and is refused unless
`HARDLINE_ALLOW_WRITE=1` is set for this process (see *Write access requires
an explicit opt-in* above). It grants full tool access and adds
`--permission-mode bypassPermissions`: approvals are disabled because a
spawned Claude process's stdin is `/dev/null`, so an interactive permission
prompt would hang until timeout instead of ever being answered — the same
rationale as Codex's `-a never`.

```text
ask_claude(
  prompt="Add input validation to the login handler.",
  workdir="C:/src/project",
  write=True,
)
```

`workdir` also works without `write` — Claude has no `-C`/`--cd` flag, so
hardline targets it by launching the child process with that directory as
its `cwd`; omitted, `ask_claude` inherits whatever directory hardline-mcp
itself was started from, same as before this option existed.

`ask_claude_async` mirrors `ask_codex_async` exactly: dispatches `ask_claude`
through the same bounded background thread pool and delivers the result
through the mailbox (`sender="claude"`, `recipient=from_agent`) once it
finishes.

```text
ask_claude_async(
  prompt="Refactor the retry loop; write the diff.",
  from_agent="codex",
  workdir="C:/src/project",
  write=True,
  label="retry-refactor",
)
# later:
inbox(agent="codex")
```

Same caveats as the Codex version: `label` is echoed back for matching
concurrent dispatches, and this is fire-and-forget — a hardline-mcp restart
before completion loses the task, since only the existing mailbox holds the
eventual result, not a task table.
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

The headless suite includes deterministic MCP-to-executable E2Es that capture
the actual Claude and Codex argv and prove model/effort options survive the full
transport. The live module additionally launches Hardline over stdio, requests
Fable and Sol at `low` effort in advisory mode, and verifies each CLI's truthful
telemetry contract. Claude does not echo effective effort; Codex JSONL echoes
neither effective effort nor served model. Those fields therefore remain null
rather than being inferred from the requested options. Live tests remain opt-in
and consume plan tokens.

## License

MIT — see [LICENSE](LICENSE).
