# Architecture — why hardline is shaped this way

The [README](../README.md) starts the user workflow;
[working notes](../CLAUDE.md) cover development. This page records the design
decisions behind the current [messaging and job contracts](messaging.md).

## The process model, which explains most of the rest

There is no daemon. Every agent session spawns its **own** hardline server over
stdio, so on a working machine ~20 of them run at once against a single SQLite
file, coordinating only through that file.

Two consequences that shape everything:

**A session is bound to its host.** The server's process, launcher ancestry,
and environment supply identity. Session records retain both the server and
launching host identities, so a surviving MCP child cannot keep a departed
host's lanes alive. Explicit declarations support hosts that cannot be inferred.

**It is an editable install, so several code revisions run at once.** A process
runs whatever the tree said when it *spawned* — forever. Nothing restarts the
fleet together. Any schema change is therefore a change to a store that older
code is still reading and writing, and no coordinated migration is possible.

Schema version 5 adds nullable `jobs.owner_key` and
`agent_sessions.host_pid`/`host_key`. Existing rows and older writers remain
compatible; readers account for missing tokens and host bindings. Version 6
adds `push_delivery`, which is also created on first use because a store
rebuilt by older code will not be re-initialized by a running newer process.
Any initializer overwrites `meta.schema_version` with its own constant, so
readers detect tables rather than trusting the number.

## Three tables, three questions

| Table | Answers | Written by |
| --- | --- | --- |
| `messages` | what was said | the sender |
| `jobs` | what work exists and how it ended | the dispatcher |
| `agent_sessions` | who exists and what they are called | each session |

They share one file deliberately. A job captures its database path at acceptance;
completion writes the result and its mailbox notice in one transaction.

Identity was the last of the three to get a durable record, and its absence
caused the failures the registry was built to fix — nothing could answer "who
can I address?", Codex sessions all shared one name, and mail sent to an exited
session became permanently unconsumable because only its holder may consume it.

## Lanes: the ownership model

A recipient may be bare (`codex`) or **lane-qualified** (`codex:construction`).
Bare names are shared; a lane-qualified message is consumable only by the
process holding that lane. That is the whole isolation mechanism, and every
other rule serves it.

**Ownership is over whole recipients, not suffixes.** Matching a suffix alone
let a session called `construction` consume `codex:construction` *and*
`claude:construction`. Nearly unreachable while lanes came from session ids,
which never collide — and the obvious case the moment sessions can name
themselves.

**A rename adds a name, it does not replace one.** An async result's recipient
is fixed when the job is *accepted*, so a session that renamed mid-flight
would find its own result addressed to a lane it no longer held. A session is
therefore *addressed* by its newest name and *consumes* mail for every name it
still holds. Explicit release gives a name back. Results go to the derived
lane instead where a relaunch derives it again (see the mitigations under
stable instance addresses below).

**Where identity comes from**, in order: an explicit `HARDLINE_AGENT_LABEL`
pin, then a session id the host supplied, then the process that spawned this
one. A runtime claim selects the current name. For hosts without a session ID,
one verified ancestry snapshot supplies the launcher identity and recognized
agent name together. Cached identities prevent reparenting from changing the
session's address or lifetime binding.

**One rule governs acquisition and consumption.** Registration reserves the
SQLite writer before checking holders and outstanding work. Consuming reads and
acknowledgements check the same durable grants inside their write transaction.
A local name alone grants no right to consume; contested lanes are refused.

## Derived, not stored

**Liveness.** A process that crashes cannot write "I died", so the only true
answer is to ask the OS at read time. `jobs.lost` and session liveness both work
this way. Nothing needs cleaning up: a vanished session simply stops being live.

**Three states, not two.** `ALIVE` / `DEAD` / `UNKNOWN`. A probe that cannot
answer is not a death — on Windows, opening a higher-integrity process returns
`ACCESS_DENIED`, indistinguishable from "no such process" unless the error code
is read. Reads are optimistic about `UNKNOWN`; deletion requires certainty.
Being wrong optimistically means reporting a destination that has gone. Being
wrong pessimistically means handing a live session's name to somebody else.

**A pid is not an identity.** Reused after exit, so new job owners and session
bindings pair it with a creation-time token where available. A verified own
token is retained across temporary probe failures. Legacy records without
tokens preserve their earlier liveness behavior; a bound host without a token
is unknown even if its PID exists.

## Durable completion and bounded work

The mailbox carries a compact completion reference; the job record holds the
full result. Committing both in one transaction prevents a result without its
notice or a notice without its result. Only the owning process instance may
finish the job, and a conditional transition prevents duplicate notices.
Cancellation keeps its terminal state even if a worker later returns a result.

A terminal write the store refuses is kept, not printed and dropped: the
worker's result exists only in its process until it lands, so that process
retries it on its own polls, job lookups and wake cycles (`server._settle_debts`,
every `channel.FULFIL_S` while a wake loop runs), one pass at a time. A refusal
ends the pass, sends that debt to the back of the line and rests before the
next, so a store still down costs one wait per rest and one failing debt
cannot hold up the rest. Repeats are safe because `finish` matches only a row
still owed a result. A job whose claim (`mark_running`) the store refuses
never runs its agent, since a cancel may have landed; it fails with the reason.
An unpaid result keeps its admission slot, so admission bounds debts too. A
clean exit retries what is still owed, past any rest, beginning no debt after
ten seconds (`server._settle_at_exit`); one begun still makes its writes, each
of which can wait on the store, so this bounds attempts, not the exit. It
runs from `atexit`, which an uninterrupted
interpreter shutdown reaches after `threading._shutdown()` has joined every
running dispatch (CPython 3.10 to 3.14), so a debt left at the last moment is
in it, though it then waits behind the longest running dispatch. A debt is
recorded before its traceback is printed, so a closed stderr cannot lose it.
The result is lost if its process is killed first (by any signal Python does
not handle, SIGTERM included) or the store stays shut through that try: the
job then reads `lost`, or stays `cancelled` without its result and notice if
a cancel got there first. It is also lost if the store loses the job.

The old console-script target `server.main` selects the command as
`cli.main` does, since pip does not regenerate an editable install's launcher
on a pull: one generated before `cli.py` ran every command as the server, so
`hardline-mcp report` exited 0 having done nothing. `python -m
hardline_mcp.server` still serves directly; through `cli` it would import the
module a second time.

Review record (gpt-6-astra), adopted: the exit deadline was checked only
after a pass, so it started writes past it and paid a whole pass of slow
writes; it is now checked before each write. `_owe` printed before recording,
so a closed stderr lost the debt. `python -m hardline_mcp.server` would have
run the module twice. The report's tokenless count missed a registration
whose host has no token (its lane is held as unknown) and counted one whose
host is dead (it holds nothing); it now applies ownership's own predicate,
and counts unfinished jobs' children too, whose cancel kills on the pid
alone. Rejected: a stderr warning when `server.main` is called, which a
direct caller would trigger as well. Implementation review (gpt-6-astra),
adopted: the exit deadline bounds debts begun, not writes (a never-started
job's debt makes two), so the docs say so; `children` counted a dead owner's
child, which a cancel finds lost and never kills; the module-entry test proved
neither serving nor one copy (it now completes an `initialize` and reads
`-X importtime`); the exit test ordered its debt by a sleep, now by a hook at
the start of threading's shutdown; a debt lost from a cancelled job leaves it
`cancelled`, not `lost`. Not fixed here: a queued job cancelled,
or one whose submission failed, frees its admission slot before its refused
write is kept, so those debts are outside the admission bound.

Admission bounds running plus queued work per server. Validation happens before
capacity is reserved; a full pool creates no job. A cancellable queue removes
cancelled callables and releases slots, so admission tracks work that can still
run. Receipts expose a durable state snapshot rather than waiting an arbitrary
interval to guess whether dispatch started. Durable records make interruption
visible; they do not promise replay after restart.

## Decisions, and what was rejected

**A label is a role, not an instance.** Mail sent to `codex:construction` is
consumable by whoever holds that name — including a session that claims it
*after* the message was sent. Rejected alternative: an ownership epoch in the
recipient, making each claim a distinct address. That would make mail to a name
nobody currently holds undeliverable by construction, which is the stranding
this exists to remove, and would make addressing a session that has not started
impossible. It is also what makes a lost claim recoverable after a reconnect.

**Runtime names must be reclaimed after reconnect.** Ownership is recorded
durably for a process instance, while the current runtime name is selected in
memory. A replacement MCP process may register an automatic or configured lane,
but it must explicitly reclaim a runtime role. A successful claim inherits that
role's backlog.

**A held name can be waited for, never taken.** A Claude Code conversation
moved to the background continues in a new process, while the old process
keeps its lanes until its window closes. No available signal distinguishes that
move from a copy that is still being read, so the name is not transferred.
`register_session(wait=true)` instead keeps the claim pending in memory until
the holder is dead, which is positive evidence, and then grants it through the
unchanged ownership rule. See [session continuity](session-continuity.md).

**Rename tables, never reshape them.** Inspecting a table and then dropping it
is two statements with no transaction between them, and under this deployment
another process can build the correct table in that window and have it
destroyed. The cost is a split-brain while old processes remain — closable only
from the new side, which reads both tables for ownership questions.

**Absence was made insufficient, not abandoned.** A design review argued that
absence-based ownership is the wrong foundation and that a stable instance
address should be separate from a display label. Not adopted. Instead a claim
now requires positive evidence that nobody is there — an unfinished job with a
live or unverifiable owner blocks a takeover, because its recipient was fixed
at acceptance before any message exists. Reusable roles remain a deliberate
tradeoff: a distinct instance address could provide stronger recipient identity,
but would need a separate policy for messages sent to an unattended role.

**Stable instance addresses: deferred, against stated criteria (2026-10-13).**
One name still serves as process, conversation and role, and no current name
is an immutable recipient: the derived lane is reproducible (project basename
plus eight characters of the session id, or a pin) and reclaimable once its
holder is gone. Roles deliberately let a new holder inherit backlog.

What a stable address would fix that nothing else does:
- A Codex or Hermes session's job completion is addressed to its latest role
  at dispatch. If that role is released and claimed by someone else, the new
  holder inherits the result while the requester lives on under another name.
  A Claude conversation's goes to its derived lane since the mitigation
  below.
- A claim re-acquires every retained name together, so one contested old name
  blocks an unrelated new one. Legacy writers can create that contest.
- A reply sent to a role reaches whoever holds it next.

What it would not fix: a move to the background is a fork with a new
conversation id. A parent id, if Claude Code ever exposed one, would prove
ancestry, not permission to hand over consumption.

Why not now:
- **Older processes can't read new addresses.** They consume by the recipient
  names they hold, so mail written to a new address form is invisible to them
  until they exit.
- **A conversation id is not one reader.** Two windows can share a transcript.
- **Mail that never transfers needs a rule for abandoned conversations.**
  Today 19 lanes hold unread mail with no holder.
- **Resolution must serialize with rebinding.** Alias resolution and insertion
  would have to serialize against rebinding, assigning unbound backlog exactly
  once.

Mitigations short of a migration, both built (2026-10-13):
- **Report inherited backlog on a grant.** `register_session` returns
  `inherited_unread`, and its note says how many unread messages were sent to
  the name before the claim. Review found the count could name the wrong
  lane: a claim re-acquires every retained name, and the result reported the
  last one rather than the one asked for, so re-claiming `red` after `blue`
  showed `red`'s count under `blue` (and gave `blue` to `drop_lane` on a
  local refusal). `sessions._acquire` now reports the lane asked for. A
  pending claim's grant has no caller to return to; its backlog arrives as
  mail and wakes the session.
- **Address new completions to the derived lane rather than the latest role,
  where a relaunch derives it again:** a `HARDLINE_AGENT_LABEL` pin or a
  Claude conversation id (`server._result_lane`). Every current reader holds
  that lane. A parent-derived lane (Codex, Hermes) ends with its launcher,
  while a claimed name can be claimed again after a restart, so there the
  claim stays the address. The derived lane is preferred only while this
  process holds it uncontested. I first argued that check was redundant,
  since a claim is granted only atomically with the derived lane. Review
  reproduced the counterexample: after a store rebuild another process can
  take the derived lane first, and the heartbeat, which registers lanes one
  by one, restores only the claim.

Revisit when any of these holds:
- a role must be handed over while jobs are outstanding;
- contested names or the claim cap block real claims;
- recorded provenance shows misdelivery (see "Who acked a message" below);
- every writer that predates `agent_sessions` has been retired, the
  precondition for any change of recipient format. Held since 2026-10-13
  (see "Retiring the legacy registry" below), so this decision is due for
  review; `TODO.md` dates it.

Review record (gpt-6-astra), adopted:
- **"The instance address already exists" was rejected.** A reproducible,
  reclaimable name is not one.
- **"Only the background move remains" was rejected.** It defined unresolved
  recipient identity away.
- **Two proposed triggers were dropped.** "Codex exposes its thread id" has
  already happened, through `_meta`. A fork parent id proves only ancestry.

**Retiring the legacy registry (2026-10-13).** Ownership read the pre-rename
`sessions` table beside `agent_sessions` (`_legacy_holders`), so a process on
older code that wrote only `sessions` was still seen as a holder. It no longer
does, on this evidence rather than on an empty table, which absence alone
would not prove:
- the table had no rows;
- every running hardline server (27, through an OS process listing, its
  launchers' children included) started on 2026-09-24 or later;
- the editable install runs whatever the working tree held at spawn, and every
  tree HEAD pointed at from before the oldest of them until now (67, from the
  complete reflog) creates `agent_sessions`. The same check with an earlier
  cutoff does find a tree without it.

Uncommitted edits are the one gap a reflog cannot see. A process that wrote
only `sessions` would now go unseen, and its lane could be claimed and its mail
split; old code cannot come back except through an old checkout. The table
stays, as every table here does, and `hardline-mcp report` shows any writer it
gains. Review (gpt-6-astra) held that a start date alone does not identify
loaded code, which is why the tree each process spawned from was checked.

**Who acked a message (2026-10-13).** The misdelivery trigger could not fire
while nothing recorded who read what. Each acknowledgement on current code now
records its process (pid and creation token) and the Claude conversation its
server was started for, in `message_acks`, inside the acknowledging
transaction, so an ack and its record commit or roll back together; `peek`
reports it as `acked_by`, reading both from one snapshot. Only an ack that
took effect is recorded. An acked message with no record was acked without
one (by an older revision): unknown, not a match.

That makes the documented failure checkable: a job's notice is meant for the
process that dispatched the job (`jobs.owner_pid`, `owner_key`; the notice body
names the job), so a notice acked by another instance is a notice consumed
elsewhere. Whether that was a relaunch of the same Claude conversation takes
interpretation: the job stores no conversation, only its requester lane, which
for a lane derived from a conversation carries the id's first eight
characters; the recorded conversation is the acker's, the one its server was
started under, and two windows can share it. A missing creation token proves
nothing about two equal pids. Ordinary mail has no recorded intent, so
a different acker there is a difference to interpret, not a verdict.

This records consumption, not readership. A consuming read commits before its
response reaches the caller, and `job_result`, `peek` and `history` show
content without acknowledging it.

`hardline-mcp report` reads both triggers' evidence from a store, read-only.
It counts acked job notices as `same_instance`, `other_instance` (with a
newest-first sample carrying both identities and timestamps) or `unknown`, and
lists the revision each registered server runs (`process_revisions`, keyed by
identity so a reused pid cannot inherit one) beside any live writer of the
legacy `sessions` table. Under `tokenless` it counts records that still act on
a pid without a creation token, judged as their consumers judge them:
registered processes whose server or host has none and is not shown dead,
unfinished jobs whose owner has none, and children of unfinished jobs whose
owner is not shown dead (a cancel kills such a tree unverified). Zero is that run's finding: a token that will not read makes
new ones. It reads every table in one `mode=ro` snapshot and
closes it before probing any process, so a slow probe holds no reader against
writers or checkpoints; it never initializes or prunes, and a missing column
makes its section `unavailable`. Its review (gpt-6-astra) rejected a
`revision` column on `agent_sessions`, which an older revision's update of a
reused pid's row would carry over, and reading through `mailbox._connect`,
which initializes the store it is asked about; it also reproduced a crash on a
notice body with a non-string job id and the held reader, both fixed.

First run on the real store (2026-10-13): three `other_instance` notices, all
results a conversation's earlier process dispatched on 2026-10-08 and the same
conversation, relaunched, acked today: a relaunch, not a misdelivery.

Review record (gpt-6-astra), on a larger first design that also snapshotted a
lane's holders at send time:
- **Holders at send are candidates, not the recipient,** and a job notice is
  inserted at completion while its recipient was fixed at dispatch, so a
  replacement holder would have looked correctly addressed. The job row already
  holds the dispatch intent; nothing is snapshotted at send.
- **A `conversation` column on `agent_sessions` was dropped.** A store an older
  revision rebuilds under a process that cached its initialization lacks added
  columns, so registration would fail; the existing host columns already do
  (#67). Acks go in a table created on use inside the acking transaction.
- **Not adopted: an explicit job-id column.** The notice body carries the job id.

**A model name says which tool serves it (2026-10-13).** A Codex session told
to ask Fable called `ask_codex(model="fable")`, got Codex's rejection, and
concluded Fable was unavailable: nothing it could read said Fable is a Claude
model. Now the tool descriptions name each side's models, `list_models` lists
them, and a name sent to the other agent's tool comes back with a
`model_hint` (see [models](configuration.md#models-effort-and-results)). The
call still goes where it was sent; a hint never refuses or substitutes.

Review record (gpt-6-astra), on a first design that hinted on failure only,
checked Codex's catalog to recognise a Codex name on `ask_claude`, and put
the catalog in `server_info`:
- **A failure is not a diagnosis.** A demoted answer, a timeout or a
  cancellation also reads `ok: false`, so the hint states what the name is,
  conditionally ("if you meant Claude"), and is attached whatever the answer.
  That also puts it on the async receipt, where a fast failure's hint would
  otherwise be lost.
- **A second review found async paths that dropped it** (an admission
  refusal, could-not-start, a cancellation before start), and `list_models`
  naming a family's newest model where dispatch runs a listed identifier as
  itself. Admission's every answer now carries it, `job_result` reads it from
  the job's request, and both lists use dispatch's rule (`adapters._runs_as`).
- **No process for a hint.** A catalog lookup after a failed Claude call would
  run outside the job's child tracking, extend the Claude dispatch lock under
  quota routing, read a different context than the call, and could replace the
  real failure with its own exception. The hint reads the name's shape alone,
  so a Codex model the shape cannot recognise gets no hint rather than a
  probe.
- **`server_info` starts no process.** Its executable report is tested to
  spawn nothing, and watcher setup waits on it; live discovery is its own
  tool.
- **Recognised, not available.** Claude Code has no command that lists
  models, so its documented aliases recognise names. Provider IDs of other
  shapes (Bedrock ARNs, prefixed IDs) get no hint either way; a letters-only
  deployment name sent to `ask_claude` gets the conditional one.

## Why read controls are explicit

Earlier Codex calls inherited the host sandbox unless an option selected one.
A recorded Windows probe in a trusted checkout wrote a file despite the tool's
read-only description. Default calls now explicitly pin read-only, through a
permission profile that also denies the reviewer the user's secrets (under
MXC on Windows; `--sandbox read-only` for the Windows sandboxes that cannot
deny; see
[configuration](configuration.md#what-a-codex-reviewer-can-read)).
The lesson is to select the execution boundary in the adapter instead of relying
on an operator's incidental configuration.

**Spawned agents get no MCP servers or app connectors.** Claude children run with `--strict-mcp-config`. Codex has no equivalent flag, and a default `ask_codex` child was found (#42) loading every server the user's Codex knows: hardline itself (more dispatch, mail as anyone, other sessions' history), `node_repl`'s browser, and web search through the `apps` connector. It called them unattended, outside its sandbox.

Every Codex child, advisory included, now runs with two things:
- **The connector features off:** `-c features.apps=false -c features.plugins=false`.
- **Every remaining server disabled:** `codex mcp list` is asked, with those features off and in the child's own environment and directory, which servers any configuration layer still enables. They are all disabled in one override, `-c mcp_servers={"<name>"={enabled=false},…}`, which deep-merges into each definition; quoted keys address any name.

Why not the obvious alternatives, all measured:
- `-c mcp_servers={}` merges and removes nothing.
- `--ignore-user-config` drops the configured default model and still loads system, cloud and project layers.
- `--disable <unknown>` is a hard error, while an unknown `features.<name>` is silently ignored. That's why `apps` and `plugins` are also checked against `codex features list`.

**Unprovable isolation refuses the call.** A failed or malformed listing, a missing or `removed` feature, or a server name TOML can't express stops the child from being started.
- **Where the probes run:** both run against the same executable that will be launched, in the child's own environment and directory, under the call's spawn claim, so a cancel reaches them.
- **Nothing is cached,** since the binary behind a name can change.

**The guarantee covers servers configured when the call starts.** Codex reloads configuration at launch and has no execution-time "no MCP" switch. A server added in the sub-second gap between the listing and the launch is therefore not disabled.

Codex's built-in web search stays available to default calls, which carry the orchestrator's own prompts.

An earlier Claude probe requested `echo x > probe.txt` with host settings that
allowed all Bash commands:

| Controls in that probe | File written |
| --- | --- |
| Deny Edit/Write/NotebookEdit | Yes |
| Also use `--strict-mcp-config` | Yes |
| Also discard settings with `--setting-sources ""` | No |

These are historical measurements, not universal containment guarantees.
Discarding settings restores Claude's command classifier, which can still
permit indirect writes. The [execution guide](configuration.md#execution-modes-and-write-access)
states the current boundaries. Hardline also strips its write-enable variable
from spawned children so inherited environment does not enable nested writes.

## What this cannot do

**Wake requires a host connection.** `watch.py` observes an exact unread scope
through fresh read-only SQLite snapshots. Claude's Monitor consumes its JSON
notices; `wake_codex.py` supplies the same observation as tool output to an
explicitly bound app-server thread. Both leave consumption to the existing
`inbox` tool. Codex waits while the thread is busy, and neither adapter creates
a replacement session. An already-open client without an inbound connection
still needs host integration. `deliver=true` continues to spawn a fresh
one-shot CLI. See [inbox setup](inbox-signals.md) and
[the watch design](hardline-watch-design_2026-09-09.md) for integration boundaries.

**Identity is self-declared and unenforced.** Every process runs as the same
user on one machine, so there is nothing to defend against that an attacker
could not do directly. The guards here prevent *confusion*, not intrusion.

**A record without a creation token cannot detect PID reuse.** Every platform
reads one now (`GetProcessTimes`, `/proc`, `proc_pidinfo` on macOS, which read
none before #69), but records written without one, and processes whose token
will not read, keep PID-only behavior; a bound host without a token stays
unknown.
