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
| `HARDLINE_CODEX_WINDOWS_SANDBOX` | Windows only. `elevated` (default), `unelevated`, or `inherit` (pass nothing; Codex's own configuration decides, which in advisory mode excludes the host's `config.toml`). See [the Windows sandbox](#codex-windows-sandbox). |
| `HARDLINE_AGENT` | Declare `claude`, `codex`, or `hermes` when needed. |
| `HARDLINE_AGENT_LABEL` | Select a fixed session role; see [ownership and reconnects](messaging.md#name-a-session). |
| `CODEX_HOME` | Not a hardline variable, but read by the `codex queue` it runs to wake a Codex session. Codex does not pass it to MCP servers, so if Codex uses a custom home, set the same value in hardline's MCP registration env. See [queue-wake](inbox-signals.md#codex-queue-wake-preferred). |

Executable overrides are paths, without arguments: Hardline appends `-p` for
Claude, `exec` for Codex, or `chat -Q -q` for Hermes. For example,
`HARDLINE_HERMES_CMD=C:/Users/you/AppData/Local/hermes/hermes-agent/venv/Scripts/hermes.exe`.

Explicit overrides take precedence. Codex then uses the executable on `PATH`,
falling back to legacy Windows install discovery only when it is absent. This
lets the current CLI's maintained launcher take precedence over older bundled
binaries. Claude and Hermes use their bare commands on `PATH` without discovery.
When pinning Codex, prefer its maintained launcher over a versioned release path.

### Codex Windows sandbox

On Windows a spawned Codex runs with `-c windows.sandbox="elevated"` unless
`HARDLINE_CODEX_WINDOWS_SANDBOX` says otherwise. `unelevated` is upstream's
fallback, whose isolation is weaker, especially for the network. Hardline's
`--sandbox read-only` pin applies under either. Reconnect the MCP server after
changing the variable.

The elevated sandbox needs one workaround for Codex 0.161.0 (#47,
openai/codex#51590). Before each command its setup refresh opens every file
under `%LOCALAPPDATA%\OpenAI\Codex\runtimes` for an ACL update. While the
Codex desktop app runs its computer-use runtime, that fails with a sharing
violation (os error 32) and no command starts:
`helper_unknown_error: setup refresh had errors`, with `runtime read/execute
validation failed` in `~/.codex/.sandbox/sandbox.<date>.log`.

The refresh finds that tree through `LOCALAPPDATA` and skips a missing one.
So an elevated Codex gets `LOCALAPPDATA` pointed at a path that does not
exist, and its commands get the real value back through
`-c shell_environment_policy.set.LOCALAPPDATA=...`. The skip costs the sandbox
its read grant on the desktop app's computer-use runtime, which a
hardline-spawned Codex has no connectors to use. That `set` also wins over a
`shell_environment_policy` of yours that would drop `LOCALAPPDATA`. The
workaround applies only to the elevated sandbox chosen here: under `inherit`,
an elevated sandbox from Codex's own configuration still fails this way.

### Codex compatibility errors

If Codex says a model needs a newer client, check the executable selected by
`HARDLINE_CODEX_CMD` or the MCP server's `PATH` with `--version`. Updating a
different Codex installation will not change that selection. Reconnect the MCP
server after a Hardline update; restart its host if it inherited an old `PATH`.

Use `codex debug models` on the selected current CLI to inspect its model catalog
without starting a task. Pass an exact supported identifier, such as
`gpt-5.6-sol` or `gpt-5.6-terra`, or a family name that Hardline resolves from
that catalog (see [models](#models-effort-and-results)). Availability
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
| `HARDLINE_GITHUB_TIMEOUT_S` | `60` | One deadline for every `gh` call that collects a pull request. |
| `HARDLINE_GITHUB_MAX_CHARS` | `400000` | Evidence piped to one reviewer; files past it are listed as `over_budget`. |
| `HARDLINE_GITHUB_SNAPSHOT_MAX_BYTES` | `4000000` | Largest snapshot collected; over it, collection fails and names the largest patches. |
| `HARDLINE_GITHUB_SNAPSHOT_DIR` | `~/.cache/hardline-mcp/github` | Snapshot store. An empty value disables storage. |

These values must be positive integers. Worker limits are read at startup;
invalid timeout values fail the call before spawning an agent. Hermes uses a
fixed 180-second budget. Queued dispatches are dropped at shutdown; calls already
in flight are awaited. See [job receipts and cancellation](messaging.md#track-background-work)
for admission and recovery behavior.

## Models, effort, and results

`ask_codex` and `ask_claude` accept `model`, `effort`, `mode`, `workdir`, and
`write`; their async forms use the same options. A bare `ask_claude(prompt=...)`
returns the compact `ok`/`reply` shape, and options select structured execution
and telemetry. Every `ask_codex` call is structured, a bare one included: only
Codex's JSONL events show a turn whose commands never started. So a bare call
that ends without a reply, or whose output has a line that is not JSON, is
reported `ok: false` rather than returning whatever it printed. `ask_hermes`
accepts only `prompt` and uses Hermes's own defaults.

Omitting `model` passes no model flag. Hardline passes identifiers through
unchanged; use an identifier supported by the selected CLI. Claude Code accepts
its own aliases, such as `opus`.

For Codex, a letters-only family name such as `astra` or `sol` resolves to the
newest current model in that family, read from `codex debug models` on the
selected executable: `astra` becomes `gpt-6-astra`, and `sol` becomes
`gpt-6-sol` rather than `gpt-5.6-sol`. Only identifiers shaped
`<prefix>-<generation>-<family>` qualify, so variants such as `-mini` and dated
snapshots never stand in for the family. Hidden and retiring models (any
`upgrade`) are skipped, and generations compare numerically. Advisory calls read
the catalog from the same isolated home they run in.

A name the catalog lists as an identifier is used as is. A family that cannot
be resolved (an unavailable catalog, no match, or a tie) is passed to Codex
unchanged, as before, so custom-provider model names keep working. Results,
async receipts, and job requests report the lookup under `model_resolution`,
with `resolved: null` and a `reason` when nothing was substituted. Async jobs
resolve once at admission; the worker runs exactly the recorded model.
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
- A Codex turn that could start none of its shell commands ("Failed to
  create unified exec process") is reported `ok: false`, with the reply under
  `partial_reply` and the count in `commands_not_started`: it was written
  without reading anything. When only some did not start, the count is
  reported and `ok` is kept. A command that started and failed is ordinary.
  This holds for every `ask_codex` call, a bare one included.
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

## GitHub evidence for reviews

A spawned reviewer cannot reach GitHub: Codex's sandbox blocks the network and
`gh`'s config, and Claude runs without the user's permissions. `github=` has
Hardline collect a pull request with its own authenticated `gh` and pipe it to
the reviewer's stdin.

```text
ask_codex(prompt="Review this PR for correctness.", github="owner/repo#123")
ask_claude(prompt="Attack this change.", github="owner/repo#123@<head sha>",
           github_exclude=["*.lock"])
```

`github` takes `owner/repo#N`, `owner/repo#N@<head sha>` (fails unless the head
is that commit), or a `snapshot_id`. Collection reads the PR, its per-file diff
(`gh api .../pulls/N/files?per_page=100 --paginate --slurp`, so `gh` must
support `api --slurp`), and the PR again; a head or base that moved in between
fails the call rather than mixing revisions. Only github.com is used (`GH_HOST`
is forced), `gh` never prompts, and for an async job `job_cancel` reaches every
`gh` child.

The PR's base is shown as GitHub recorded it at the PR's last update; the base
branch may have moved since. A reviewer that can read a local checkout (Codex
with `workdir`, or Claude with `github_tools="read"`) is told the checkout may
not be at the PR's head, and that the evidence is the PR.

**One evidence set for several reviewers.** Two calls given the same reference
can see different PRs while it moves. `github_snapshot(ref)` collects once and
returns a `snapshot_id`; every call given that id receives the same evidence.
Snapshots are content addressed (`snapshot_id` is the SHA-256 of the canonical
evidence), kept about 24 hours after last use, and pruned opportunistically
when a new one is written. An expired id is an explicit error, never a silent
re-collection, and so is a snapshot written by a Hardline version with another
snapshot schema. They are private repository content on disk, protected by
the profile directory's permissions. `HARDLINE_GITHUB_SNAPSHOT_DIR` must be
absolute; set it to `""` to keep nothing (a reference still works for its own
call).

**Coverage.** Every file GitHub lists is shown in the listing. A patch is shown whole, cut at 40,000
characters, or withheld with its reason: `omitted_by_github`,
`excluded_by_caller`, `over_budget`, `rename_only`, or `no_textual_diff` (binary
or empty; GitHub does not say which). `HARDLINE_GITHUB_MAX_CHARS` caps the whole
text: patches get what the listing leaves, and a listing too long to fit fails
the call. Coverage is also partial when GitHub's count of changed files is
missing or differs from the files it listed (its file list stops at 3,000).
`github_exclude` globs match case-sensitively, with `/` separators, on every
host.

The result's `github` object names the `pr`, its `snapshot_id`, head and base
SHAs, and `coverage`, with the first 50 `not_shown` files (and
`not_shown_total` past that). When `coverage` is `"partial"`, the reviewer was
told to limit its verdict to what it saw, and **the review must not be treated
as approval of the whole PR**. Hardline cannot enforce that on the caller. A
reviewer that closes its stdin before accepting all of the evidence fails the
call; `stdin_delivery: "unconfirmed"` means a process the reviewer started
still holds its stdin.

**The reviewer's isolation.**

| | Codex | Claude |
| --- | --- | --- |
| Tools | Read-only sandbox shell; no MCP servers or connectors; `web_search="disabled"`. | None (`--tools ""`). `github_tools="read"` grants Read/Grep/Glob under `--restricted`, confined to the required `workdir`. |
| Directory | `workdir`, or an empty temporary directory. | `workdir`, or the server's (no tools can read it). |
| Framing | `developer_instructions`. This replaces any configured value for the call. | `--append-system-prompt` (`--system-prompt` in advisory mode). |

The evidence sits between random-nonce markers, HTML comments are stripped from
the description, and the framing tells the reviewer it is third-party text, not
instructions. `github` is refused with `write=True`. A reviewer's reply can
still quote that text; treat it as untrusted before posting it anywhere. Job
requests record the reference, never the evidence; job results carry the
snapshot id and `delivery_hash`, the SHA-256 of the exact bytes piped.

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
