# Push delivery — mail into a running Claude Code session (#38)

Operator setup is in [inbox signals](inbox-signals.md#claude-code-push-preferred). This page records the design and the evidence behind it.

## Evidence (verified live 2026-10-01, Claude Code 2.1.287, Windows; #38)

| Check | Result |
|---|---|
| Server declares `experimental["claude/channel"]`, session launched with `--dangerously-load-development-channels server:<name>` | debug log: `Channel notifications registered` |
| Push to a session idle at the prompt | the push alone started a turn, 36 ms later, with no input |
| Session launched without the flag | `Channel notifications skipped: ... not in --channels list`; server saw no error |
| Conversation moved to the background (left arrow) | pushes from old and new hosts lost silently; the flag is not carried to the background host |
| Claude Code `initialize` | `clientInfo.name == "claude-code"` |

## Serving

`server.main` serves through `server.serve_streams`. That is FastMCP's own `Server.run`, with two tasks spliced onto the raw stdio streams. All MCP types stay in `server.py`; `channel.py` is pure logic that emits notification params through an injected `send`.

- **Capability.** The initialize result declares the capability. FastMCP's `run_stdio_async` passes no experimental capabilities, which is why it is replaced.
- **Tap.** Passes client messages through unchanged and notes `clientInfo.name` and `notifications/initialized`.
- **Pusher.** Starts only for a `claude-code` client and writes to a clone of the write stream.
  - **Isolated.** A fault is logged and retried with backoff (up to 60 s), never allowed to cancel the task group serving the tools.
  - **Short snapshots.** Reads use a short read-only snapshot (`mode=ro`); a store error is a fault, never an empty inbox.
  - **No SDK internals.** No session capture, no SDK patching, and no cross-thread event loop.

Declaring and pushing is harmless where unused: the host drops pushes for sessions not launched with the flag, and other clients are never pushed to.

## What is pushed, and when

- **Scope.** Unread mail for lanes this process holds. Bare mail is excluded twice: `owned_recipients()` holds qualified lanes only, and the per-batch check filters anything ungranted. Grants are revalidated for every non-empty batch, as consumption revalidates them, so a lane lost to a contest is not advertised.
- **Schedule.** Per message, in memory. A new id is pushed in the next batch. Unread pushed mail is re-announced 5 m, 15 m, then every 60 m after its own last push, so new arrivals never postpone an old reminder. Mail read by anyone (including `inbox(auto_ack=true)` or a later holder) leaves the schedule.
- **Payload.** One line per message, sender and 200-character preview first, then id and lane: Claude Code shows the user only the start of the first line. It asks the model to tell the user who sent each message and what it says. `meta` carries `message_ids`, `count`, `lanes`, `receipt`. Pushing never acks. A batch is stamped and recorded when it is built, before the write, so a receipt can never arrive for an unknown nonce. A write that fails or times out (5 s) is taken back: its nonce is dropped and its messages are due again. Otherwise a later push's receipt would cover mail the model never saw.
- **Scan.** Unread ids are paged (500 per page, up to 20 pages per poll), so mail left unread at the front never starves later mail. A larger backlog is swept across polls from a rotating cursor. Only a sweep that started at the front and finished in one poll may conclude that a scheduled message was read. Bodies are fetched only for the batch being pushed.
- **Pending claims.** The pusher also drives pending claims ([session continuity](session-continuity.md)) every 15 s. A granted name is announced in the next push, followed by its backlog.

## Delivery state: receipts, not inference

The host never acknowledges a notification, and consumption is no proof: a Monitor or manual `inbox` consumes mail just the same. The proof is a receipt, a nonce that exists only inside a notification, which the model echoes through `inbox(receipt=...)`. Echoing it on read rather than on `ack` keeps "I saw it" separate from "I finished it", so deliberately deferred work is not mistaken for a broken channel.

- **What a receipt proves.** A receipt proves its push and covers every earlier one. Each nonce counts once.
- **Recorded facts.** Each serving process records facts about itself in `push_delivery` (pid, process key, declared, last push, last receipted push, oldest unreceipted push). It prunes rows of certainly-dead processes.
- **Derived state.** Readers derive the state: `declared`, `receipted`, `awaiting_receipt` (an unreceipted push younger than 10 minutes), or `unreceipted` (older).
- **Where it is reported.** `list_agents().you.delivery`, each `live_sessions[]` entry, and `send`'s `recipient_delivery`. No record is reported as `unknown`, never as "not delivered".

A receipt echoed by a subagent proves the nonce was seen inside the conversation, not by its main thread. That is accepted as a known limit.

## Review record (gpt-6-astra, two rounds)

**Adopted:**
- **Receipts replace same-process acks as proof.** Consumption through a Monitor would have certified a broken channel.
- **Per-message reminder schedules**, replacing timestamps that new arrivals could postpone.
- **Pusher isolation, send timeout, and cancellation on EOF.**
- **Per-batch grant revalidation.**
- **Detecting tables instead of trusting `meta.schema_version`.**
- **`ack` keeps `message_id` and adds `message_ids`.**
- **No "confirmed if recent" grace clause.** Pushes every nine minutes would have kept a dead channel "confirmed" forever.

**Implementation review (round 3), each with a test and a mutation case:**
- **A startup fault escaped isolation.** `declare()` ran outside the guarded loop.
- **The event loop could block on SQLite.** `state()` shared a lock that was held across fact writes. The lock now guards memory only, and writes are ordered by a version.
- **A malformed `clientInfo` crashed the tap** before the SDK could reject it.
- **A release could be resurrected by a concurrent fulfilment.** The whole release is now serialized under `_claim_mutex`.
- **A later receipt certified an earlier push whose write had failed.** A failed write is now taken back, and its messages are due again.
- **Mail left unread at the front starved everything after the first 500.** The scan is now paged by id.

**Accepted as known limits:**
- **Shutdown can wait on the SDK's own stdout writer and on an in-flight SQLite call**, up to the 10 s busy timeout. That's the same as FastMCP.
- **Revalidation is not atomic with the send.** A notification in flight may describe a lane that was lost a moment earlier; consumption still enforces ownership.

**Found by mutation testing:**
- **The grant revalidation had no test of its own.** Bare-mail exclusion masked it.
- **Receipt single-use was guaranteed by the stale purge, not by `pop`.** The code was simplified to match.

## Not in scope

- **Codex:** [queue-wake](codex-queue-wake.md), a separate transport beside this pusher (`codex_queue.CodexWake`), plus `wake_codex` for hosts with an app-server endpoint.
- **The Monitor's bare-mail scope:** `watch.py` includes the bare name deliberately.
- **Spare background sessions** registering lanes.
