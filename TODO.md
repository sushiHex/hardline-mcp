# TODO

The short list. Collaborative detail lives in
[GitHub issues](https://github.com/sushiHex/hardline-mcp/issues); everything
below is known and deliberate.

## Open design question

**Stable instance addresses.** Ownership rests on reclaimable names, with
absence made insufficient rather than replaced: a claim needs evidence. A
2026-10-13 design pass deferred a separate instance address against stated
criteria and recorded when to revisit; see "Stable instance addresses" under
[Decisions](docs/architecture.md#decisions-and-what-was-rejected).

**Review it on or after 2026-10-27, with `hardline-mcp report`.** By then most
servers run code that records revisions and acks. Against the triggers:
- *Misdelivery.* Read `job_notices.other_instance_sample`. An entry is a
  relaunch when the acker's `conversation` is the one the requester lane was
  derived from (its suffix carries the id's first eight characters) or one
  that claimed that lane; anything else is a candidate misdelivery to trace.
  Baseline 2026-10-13: 3 of 128 acked notices `other_instance`, all a relaunch
  of the dispatching conversation; 0 `same_instance`, 125 `unknown` (acked
  before acks were recorded).
- *Legacy writers retired.* Holds since 2026-10-13: a precondition for a
  recipient-format change, not a reason for one.
- *Handover with jobs outstanding, contested names or the claim cap blocking
  real claims.* From the sessions in use; `unread_mail` lanes whose `holder`
  is `contested` are contested names with mail waiting. Baseline 2026-10-15:
  75 unread, all on lanes with no registration, none contested.

Record the outcome in `docs/architecture.md` and, if deferring again, a new
date here.

## Known limits, each a deliberate trade

**A record without a creation token is honoured on pid liveness alone.** Every
platform reads one now (macOS since #69), but rows written before that, or
whose token would not read, let a reused pid pass for its process: they can
keep a lane held or a job looking owned, which refuses rather than steals. They
age out with the processes that wrote them; `hardline-mcp report` counts the
ones still in force under `tokenless`, with unfinished jobs' children, whose
cancel kills on the pid alone.

**A cancelled or unsubmittable job's refused write is outside the admission
bound.** Its slot is freed before the write is kept as a debt (#72's bound
covers only a finished job's result). Needs a store refusing those writes
repeatedly to matter.

## Shipped since this list was written

- Session-to-session mail watched working on the real store, Claude to Codex
  and Claude to Claude (see "Not yet verified live" in `CLAUDE.md`).
- The mutation harness and an isolated test store (#25): `scripts/mutate.py`,
  `tests/mutations.json`, run in CI.
- Process-instance binding (#29): job owners carry `owner_key`, and a session
  whose launching host exited is dead, so an orphaned server no longer holds
  its lane.
- Async Codex from Claude across an MCP reconnect, watched live (#32), and
  `server_info().agent_commands` naming the executable a spawn runs (#33).
- Ownership no longer reads the pre-`agent_sessions` registry: no process can
  still write it (see "Retiring the legacy registry" in
  `docs/architecture.md`).
- `claim` probes holders and job owners before it reserves the single writer,
  reusing inside only answers that refuse.
