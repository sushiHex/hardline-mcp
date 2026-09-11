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
compatible; readers account for missing tokens and host bindings.

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
still holds. Explicit release gives a name back.

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

## Why read controls are explicit

Earlier Codex calls inherited the host sandbox unless an option selected one.
A recorded Windows probe in a trusted checkout wrote a file despite the tool's
read-only description. Default calls now explicitly pass `--sandbox read-only`.
The lesson is to select the execution boundary in the adapter instead of relying
on an operator's incidental configuration.

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

**Creation tokens are unavailable on macOS.** The current probe does not use
`proc_pidinfo`. Legacy PID-only records cannot detect PID reuse; new bound hosts
remain unknown without a token, and instance-bound watchers cannot be armed.
