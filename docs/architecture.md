# Architecture — why hardline is shaped this way

The README says how to use it. `CLAUDE.md` says how to work on it. This says
**why**, and records what was rejected, because a decision whose alternatives
are invisible gets re-litigated by the next person.

Written against `0.6.2` / `fa85ed6`.

## The process model, which explains most of the rest

There is no daemon. Every agent session spawns its **own** hardline server over
stdio, so on a working machine ~20 of them run at once against a single SQLite
file, coordinating only through that file.

Two consequences that shape everything:

**The process *is* the session.** Nothing has to be declared. A server's own
pid, its parent, and its environment are the session's identity, which is why
identity is read rather than configured.

**It is an editable install, so several code revisions run at once.** A process
runs whatever the tree said when it *spawned* — forever. Nothing restarts the
fleet together. Any schema change is therefore a change to a store that older
code is still reading and writing, and no coordinated migration is possible.

## Three tables, three questions

| Table | Answers | Written by |
| --- | --- | --- |
| `messages` | what was said | the sender |
| `jobs` | what work exists and how it ended | the dispatcher |
| `agent_sessions` | who exists and what they are called | each session |

They share one file deliberately: a dispatch and its delivery must not be able
to land in two different databases.

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
is fixed when the job is *dispatched*, so a session that renamed mid-flight
would find its own result addressed to a lane it no longer held. A session is
therefore *addressed* by its newest name and *consumes* mail for every name it
has ever held.

**Where identity comes from**, in order: an explicit `HARDLINE_AGENT_LABEL`
pin, then a session id the host supplied, then the process that spawned this
one. Claude Code passes a session id. Codex passes 22 environment variables and
not one identifies the session — so for Codex both the lane and the agent are
read from the parent process, which needs no cooperation from the agent at all.

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

**A pid is not an identity.** Reused after exit, so every durable record pairs
it with a creation-time token and compares before acting.

## Decisions, and what was rejected

**A label is a role, not an instance.** Mail sent to `codex:construction` is
consumable by whoever holds that name — including a session that claims it
*after* the message was sent. Rejected alternative: an ownership epoch in the
recipient, making each claim a distinct address. That would make mail to a name
nobody currently holds undeliverable by construction, which is the stranding
this exists to remove, and would make addressing a session that has not started
impossible. It is also what makes a lost claim recoverable after a reconnect.

**A claim does not survive its process.** Runtime claims live only in memory, so
a reconnect returns a session to anonymity. Persisting one would require
deciding a *new* process is the same session as a dead one, which nothing here
can know. The decision above is what makes this recoverable rather than fatal.

**Rename tables, never reshape them.** Inspecting a table and then dropping it
is two statements with no transaction between them, and under this deployment
another process can build the correct table in that window and have it
destroyed. The cost is a split-brain while old processes remain — closable only
from the new side, which reads both tables for ownership questions.

**Absence was made insufficient, not abandoned.** A design review argued that
absence-based ownership is the wrong foundation and that a stable instance
address should be separate from a display label. Not adopted. Instead a claim
now requires positive evidence that nobody is there — an unfinished job with a
live owner blocks a takeover, because its recipient was fixed at dispatch before
any message exists. **That is a hardening, not an answer**, and the argument
still stands. See `TODO.md`.

## What this cannot do

**Wake requires a host connection.** `watch.py` observes an exact unread scope
through fresh read-only SQLite snapshots. Claude's Monitor consumes its JSON
notices; `wake_codex.py` supplies the same observation as tool output to an
explicitly bound app-server thread. Both leave consumption to the existing
`inbox` tool. Codex waits while the thread is busy, and neither adapter creates
a replacement session. An already-open client without an inbound connection
still needs host integration. `deliver=true` continues to spawn a fresh
one-shot CLI. See `hardline-watch-design_2026-09-09.md` for the contract and
measured integration boundaries.

**Identity is self-declared and unenforced.** Every process runs as the same
user on one machine, so there is nothing to defend against that an attacker
could not do directly. The guards here prevent *confusion*, not intrusion.

**macOS has no process identity.** No `/proc`, and `proc_pidinfo` is not wired
up, so pid reuse is undetectable there.
