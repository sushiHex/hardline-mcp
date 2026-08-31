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
| `inbox(agent, unread_only=true, limit=25, auto_ack=true)` | One bounded batch addressed to `agent`, oldest first. Consumes what it returns so each poll advances; returns `remaining` — poll again while it's non-zero. The whole response is capped, not just each body, and it returns `first_message_id` / `last_message_id` / `recover_with` so a lost response is recoverable. |
| `peek(message_id)` | One message with its body in full, never truncated and never acked. |
| `ack(message_id)` | Mark read (idempotent). |
| `history(limit=50, agent=None, before_id=None)` | Recent messages newest-first; `agent` matches sender or recipient. Never acks and never hides acked messages, so it is the recovery path for anything `inbox` consumed. Row count, each body, and the aggregate response are all capped; page with the returned `next_before_id` while `has_more`. |
| `list_agents()` | The addressable roster, the sessions **live right now** (`live_sessions`), every recipient/sender name the mailbox has ever seen — a history, with each lane marked `live` — and **your own identity**, including whether this session can be addressed individually. |
| `register_session(label, agent=None)` | Claim `label` as this session's name, so mail can be aimed at it: `codex:construction`. The runtime answer to a static MCP env block that can't name two sessions differently. Keeps previously-held lanes, so renaming never strands in-flight results. |
| `release_session(label)` | Give a claimed name back, so a mistaken claim doesn't stay owned until the process exits. Only releases a name this session holds. |
| `server_info()` | Version, schema version, module path, db path, pid, effective limits, per-agent timeout budgets, job counts, and whether write is enabled — for answering "is the fix live?" in one call. |
| `job_status(job_id)` | State and timings of one dispatch: `queued` / `running` / `completed` / `failed` / `cancelled` / `lost`. Answers "is it still running?", which polling an inbox cannot. |
| `job_result(job_id)` | The terminal result in full. Recorded against the job *before* delivery is attempted, so it survives the message being consumed, lost, or never sent. |
| `job_cancel(job_id)` | Stop a running dispatch and kill its whole child tree. Works across processes — one session can cancel a job another started. |
| `list_jobs(state=None, agent=None, requester=None, active_only=false, limit=25)` | Recent jobs newest-first, with a state-count summary. `active_only` answers "what is still in flight?". |
| `ask_hermes(prompt)` | Live query → `hermes chat -Q -q`. |
| `ask_codex(prompt, model=None, effort="default", mode="default", workdir=None, write=False)` | Ephemeral live query → `codex exec` (omitted `model` defers to Codex's own configured default); optional routing, isolation, telemetry, and opt-in write access. |
| `ask_codex_async(prompt, from_agent, label=None, model=None, effort="default", mode="default", workdir=None, write=False)` | Fire-and-forget `ask_codex` dispatched on a bounded background thread pool; result is delivered through the mailbox (`sender="codex"`, `recipient=from_agent`) — poll it with `inbox`. |
| `ask_claude(prompt, model=None, effort="default", mode="default", workdir=None, write=False)` | Live query → `claude -p` (omitted `model` defers to Claude Code's own configured default); optional routing, isolation, telemetry, and opt-in write access (parity with `ask_codex`). |
| `ask_claude_async(prompt, from_agent, label=None, model=None, effort="default", mode="default", workdir=None, write=False)` | Fire-and-forget `ask_claude` dispatched on a bounded background thread pool; result is delivered through the mailbox (`sender="claude"`, `recipient=from_agent`) — poll it with `inbox`. |

Async dispatch is backed by a durable job. `ask_*_async` returns a `job_id`
that identifies the run across a hardline-mcp restart — a label is a
correlation aid the caller picks and may reuse, a job id is an identity. The
row records what was asked, who asked, which process owns it, every state
transition, and the terminal result. A job whose owning process exits without
finishing is reported `lost` rather than vanishing; that state is resolved on
read, because the process that would have written it is precisely the one that
died.

A timeout no longer reports only `timeout after Ns`. It returns `timed_out`,
`timeout_s`, `elapsed_s`, `timeout_layer`, `stdout_chars`/`stderr_chars`,
`produced_output`, and the `partial_stdout`/`partial_stderr` the child had
already written — so a healthy-but-slow agent is distinguishable from a wedged
one, and a partially complete answer is not thrown away.

Agents are the fixed set `claude`, `hermes`, `codex`. Identity is self-declared
(`from_agent`) — convention, not enforced auth; every process runs as the same
user on one machine, so there's nothing to defend against that it couldn't do
directly anyway.

### Session lanes

Several Claude Code sessions can run at once, and they'd otherwise all share
the single `claude` mailbox — every session seeing every other's results, and
able to `ack` them out of each other's inbox. Since each session spawns its
**own** hardline process over stdio, the process *is* the session and derives
its own lane from `CLAUDE_CODE_SESSION_ID` + `CLAUDE_PROJECT_DIR`:
`claude:fonts.1a2b3c4d`.

Nothing to opt into. `ask_*_async(from_agent="claude")` delivers to the
calling session's lane, `inbox(agent="claude")` reads that lane **plus** the
unqualified `claude` (so broadcasts still arrive), and `ack` refuses messages
belonging to another session's lane. Keying on the session id rather than the
process means a `/mcp` reconnect doesn't orphan in-flight results.

Codex sets neither variable. Its MCP child gets a deliberately minimal
environment — 22 variables, none of them a session id — so for Codex the lane
cannot come from the environment at all.

It comes from the **process** instead. Each session spawns its own hardline over
stdio, so the thing that spawned it *is* the session: its pid paired with its
creation time is unique, stable for the session's life, and needs no
cooperation from the agent. The agent name comes from the same place — the
launcher is called `codex.exe`. A Codex terminal session therefore registers
itself at startup exactly as a Claude one does, with no call and no config.

Two cases deliberately get no lane. A hardline running underneath **another**
hardline was spawned by `ask_codex`/`ask_claude` — a one-shot doing a single
piece of work, not a session anybody can address. And a launcher whose name
matches no known agent is left alone rather than guessed at.

Order of precedence: `HARDLINE_AGENT_LABEL` if pinned, then a session id the
host supplied, then the parent process.

### Naming a session yourself

`register_session` overrides the derived name when you want a human one:

```python
register_session(label="construction", agent="codex")
# -> {"ok": true, "lane": "codex:construction"}
```

From then on `send(to_agent="codex:construction", ...)` reaches **that**
session and only that session, and it shows up in `list_agents()` under
`live_sessions`. `agent` may be omitted where it can be inferred — a Claude
Code session, or `HARDLINE_AGENT` set in the registration.

Renaming never strands mail. An async result's recipient is fixed when the job
is *dispatched*, so a session keeps every lane it has held: it is **addressed**
by its newest name but still **consumes** mail sent to the older ones. The
registry records all of them, so an older name still shows a live holder and
can't be claimed out from under the session still reading it.

A claim is refused if a *live* session already holds that name. A dead holder's
claim is ignored, so a label doesn't become unusable forever because the
session that used it crashed.

Two consequences worth knowing, both deliberate:

- **A label is a role, not an instance.** Mail sent to `codex:construction` is
  consumable by whoever holds that name — including a session that claims it
  *after* the message was sent. You can address a name before anyone answers to
  it, and a later claimant inherits the backlog. Making each claim a distinct
  address would mean mail to a name nobody currently holds is undeliverable by
  construction, which is the stranding this exists to remove.
- **A claim does not survive its process.** After a `/mcp` reconnect a Codex or
  Hermes session is anonymous again and must call `register_session` a second
  time. That's recoverable precisely because of the point above: re-claiming
  the same label succeeds (the old holder is dead) and the session picks up
  whatever arrived while it was away.

For a session that should always have the same name, set both variables in its
MCP registration instead — `HARDLINE_AGENT` (which agent this is: hardline
cannot tell for Codex or Hermes) and `HARDLINE_AGENT_LABEL` (the name). A
runtime `register_session` overrides them; the last writer of a name wins.
`HARDLINE_AGENT_LABEL` without `HARDLINE_AGENT` gives the session a name it
cannot own — ownership is checked on the full `agent:label`, so that a session
called `construction` cannot reach into another agent's mail.

### Who is actually out there

Liveness is derived from the OS, never stored — a session that crashes can't
write "I died", so its row is resolved on read from the pid **and** its
creation-time token (a pid alone is not an identity; a reused one would
otherwise inherit the previous session's lane and its mail). Nothing to clean
up: a vanished session simply stops being live, and is pruned by the next
reader. On macOS that token is unavailable, so identity there degrades to the
pid alone and a reused pid is undetectable.

This matters because a dead session's lane looks exactly like a live one in the
mailbox. `list_agents()` therefore separates them:

- `live_sessions` — who is running **now**, and can receive lane-qualified mail
- `observed_recipients` — every name the mailbox has ever seen, a *history*,
  with each lane-qualified entry marked `live: true/false`

Sending to a lane with no registered holder still persists, and the response
says so. The wording is careful, because the registry cannot see everything: a
session running older code never registers, one whose announcement failed is
absent, and a probe that can't answer looks like an exit. So `send` reports
that **no registered holder was found** — not that the message is unreadable.

For the same reason, absence does not grant ownership. A claim on an unheld
lane is refused when unfinished work is still addressed to it and that work's
owner is alive: an unregistered consumer is invisible here, but its outstanding
job is not. A successful claim reports the backlog it inherited, since a label
is a role and a claimant takes on whatever was waiting at that name.

`list_agents` marks each observed lane with `registered_holder`, flags any lane
two live sessions both hold (`contested_lanes` — they will drain each other's
mail), and reports a `registration_warning` if this session failed to register
itself.

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
`HARDLINE_ALLOW_WRITE=1` is set — see above). Every non-advisory read call
pins `--sandbox read-only` rather than leaving it to the host.

That pin is the guarantee. `--sandbox` used to be passed only for `write`
(`workspace-write`) and advisory (`read-only`), so the default path silently
inherited whatever `~/.codex/config.toml` set. Measured on a host with
`[windows] sandbox = "elevated"` and the target project marked
`trust_level = "trusted"`: a plain `ask_codex` call ran `echo x > probe.txt`
and **the file was written**, while this README promised read-only. (In an
*untrusted* directory Codex refuses to run at all, which is why the hole was
easy to miss — the obvious test never exercises the sandbox.) A safety claim
has to be enforced by a flag hardline passes, not by the operator's config
happening to agree with it.

Unlike Claude's read posture — a command classifier, see below — this is a
real OS-level sandbox, so a side-effect write from a test run or build is
blocked too.

`write=True` requires an explicit `workdir` (never an implicit cwd) and is
rejected with `mode="advisory"` (advisory is fixed read-only by design). It
swaps in `--sandbox workspace-write -a never`: approvals are disabled because
a spawned Codex process's stdin is `/dev/null`, so any approval prompt would
just hang until timeout instead of ever being answered — the sandbox boundary
is what keeps an unattended, un-approvable run safe.

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
NotebookEdit`) and drops the host's settings layer (`--setting-sources ""`).
Read/Grep/Bash and the rest of the built-in toolset still work — a closer
analog to Codex's read-only sandbox than advisory mode's zero-tools
restriction, which is a separate, stricter concept for isolated opinions.

**Both halves are load-bearing.** `--disallowedTools` denies the Edit/Write
*tools* and nothing more; Bash writes files just as well. A host whose
`settings.json` grants a blanket `Bash(*)` permission overrides Claude Code's
own built-in Bash write guard, and for a while every "read-only" `ask_claude`
call here could modify the filesystem. Measured against a real `claude -p`
asked to run `echo x > probe.txt`:

| flags | file written |
| --- | --- |
| `--disallowedTools Edit,Write,NotebookEdit` | **yes** |
| `+ --strict-mcp-config` | **yes** |
| `+ --setting-sources ""` | no |

**Read mode is a posture, not a sandbox.** Dropping the settings layer
restores a command *classifier*. It catches a direct write; it cannot catch a
write that is a side effect — a test run dropping a cache, a build emitting
artifacts, a `git` command firing a hook, an interpreter whose name says
nothing about what it does. Treat it as default-safe, never as containment.

One consequence: with the host settings layer gone, an omitted `model`/`effort`
on a read call falls to Claude Code's **built-in** defaults rather than
whatever `settings.json` configures. On a host set to a high default effort
that is a cheaper, shallower answer than the same call used to give — pass
`effort` explicitly when it matters. `CLAUDE.md` is unaffected
(`--setting-sources` selects settings layers only), so a spawned Claude still
picks up the target repo's conventions.

`--strict-mcp-config` is passed on **every** spawn, read and write alike, so a
spawned agent loads no MCP servers. It is not, on its own, a recursion
boundary: it stops the child *discovering* hardline through the host's MCP
config, but not from launching `claude --mcp-config`, running the hardline
executable, or starting another agent CLI. The boundary is the environment —
`HARDLINE_ALLOW_WRITE` is stripped from every spawned child, so no route back
into hardline arrives write-enabled.

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

**Point `workdir` at a git worktree.** The reason write mode exists is to stop
an orchestrating agent asking for a change in prose and then reimplementing it
itself — paying for the same work twice. Have Claude edit the files, then
review a `git diff` rather than re-deriving the change. A worktree keeps that
isolated from anything else in flight and makes the review a clean diff:

```text
git worktree add ../proj-feature -b feature
ask_claude(prompt="...", workdir="C:/src/proj-feature", write=True)
git -C ../proj-feature diff
```

The directory must already exist — `write=True` will not create it, and a
missing `workdir` is rejected rather than silently falling back to a default.

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

# hermes, whenever it runs, drains a bounded batch (the read acks it):
inbox(agent="hermes")   -> {messages: [{message_id: 7, sender: "claude", ...}],
                            count: 1, remaining: 0, truncated: 0}

# A long body arrives shortened; fetch that one in full on demand:
peek(message_id=7)

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
