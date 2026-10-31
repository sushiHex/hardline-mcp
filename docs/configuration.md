# Configuration

[Start here](../README.md) · [Messaging and jobs](messaging.md) · [Inbox signals](inbox-signals.md)

Set environment variables on the **MCP server registration**. Reconnect after
changing its environment or updating the editable source; an already-running
server retains its loaded code. `server_info()` reports the code revision,
package version, module path, database path, limits, write gate, job counts,
watcher command, and the agent executables a spawn would run. Check
`code_revision` when confirming an update is loaded: it is the revision the
process spawned on, and `checkout_revision` is what the checkout says now.
When the branch the process spawned on has moved, `revision_note` says to
reconnect. A checkout switched to another branch, or detached, gets no note: a
reconnect then would load unfinished work. `list_agents().you` carries
the same note, and `inbox` gives it once for each new checkout revision, so a
session learns that newer code is waiting without being asked.

## Mailbox and CLI paths

| Variable | Default / purpose |
| --- | --- |
| `HARDLINE_DB` | `~/.cache/hardline-mcp/mailbox.db`; clients must use the same file to exchange messages. |
| `HARDLINE_CLAUDE_CMD` | Override the Claude executable path. |
| `HARDLINE_CODEX_CMD` | Override the Codex executable path. |
| `HARDLINE_HERMES_CMD` | Override the Hermes executable path. |
| `HARDLINE_CODEX_WINDOWS_SANDBOX` | Windows only. `mxc` (default), `elevated`, `unelevated`, or `inherit` (pass nothing; Codex's own configuration decides, which in advisory mode excludes the host's `config.toml`). See [the Windows sandbox](#codex-windows-sandbox). |
| `HARDLINE_AGENT` | Declare `claude`, `codex`, or `hermes` when needed. |
| `HARDLINE_AGENT_LABEL` | Select a fixed session role; see [ownership and reconnects](messaging.md#name-a-session). |
| `HARDLINE_CODEX_STEER` | `0` queues every Codex notice instead of steering it into a busy turn first. Steering also needs the `codex-watch` extra. See [queue-wake](inbox-signals.md#codex-queue-wake). |
| `CODEX_HOME` | Not a hardline variable, but read by the `codex queue` and `codex app-server proxy` it runs to wake a Codex session. Codex does not pass it to MCP servers, so if Codex uses a custom home, set the same value in hardline's MCP registration env. See [queue-wake](inbox-signals.md#codex-queue-wake). |

Executable overrides are paths, without arguments: Hardline appends `-p` for
Claude, `exec` for Codex, or `chat -Q -q` for Hermes. For example,
`HARDLINE_HERMES_CMD=C:/Users/you/AppData/Local/hermes/hermes-agent/venv/Scripts/hermes.exe`.

Explicit overrides take precedence. Codex then uses the executable on `PATH`,
falling back to legacy Windows install discovery only when it is absent. This
lets the current CLI's maintained launcher take precedence over older bundled
binaries. Claude and Hermes use their bare commands on `PATH` without discovery.
When pinning Codex, prefer its maintained launcher over a versioned release path.

### Codex Windows sandbox

On Windows a spawned Codex runs in Codex's native MXC sandbox
(`-c windows.sandbox="mxc"`) unless `HARDLINE_CODEX_WINDOWS_SANDBOX` says
otherwise. MXC enforces a permission profile inside the one process it starts,
so hardline pins read-only (workspace-write for `write=True`) through a
profile that also keeps the reviewer out of your secrets
([below](#what-a-codex-reviewer-can-read)). Where Windows cannot provide MXC,
`codex exec` starts none of the reviewer's commands and records no failed
command either, so the call's preflight refuses it. Choose `elevated` on such
a machine.

Codex 0.164.0 cannot load AGENTS.md under such a profile on Windows: it reads
the files through a helper whose MXC container gets no `SYSTEMROOT`, and every
thread start fails (openai/codex#56264). A call refused exactly that way, with
no thread started, runs once more with `project_doc_max_bytes=0`. Its prompt
is prefixed with a constant instruction to read the AGENTS.md files that would
have applied, which the reviewer's shell can still read. Its result carries an
`instructions_note` saying so. Each process remembers the refusing binary for
an hour, or until its file changes.

`elevated`, and `unelevated` (upstream's fallback, whose isolation is weaker,
especially for the network), are pinned with `--sandbox` and deny nothing.
Reconnect the MCP server after changing the variable.

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

### What a Codex reviewer can read

Codex's read-only sandbox reads your whole home: on Windows its setup grants
the sandbox's users read access across the profile, and on Linux and macOS
`:read-only` reads the whole filesystem. A reviewer could therefore read
Claude Code's credentials and transcripts, Codex's own sessions, SSH keys and
hardline's mailbox, and whatever a command reads can reach the model.

So hardline pins every Codex it spawns through a permission profile that
denies these. Codex enforces the profile through bubblewrap on Linux,
Seatbelt on macOS, and MXC (the default) on Windows. No command of a
hardline-spawned Codex can read:
- Claude Code's homes (`~/.claude`, and `CLAUDE_CONFIG_DIR` when set) and
  `~/.claude.json`: credentials, transcripts, settings;
- Codex's home: `~/.codex`, `CODEX_HOME` when set, and advisory mode's
  temporary copy. Every entry is denied except `AGENTS.md`, `skills`, the
  sandbox's helper binaries (`.sandbox-bin`) and scratch space (`tmp`, `.tmp`,
  where the shell's `apply_patch` alias lives). Besides `auth.json` and
  transcripts, the home holds logs, dictation, memories and state databases.
  The elevated sandbox also logs command lines under `.sandbox`. These names
  change between releases, so entries are listed at each call, and a home
  that cannot be listed refuses the call. Codex's provisioning `.guard` file
  is also left out, since MXC cannot start over a denial of a file another
  process holds open exclusively (reported on openai/codex#52042);
- gh's configuration, where its token may live: `GH_CONFIG_DIR`,
  `$XDG_CONFIG_HOME/gh`, `%APPDATA%\GitHub CLI` and `~/.config/gh`, each that
  applies;
- hardline's store: `~/.cache/hardline-mcp`, and `HARDLINE_DB` with its
  journal files wherever it points;
- `~/.ssh`, so no git-over-SSH inside a reviewer.

Each path is denied under its own spelling and its resolved one, so a
junction or symlink does not open a second way in. A path under a missing
directory is denied as the topmost missing directory, since MXC refuses to
start over a denial whose parent does not exist (openai/codex#53576).

The denial is per call. The sandbox holds it inside the reviewer's process
and changes no file's permissions, so your own Codex sessions, full-access ones
included, are untouched. A workdir inside a denied directory stays readable
(writable with `write=True`): Codex's most specific entry wins, so a reviewer
of `~/.claude/skills` reads its workdir and nothing else under `~/.claude`.
On Linux, bubblewrap leaves a read-only workdir there unreadable, so such a
call is refused. A denied directory itself stays denied as a workdir.

**Nor does a command inherit the usual credentials.** Every spawned Codex,
under every sandbox choice, runs with
`-c shell_environment_policy.ignore_default_excludes=false`. That turns on
Codex's own default excludes, which Codex leaves off: every variable whose
name contains `KEY`, `SECRET` or `TOKEN`, in any case, is dropped from each
command's environment, and from the exports Codex captures from your shell's
rc files. That covers `GH_TOKEN`, `GITHUB_TOKEN`, `OPENAI_API_KEY` and
`AWS_SECRET_ACCESS_KEY`. The Codex process keeps them, so its own login works.
- **Other processes' environments are out of reach on Linux and Windows, not
  macOS** (all measured). On Linux, bubblewrap gives commands their own pid
  namespace and `/proc`, so `/proc/*/environ` shows nothing of Codex's or of
  your other processes'. It skips the fresh `/proc` where the mount fails.
  Under MXC a command sees only its own job's processes. On macOS a command
  can read the starting environment of any process you own through
  `sysctl(KERN_PROCARGS2)`, what `ps -E` uses: Codex's, hardline's, your
  shell's. So there a credential in your environment is not kept from a
  reviewer. Starting Codex without them would not help, since your other
  processes hold the same ones.
- **It goes by name.** `TOKENIZERS_PARALLELISM` goes too, and a command that
  needs a token, such as an `npm install` with `NPM_TOKEN` under `write=True`,
  runs without it.
- **It's one scalar on purpose.** Keyed `filters` would discard a legacy
  `exclude` or `include_only` of yours, and legacy arrays replace wholesale.

**The preflight checks the policy that holds, not the one passed.** Before
every call, hardline's own Python runs once through `codex sandbox` under the
call's exact sandbox and pin, in its workdir (about half a second). Managed
requirements, local and cloud, are loaded as `codex exec` loads them. The
probe must list the workdir, and no denied path may open; opening reads
nothing. One of those paths is a canary that hardline has just written under
its own denied cache, so a policy that denies nothing cannot pass for want of
targets. Nor may the probe inherit any credential-named variable in Codex's
environment, named in its arguments since the policy under test filters its
environment. A canary that hardline adds is always among them. Otherwise the
call is refused. That catches each of these:
- Windows without MXC, where `codex exec` would start no command and report
  none failed;
- managed requirements that reject hardline's profile and swap in one
  without the denials;
- a legacy managed configuration (`managed_config.toml` or MDM), which
  outranks `-c`, turning the default excludes back off;
- the Linux workdir above;

each of which would otherwise read as a finished review. The preflight and
the call are separate processes, so the check is of the configuration as it
stood half a second earlier. Under the elevated, unelevated and `inherit`
choices there is no preflight, so nothing checks that the excludes hold.
`tests/test_live_sandbox.py` checks all of this with dummy secrets only. It
runs in CI on Linux and macOS against Codex 0.163.0, and was run on a Windows
11 host against 0.162.1, since GitHub's Windows runners have no native MXC. Real `ask_codex`
runs, read-only and `write=True`, were also checked on that host, including
one whose command saw an ordinary variable but not a dummy `*_TOKEN`.

What the denial does not cover:
- **Credentials under other names, or put back.** A variable without `KEY`,
  `SECRET` or `TOKEN` in its name still reaches the reviewer, such as
  `PGPASSWORD` or a `DATABASE_URL` with a password in it. So does anything a
  `shell_environment_policy.set` of yours or your administrator's adds, since
  it is applied after the excludes. A shell's startup script can set one
  again, such as a PowerShell profile, which Codex's login shells run. Hooks
  in your Codex configuration run with Codex's whole environment. On macOS,
  any process's starting environment is readable (above).
- **Credentials in files outside the list.** `~/.npmrc`, `~/.netrc`,
  `~/.aws` and rc files that export a token stay readable.
- **Paths that appear mid-call.** A secret created after the call starts may
  be readable: bubblewrap masks a missing path only under a writable root.
- **The workdir and writable roots inside a denied directory.** The workdir,
  and `TMPDIR` under `write=True`, are opened again there with everything
  beneath them, because the most specific entry wins.
- **Content Codex itself loads.** Codex reads its skills, rules and
  instructions outside the sandbox. The denial binds the reviewer's commands,
  not the Codex host.

A deny profile routes Codex's own file operations (AGENTS.md, apply_patch)
through a sandboxed helper process whose answer Codex awaits with no timeout
(openai/codex#53537). One run in ten hung that way once, on 0.162.0. Until
upstream bounds it, `HARDLINE_CODEX_TIMEOUT_S` is what ends such a call.

The profile gets a new random name on every call. Codex deep-merges its
configuration layers, so a `[permissions.<name>]` table in any of them - a
trusted project's `.codex/config.toml` included - would merge into a profile
of a known name and could grant back what it denies.

**The elevated and unelevated sandboxes deny nothing.** They deny through ACEs
on a sandbox identity that every Codex session shares, and the next session to
start, your own included, reconciles those ACEs to its own list. A per-call
denial there would last only until then.

Codex's machine-wide requirements file (`[permissions.filesystem] deny_read`
in `%ProgramData%\OpenAI\Codex\requirements.toml`) denies a list to every
Codex sandbox, but **it also forbids full access**, since an unsandboxed
session could not honour the denials. Once it denies anything, a session
asking for `danger-full-access` falls back to read-only with a startup
warning, and with approvals off (`--dangerously-bypass-approvals-and-sandbox`)
Codex refuses to start. Under the elevated sandbox it also creates any missing
listed path as a directory, so it can only hold directories.

### Codex compatibility errors

If Codex says a model needs a newer client, ask the serving process which
executable it runs. `server_info().agent_commands` reports, per agent, the
program name dispatch would hand the OS next and the rule that chose it,
resolved fresh from that process's environment by dispatch's own resolver:

```json
"agent_commands": {
  "codex": {"executable": "C:/.../Codex/bin/3f9a.../codex.exe", "source": "legacy_discovery"}
}
```

`source` is `override` (`HARDLINE_CODEX_CMD`), `path` (found on the server's
`PATH`), `legacy_discovery` (the most recently modified binary under Codex's
hash-named Windows install directories), or `bare` (neither found one; the
name alone). A bare or relative name is resolved by the OS at spawn time, and
Windows searches the application, current and system directories before
`PATH`. A resolution that fails reads `{"error": ...}` for that agent alone.
A job resolves when it starts, and quota routing can send an `ask_claude` call
to Codex.

`legacy_discovery`, as above, means no `codex` on the server's `PATH`, so a
desktop bundle runs, and it may be an old one. Run the reported executable
with `--version`; updating a different Codex installation will not change the
selection. Reconnect the MCP server after a Hardline update; restart its host
if it inherited an old `PATH`.

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
| `HARDLINE_CLAUDE_TIMEOUT_S` | `14400`, or `900` for a bare call | Claude subprocess ceiling in seconds. |
| `HARDLINE_CODEX_TIMEOUT_S` | `14400` | Codex subprocess ceiling in seconds. |
| `HARDLINE_CLAUDE_IDLE_S`, `HARDLINE_CODEX_IDLE_S` | `3600` | Seconds with no output after which a streaming run is stopped as wedged. Above the ceiling, the ceiling alone applies. |
| `HARDLINE_ASYNC_MAX_WORKERS` | `4` | Concurrent background workers per MCP server. |
| `HARDLINE_ASYNC_MAX_PENDING` | Four times the worker count | Accepted jobs, running plus queued, per server. |
| `HARDLINE_GITHUB_TIMEOUT_S` | `60` | One deadline for every `gh` call that collects a pull request. |
| `HARDLINE_GITHUB_MAX_CHARS` | `400000` | Evidence piped to one reviewer; files past it are listed as `over_budget`. |
| `HARDLINE_GITHUB_SNAPSHOT_MAX_BYTES` | `4000000` | Largest snapshot collected; over it, collection fails and names the largest patches. |
| `HARDLINE_GITHUB_SNAPSHOT_DIR` | `~/.cache/hardline-mcp/github` | Snapshot store. An empty value disables storage. |

**A run that streams is judged by silence, not age.** Every `ask_codex` call
and every `ask_claude` call with options writes its progress as it works
(Codex `--json`, Claude `stream-json`). Such a run goes on while it writes, up
to the ceiling, and is stopped once it has written nothing on either stream for
the idle limit; the result then has `timeout_kind: "idle"` and keeps what was
written. A bare `ask_claude(prompt=...)` prints only at the end, so it has no
idle limit and keeps the 900-second ceiling. A fixed 900 seconds had killed a
healthy review that had written a megabyte of events, while a wedged run sat
out its whole budget.

A run whose turn had already completed before a limit stopped it returns its answer (`ok: true`), with `process_exit` saying the process had to be stopped. On 2026-10-16 a Codex review emitted `turn.completed`, then never exited, and was stopped an hour later.

Silence is a policy, not proof of a hang. Codex's `--json` stream carries
completed items, not reasoning or command-output deltas, and Claude's carries
whole messages, so a long tool call or a long stretch of reasoning writes
nothing. No recorded run bounds a healthy silence, hence the hour. The limit
holds over a grandchild left holding the output after its launcher exits. One
the kill cannot reach (on Windows, an orphan of an exited launcher) is left
running, and the result says `capture_incomplete: true`.

These values must be positive integers. Worker limits are read at startup;
invalid timeout values fail the call before spawning an agent. Hermes uses a
fixed 180-second budget. Queued dispatches are dropped at a clean shutdown and
recorded `failed` ("server exited before the job started"), with their notice;
calls already in flight are awaited. On Windows a command line over 32,767
UTF-16 units is refused before spawning, with its length in the error. See [job receipts and cancellation](messaging.md#track-background-work)
for admission and recovery behavior.

**A client's own deadline is separate.** A sync `ask_claude` or `ask_codex`
blocks the client's tool call until the agent finishes (#89). A client that
cancels the call stops the agent: Claude Code sends `notifications/cancelled`
when its timeout fires, and the recorded worker is killed. While it waits,
hardline sends a progress notification every minute, which keeps Claude Code's
idle cutoff (30 minutes for a stdio server) from ending a long call. A client
that only stops waiting, as Codex does, leaves the agent running and its reply
is lost. Codex's MCP client gives up after 300 s by default, so give hardline's
entry a longer deadline for ordinary sync calls, and use the `_async` tools for
anything long: a streaming sync call whose Codex caller has stopped waiting
runs on until it finishes or falls silent, unheard.

```toml
[mcp_servers.hardline]
tool_timeout_sec = 1000   # past a bare ask_claude's 900 s ceiling
```

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
unchanged; use an identifier supported by the selected CLI. Claude models
(`fable`, `opus`, `sonnet`, `haiku`, `claude-fable-5-1`) go to `ask_claude`,
and ChatGPT models (`astra`, `luna`, `sol`, `terra`, `gpt-6-astra`) to
`ask_codex`.

`list_models()` says which tool takes which names. Under `claude` are Claude
Code's documented aliases, since it has no command that lists models; full
IDs pass through as well. Under `codex` is Codex's live catalog, read as
`ask_codex` would read it with the same `mode` and `workdir`: `families` maps
each family name to the model a call naming it runs (itself when the catalog
lists the name as an identifier; `null` when none is uniquely newest), and
`models` lists every selectable identifier. A request `ask_codex` would refuse,
or a lookup that fails, reads `{"error": ...}` without hiding the Claude half.
Neither half proves a model is available to the account.

A name sent to the other agent's tool still goes where it was sent, since a
custom Codex provider may serve a `claude-` model and a Claude provider may
deploy under any name. The answer carries a `model_hint` naming the right
tool: for a Claude alias or `claude-` ID sent to `ask_codex`, or a
letters-only or `<prefix>-<generation>-<family>` name that is not a Claude
alias sent to `ask_claude`. The hint comes from the name alone, starts no
process, and is attached whatever the answer. On async calls it is on the
receipt or refusal, and on `job_status` and `job_result`, both read from the
job's request, so a job lost or cancelled without a result shows it too.

For Codex, a letters-only family name such as `astra` or `sol` resolves to the
newest current model in that family, read from `codex debug models` on the
selected executable: with `gpt-5.6-sol`, `gpt-6-sol` and `gpt-6.1-sol` all
listed, `sol` becomes `gpt-6.1-sol`. Only identifiers shaped
`<prefix>-<generation>-<family>` qualify, so variants such as `-mini` and dated
snapshots never stand in for the family. Hidden and retiring models (any
`upgrade`) are skipped, and generations compare numerically. Advisory calls read
the catalog from an isolated home prepared the same way as the one they run in.

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
| Default (`write=False`) | Explicit read-only sandbox, as a permission profile that also denies secrets (under MXC on Windows); ephemeral session. | Denies Edit/Write/NotebookEdit and discards user settings; Bash remains available. |
| `mode="advisory"` | Fresh neutral workspace and temporary config/auth home; read-only sandbox and fixed developer instructions. | Fresh neutral workspace; tools, slash commands, project customizations, and persistence disabled. |
| `write=True` | `workspace-write` sandbox with approvals disabled. | Full tool access with `bypassPermissions`. |
| `context="clean"` | Default or `write=True` in `workdir`, without the user's Codex home; see [Clean context](#clean-context-codex). | Not available. |

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

### Clean context (Codex)

`context="clean"` on `ask_codex` and `ask_codex_async` runs Codex without the
user's Codex home, in `workdir`, and composes with `write=True`. It is for a
builder that must start from declared files only, such as a construction
trial run on a frozen copy:

```text
ask_codex(prompt="...", workdir="/abs/frozen-copy", write=True,
          context="clean", model="gpt-6-astra", effort="high")
```

**What it removes:**
- Codex runs from a temporary `CODEX_HOME` holding only `auth.json`, so it
  does not load the user's config, `AGENTS.md`, skills or memories from their
  Codex home. A skill linked from a directory it still searches, such as
  `~/.agents/skills`, is the exception (see below). ChatGPT auth is required,
  and checked when the call is admitted.
- User config is ignored (`--ignore-user-config`), and so are user and project
  execution rules (`--ignore-rules`). API-provider overrides are stripped.
- `project_root_markers` is pinned to `.git`, so no surviving configuration
  layer can move the project root above `workdir`.
- The user's Codex homes (`~/.codex`, and `CODEX_HOME` if it is set elsewhere)
  and `~/.agents` are unreadable to Codex's commands, the `AGENTS.md` and
  `skills` that other calls leave readable included. A home relocated anywhere
  else is not known to hardline.
- Instructions come from `workdir` alone. If Codex 0.164 cannot load them
  under the sandbox, the retry names that one file instead of pointing at the
  Codex home.

**It refuses** rather than run:
- in advisory mode;
- without `workdir`;
- when `workdir` is not a repository root with a `.git` directory of its own,
  meaning exactly `.git`, not a link, and containing a `HEAD`, since
  instructions and project settings above it would otherwise apply;
- under a Windows sandbox that denies no reads (`elevated`, `unelevated`,
  `inherit`);
- inside a Codex home or `~/.agents`, or in a `workdir` that contains one;
- when `AGENTS.md` or `AGENTS.override.md` is a link leading outside
  `workdir`.

It is Codex only. Claude clean writes wait until removing memory and keeping
hooks are shown together.

**It does not remove, and says so** in every result (`context.not_removed`):
- system and managed (cloud) configuration;
- the frozen copy's own `.codex/` configuration, as Codex's trust rules
  decide;
- skills Codex discovers outside its home, such as `~/.agents/skills` or admin
  and bundled skills. Codex follows their links, including links into a Codex
  home. Of these, only `~/.agents` is denied, and only to Codex's commands;
- the environment;
- web search;
- writes a workspace-write sandbox allows outside `workdir`.

It is not filesystem confinement.

**The result's `context`** keeps four things apart:
- `configured`: what hardline asked for. Its `instructions` field is one of:
  - `automatic_loading_requested`: Codex's own rules decide what it loads.
  - `shell_read_requested`: Codex could not load the expected file, so the
    model was asked to read it.
  - `none_requested`: Codex could not load instructions and there was no
    file to point it at.
- `discovered`: the instruction files found in `workdir` with their sha256,
  and the one hardline expects Codex to load.
- `observed`: only whether a Codex thread started, read from Codex's event
  stream even when the run then failed or timed out, with the reason nothing
  more can be shown. Codex reports no instruction loading, a file found is not
  a file loaded, and a started thread is not a file read.
- `workdir_digest`: taken first, before the call creates its home or runs its
  sandbox and isolation checks, so it describes what the builder started
  from. For an async job that is when the job runs, not when it was admitted.

A call refused before Codex starts has no `context`: nothing ran.

The digest (`hardline-tree-v1`) is the sha256 of one sorted record per entry:
- files by the hash of their raw bytes, so line endings count;
- directories, empty ones included;
- links and junctions by their target, never followed;
- ignored files included;
- the root's `.git` left out.

An entry that is none of these refuses the call. On Windows, every reparse
point counts as a link, so a copy holding cloud placeholders (OneDrive) or
other reparse points that are not links is refused. Make the frozen copy
outside a synced folder.

Equal digests are not equal builder inputs. The digest records no permission
bits, so `chmod +x` does not change it. It also leaves out `.git`, and with it
the repository's config and hooks.

The digest fingerprints one walk; it does not freeze the tree. A tree that
changes while it is hashed goes undetected, so the copy must hold still until
the builder starts. Large trees, ignored dependency folders included, are
hashed in full with no time limit, and cancelling an async job does not stop
a hash already under way.

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
