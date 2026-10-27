# Registering a parent-derived lane at its first call (#109): not built

Proposed and reviewed 2026-10-27, from the stable-address review. Fable
(claude-fable-5-1) and Astra (gpt-6-astra) each attacked it against source.
Both returned "build with named changes", and the changes outweigh the gain.
It is not built. This records why, so it is not proposed again unchanged.

## The proposal

`serve()` registers this process's derived lane before serving, "so a session
is discoverable from its first moment". A Codex or Hermes server's lane is
parent-derived (`<cwd name>.<launcher token>`, `adapters._compute_anchor`), and
most Codex subagent servers never call hardline, yet each appears in
`list_agents` as a session (#109). The proposal: register a lane no relaunch
derives again (`reproducible_lane_suffix() == ""`) at the first tool call
instead of at startup, so silent servers are never offered as destinations.

## Why not

- **It widens the absence gap.** Registration is ownership evidence, not
  advertising: with no live row and no live-owned work, `sessions._refusal`
  grants the name to whoever claims it, and a label may be any generated
  suffix (`adapters.validate_label`). The repo rule is that new checks narrow
  that gap, never widen it.
- **On POSIX, siblings share a lane.** The token hashes the first ancestor.
  On Windows that is each server's own `hardline-mcp.exe` launcher; on POSIX
  it is the `codex` process, so every server it spawns in one cwd derives the
  same lane. Today the top-level server registers first. Deferred, whichever
  sibling calls first wins, and the top-level session's first call comes back
  `contested`.
- **Mail is lost by the delay after all.** A sender who cannot see a silent
  session falls back to bare `codex`, whose one copy any Codex session
  consumes. Today it could address the session's lane, read at its first
  `inbox`.
- **There is no choke point to hang it on.** `_in_thread` runs inside tool
  bodies, and a sync `ask_codex` bypasses it (`_sync_agent`). The correct
  place is a wrapper on the low-level `CallToolRequest` handler in
  `serve_streams`, serialized against Codex's parallel first calls, with "not
  activated" kept distinct from "registration failed" so fulfilment cannot
  undo the deferral.
- **The process inventory would go with it.** `record_revision` runs only
  inside a successful registration, and every revision's `sessions._prune_dead`
  deletes a revision row with no `agent_sessions` row on each `sessions.live()`.
  A rowless startup record would be deleted by older servers, so keeping
  `report`'s view of silent servers needs storage older pruners do not touch.
- **The rule is not universal as keyed.** A Claude host without
  `CLAUDE_CODE_SESSION_ID` (Claude Desktop) is parent-derived too and would be
  deferred, losing its receive-before-call wake.

## What the review settled anyway

- **A subagent's async results stay on its own lane.** Refusing until it
  claims a name buys nothing: a subagent's claim is as short-lived as its
  lane, and an unattributed claim unbinds a name its parent thread had bound.
  The failure that remains is a notice stranded when the subagent exits early;
  the result stays readable through `job_result`.
- **Found in passing, not fixed here:** `_result_lane` returns a parent-derived
  lane without checking it is granted, so a first `ask_*_async` after a
  contested registration admits a job whose notice this process cannot
  consume. Admission should refuse an unowned result destination, checked in
  the transaction that creates the job.

## What the evidence shows instead

On 2026-10-27, 7 unread messages sat on 6 parent-derived `codex:` lanes with
no holder, sent 2026-10-21 to 2026-10-26; two arrived after the
2026-10-25T23:02Z relaunch. Whether each lane was a silent subagent's or a
replaced top-level server's is not recorded: a dead process's rows are
pruned. Deferral would help only with the first kind. For the second, what
survives a relaunch is a claimed name, bound to its thread and restored by its
next server (#100; "Embedded sessions" in `docs/session-continuity.md`).

## Reopen when

Stranded mail can be attributed to silent subagent lanes: mail on a lane whose
server never called hardline. Today a report cannot tell that apart from a
relaunch.
