# Session continuity — names that outlive a process (#39)

## Problem

A lane is held by a hardline **process**, but Claude Code can move a conversation to a new one. Moving a conversation to the background (the left arrow, `/bg`) forks it into a new host: `claude.exe --session-id <new> --fork-session --resume <old transcript>`. The new hardline process derives a **different** lane from the new session id. The original host stays alive as a viewer, and its hardline stays connected, so the old lane stays held, unread, until that window closes. Mail sent to the old lane waits there.

## Evidence (verified 2026-10-01, Claude Code 2.1.287, Windows)

| Signal | Available? |
|---|---|
| The old holder exiting, so its process (and host) is DEAD | yes, and it's positive evidence |
| A parent id for a fork, via env, transcript metadata or hook input | **no**; only the host's undocumented command line has it |
| SessionEnd when a conversation moves to the background | **no**; it fired only at window close (move 03:14:44, `SessionEnd:other` 03:23:28) |
| Telling a move from a copy (`/fork` and `/branch` also fork) | **no** |

## Design: wait for the name, don't take it

Nothing can tell a conversation that moved away from a copy that is still being read. So a live holder's name is never transferred, and the rule *absence is not evidence* stands unchanged. What changes is that a refused claim can **wait**:

- **Asking.** `register_session(label, wait=True)`: when the claim is refused because of a live or unknown holder, or work still owed to the name, the claim is kept pending in this process. The result stays `ok: false` (`status: "pending"`), so no caller can mistake it for ownership.
- **Fulfilment.** Pending claims are retried through the same `register_session` path, under the same atomic ownership rule. This happens on the heartbeat (tool calls), on `list_agents`, and every 15 s in the wake loop, so an idle session is not left waiting. When the holder is DEAD, the claim is granted with its unread backlog.
- **The session's own lane.** A reconnect can start the new server before the old one exits, and the derived lane is refused then. The wake loop retries the session's own registration the same way while it is failing, so the session takes its lane, and wakes for its mail, once the old server is gone.
- **Cancelling and lifetime.** `release_session` cancels a pending claim. It lives in memory only, ends with its process, and counts toward `MAX_CLAIMED_LANES`.
- **The trigger.** The server's MCP `instructions`, which reach every connected model, say: *if an earlier hardline result shows a lane you no longer hold, call `register_session(label=<that lane>, wait=true)` once.* The name is public, so no secret is involved, and the conversation's own transcript carries it across the fork.

A moved conversation therefore gets its old address back, with nothing lost and no overlap, the moment the old window closes.

## Review record (gpt-6-astra, two rounds)

**Adopted:**
- **The secret was rejected as evidence.** A first draft proved continuity with a secret shown to the conversation. The review showed it proves possession, not identity: `/fork` copies, subagents and pasted output all pass, and spent secrets can be re-armed by ordinary reacquisition.
- **Durable intents were cut.** Keyed by host and session, they would have caused surprise renames, durable capacity accounting, cancellation across code revisions, and a waiting loser inheriting a role long after its task ended. Waiting is opt-in and process-local instead.
- **Pending stays `ok: false`.** `_register_session_impl` adopts the name locally whenever `ok` is true, and the tool contract says to check `ok`.
- **Fulfilment is serialized.** `_claim_mutex` serializes it against explicit claims and against the **whole** of `release_session`, not only pending-claim cancellation. Round 3 reproduced the gap: a fulfilment snapshots every held lane and re-claims them together with the awaited one, so a release landing between the snapshot and the write saw its lane written back.

**Deferred:**
- **Automatic restoration of runtime names after a same-host reconnect**, keyed by verified host plus session id. It's valid evidence, but a separate change; the docs still say to re-claim after a reconnect.

**Rejected:**
- **Parsing `--fork-session --resume` from the host command line.** It's undocumented and per-host, and it proves ancestry, not move-versus-copy.
- **A SessionEnd hook releasing the old lanes.** It does not fire on a move.

## Operator note

The left arrow moves a conversation easily by accident. `leftArrowOpensAgents: false` in `/config` disables it for foreground sessions; `/bg` remains a deliberate act.
