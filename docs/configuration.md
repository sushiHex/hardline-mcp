# Configuration

[Start here](../README.md) · [Messaging and jobs](messaging.md) · [Inbox signals](inbox-signals.md)

Set environment variables on the **MCP server registration**. Reconnect after
changing its environment or updating the editable source; an already-running
server retains its loaded code. `server_info()` reports the code revision,
package version, module path, database path, limits, write gate, job counts,
and watcher command. Check `code_revision` when confirming an update is loaded.

## Mailbox and CLI paths

| Variable | Default / purpose |
| --- | --- |
| `HARDLINE_DB` | `~/.cache/hardline-mcp/mailbox.db`; clients must use the same file to exchange messages. |
| `HARDLINE_CLAUDE_CMD` | Override the Claude executable path. |
| `HARDLINE_CODEX_CMD` | Override the Codex executable path. |
| `HARDLINE_HERMES_CMD` | Override the Hermes executable path. |
| `HARDLINE_AGENT` | Declare `claude`, `codex`, or `hermes` when needed. |
| `HARDLINE_AGENT_LABEL` | Select a fixed session role; see [ownership and reconnects](messaging.md#name-a-session). |

Executable overrides are paths, without arguments: Hardline appends `-p` for
Claude, `exec` for Codex, or `chat -Q -q` for Hermes. For example,
`HARDLINE_HERMES_CMD=C:/Users/you/AppData/Local/hermes/hermes-agent/venv/Scripts/hermes.exe`.

Explicit overrides take precedence. Codex then uses the executable on `PATH`,
falling back to legacy Windows install discovery only when it is absent. This
lets the current CLI's maintained launcher take precedence over older bundled
binaries. Claude and Hermes use their bare commands on `PATH` without discovery.
When pinning Codex, prefer its maintained launcher over a versioned release path.

### Codex compatibility errors

If Codex says a model needs a newer client, check the executable selected by
`HARDLINE_CODEX_CMD` or the MCP server's `PATH` with `--version`. Updating a
different Codex installation will not change that selection. Reconnect the MCP
server after a Hardline update; restart its host if it inherited an old `PATH`.

Use `codex debug models` on the selected current CLI to inspect its model catalog
without starting a task. Pass an exact supported identifier, such as
`gpt-5.6-sol` or `gpt-5.6-terra`, rather than guessing a family name. Availability
also depends on sign-in and rollout; see the [official model guide](https://learn.chatgpt.com/docs/models).
Hardline preserves the requested model and reports errors without substituting
a fallback.

## Limits

| Variable | Default | Controls |
| --- | --- | --- |
| `HARDLINE_CLAUDE_TIMEOUT_S` | `900` | Claude subprocess time budget in seconds. |
| `HARDLINE_CODEX_TIMEOUT_S` | `14400` | Codex subprocess time budget in seconds. |
| `HARDLINE_ASYNC_MAX_WORKERS` | `4` | Concurrent background workers per MCP server. |
| `HARDLINE_ASYNC_MAX_PENDING` | Four times the worker count | Accepted jobs, running plus queued, per server. |

These values must be positive integers. Worker limits are read at startup;
invalid timeout values fail the call before spawning an agent. Hermes uses a
fixed 180-second budget. Queued dispatches are dropped at shutdown; calls already
in flight are awaited. See [job receipts and cancellation](messaging.md#track-background-work)
for admission and recovery behavior.

## Models, effort, and results

`ask_codex` and `ask_claude` accept `model`, `effort`, `mode`, `workdir`, and
`write`; their async forms use the same options. Bare `ask_*(prompt=...)` calls
return the compact `ok`/`reply` shape. Additional options select structured
execution and telemetry. `ask_hermes` accepts only `prompt` and uses Hermes's
own defaults.

Omitting `model` passes no model flag. Hardline passes identifiers through
without expanding aliases; use an identifier supported by the selected CLI.
Claude read calls discard user settings, so their omitted model and effort use
the CLI's built-in defaults. Pass them explicitly when that distinction matters.

| Agent | Accepted `effort` values |
| --- | --- |
| Codex | `default`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra` |
| Claude | `default`, `low`, `medium`, `high`, `xhigh`, `max` |

`default` omits the effort override. Hardline rejects values outside these sets
and unsafe model identifiers before spawning. The selected CLI/model may still
reject an unsupported combination.

```text
ask_codex(prompt="Review the cancellation protocol.", effort="high",
          workdir="/absolute/path/to/project")
ask_claude(prompt="Challenge this design: ...", effort="high", mode="advisory")
```

An explicit `workdir` must already exist and is resolved to an absolute path.
Codex receives it as both cwd and `-C`; Claude receives it as cwd. Without one,
default-mode calls inherit the server's working directory. Advisory mode uses
a fresh neutral directory and rejects `workdir`.

Structured results include the final reply, requested model/effort, usage, and
available execution metadata:

- Codex uses JSONL events, reports the ephemeral `thread_id`, and preserves
  structured `turn.failed` errors. `actual_model` and `effective_effort` remain
  null; the adapter does not infer served settings from the request.
- Claude uses stream JSON, reports `actual_model`, `api_key_source`, usage,
  model usage, rate limits, and parsed fallback metadata when provided.
  `effective_effort` remains null.

## Execution modes and write access

| Mode | Codex | Claude |
| --- | --- | --- |
| Default (`write=False`) | Explicit `--sandbox read-only`; ephemeral session. | Denies Edit/Write/NotebookEdit and discards user settings; Bash remains available. |
| `mode="advisory"` | Fresh neutral workspace and temporary config/auth home; read-only sandbox and fixed developer instructions. | Fresh neutral workspace; tools, slash commands, project customizations, and persistence disabled. |
| `write=True` | `workspace-write` sandbox with approvals disabled. | Full tool access with `bypassPermissions`. |

**Claude's default read mode is not a filesystem sandbox.** Its command
classifier may block direct writes while allowing side effects from builds,
tests, interpreters, or hooks. Codex requests an OS sandbox; enforcement depends
on the installed CLI and platform. Trusted wrappers and managed settings remain
outside Hardline's control.

Writes require all three: `HARDLINE_ALLOW_WRITE=1` in the serving process,
`write=True` in the call, and an explicit existing `workdir`. Write access is
incompatible with advisory mode. The gate also accepts `true`/`yes`, ignoring
case; unset, `0`, `false`, or `no` disable it. Other values fail with an error.

Write calls are unattended: child stdin cannot answer approval prompts. Enable
them only on registrations intended to edit files. Use a Git worktree so the
caller can inspect the resulting diff:

```sh
git worktree add ../project-review -b review-change
```

Then, through the connected agent:

```text
ask_claude(prompt="Add input validation and run the relevant tests.",
           workdir="/absolute/path/to/project-review", write=True)
```

Hardline strips `HARDLINE_ALLOW_WRITE` from spawned children. Claude spawns also
use `--strict-mcp-config` to avoid loading the host's MCP registrations. These
controls do not prevent an OS user or trusted wrapper from changing its own
environment. See [the design rationale](architecture.md#why-read-controls-are-explicit).

### Advisory authentication

Codex advisory calls require local `auth_mode: chatgpt`, copy only `auth.json`
into a temporary `CODEX_HOME`, and remove OpenAI/Azure provider overrides. They
ignore user configuration and rules. A successful preflight reports
`subscription_configured: true`; `subscription_verified` stays null because
the adapter has no runtime billing proof.

Claude advisory calls remove Anthropic API-key/base-URL and
Bedrock/Vertex/Foundry overrides. After execution they require runtime telemetry
showing first-party account authentication (`apiKeySource: none`) without
overage, and report `subscription_verified`. A failed post-call check cannot
undo a request already made by a misconfigured wrapper.

## Quota-aware Claude routing

Routing is optional. `HARDLINE_QUOTA_ROUTER_COMMAND_JSON` specifies a JSON argv
array for an operator-provided collector that prints a subscription snapshot:

```text
HARDLINE_QUOTA_ROUTER_COMMAND_JSON=["python","subscription_quota_monitor.py","--json"]
HARDLINE_CLAUDE_WEEKLY_RESERVE_PERCENT=5
HARDLINE_QUOTA_ROUTER_TIMEOUT=30
```

The collector is not included. Its snapshot must contain
`providers.{claude,chatgpt}.{status,weekly.remaining_percent}`. Missing,
malformed, failed, or timed-out telemetry blocks routing instead of guessing.

Before each Claude launch, Hardline compares weekly remaining percentages and
redirects eligible work to ChatGPT when it has more headroom. Redirection
accepts no Claude model pin, workspace, or write request and uses Codex advisory
mode. Ineligible requests return an explicit reason. A dispatch lock serializes
the final quota check and Claude run; resets take effect on the next call.

`require_claude=True` bypasses balancing, but still respects the reserve floor.
A one-call reserve exception requires `override_claude_reserve=True` and a
non-empty `override_reason` of at most 500 characters. It cannot bypass
unavailable telemetry, an unavailable provider, or zero allowance.

The response's `routing` records non-default request options and whether an
override applied; async jobs retain the same metadata. Authority is
`caller_asserted`, not proof of owner approval. Invalid override attempts are
recorded too, with overlong reasons bounded and truncation reported. The final
locked check revalidates the request before launching.
