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

## Names held before: tell, don't restore

A relaunch or an `/mcp` reconnect keeps the conversation id but starts a new hardline. The derived lane comes back by itself; a name the conversation claimed does not, since claims live in their process. Before this, only the model's own reading of its transcript recovered one.

`hints` records, at each successful grant, which conversation (full `CLAUDE_CODE_SESSION_ID`) now holds the name, and which process granted it, in a table of its own (`lane_hints`). On current code, the next grant of that name replaces the row. A claimant with no conversation id (Codex, Hermes), or an automatic grant, only clears it. The granting process's release removes it, and the table is bounded. Older revisions write and clear nothing, so their hints can go stale, which costs only advice. A restarted conversation is then told:
- `list_agents().you.previously_held`, with unread counts, and `inbox` for those with mail. Only names of the agent this process serves are listed.
- A wake when newer mail arrives at such a name, through the same notice or push as its own mail. It's tracked apart from that mail, so a later reclaim is still announced, and it shares the one-outstanding-notice gate.

The conversation decides. `register_session(..., wait=true)` asks under the ordinary rule; `release_session` forgets a hint it does not want. Nothing is claimed, routed or read on a hint's behalf, so a stale hint costs a suggestion and never a name.

## Review record (gpt-6-astra, two rounds)

**Adopted:**
- **The secret was rejected as evidence.** A first draft proved continuity with a secret shown to the conversation. The review showed it proves possession, not identity: `/fork` copies, subagents and pasted output all pass, and spent secrets can be re-armed by ordinary reacquisition.
- **Durable intents were cut.** Keyed by host and session, they would have caused surprise renames, durable capacity accounting, cancellation across code revisions, and a waiting loser inheriting a role long after its task ended. Waiting is opt-in and process-local instead.
- **Pending stays `ok: false`.** `_register_session_impl` adopts the name locally whenever `ok` is true, and the tool contract says to check `ok`.
- **Fulfilment is serialized.** `_claim_mutex` serializes it against explicit claims and against the **whole** of `release_session`, not only pending-claim cancellation. Round 3 reproduced the gap: a fulfilment snapshots every held lane and re-claims them together with the awaited one, so a release landing between the snapshot and the write saw its lane written back.

**Deferred:**
- **Automatic restoration of runtime names after a same-host reconnect**, keyed by verified host plus session id. Superseded by [names held before](#names-held-before-tell-dont-restore), which tell rather than restore.

## Review record: names held before (Fable, gpt-6-astra, 2026-10-09)

A first design restored claimed names automatically at startup from a `lane_holders` table. Fable rejected it as the cut durable intents under a new name:
- its wait rule was always true, so a second window on one transcript would inherit the name and its mail when the first closed;
- replayed claims rename the process, because `lane_suffix` returns the last claim;
- old names refill the claim cap;
- a release on older code leaves a row that new code would restore;
- claiming before `anyio.run` delays the MCP handshake;
- a relaunch is a new host, so it lacks the verified-host evidence the deferral required.

Both reviewers chose hints instead. Astra's corrections are adopted:
- the field says what was held, not what may be reclaimed;
- the wake for such mail is tracked apart from announced mail;
- a hint is never turned into an automatic `register_session` by the instructions;
- release forgets a hint;
- recovery never initializes before serving, never fetches unclaimed bodies, and never fails a tool.

**Rejected:** restoring on a verified-host reconnect only. It's syntax-guarded rather than provenance-guarded, an immediate grant can lose to an exiting predecessor, and a release by older code can be replayed. A relaunch, the case actually hit, would need the hint anyway.

**Implementation review (Astra), all adopted:**
- **A grant generation is needed after all.** My claim that a release and a later grant of the same name can't interleave was false. The next claim needs only the registry row gone, and a second window on the same transcript can claim in the gap between the release committing and its hint delete. A hint therefore records its `writer` (the granting process's identity), and a release forgets only its own. Dismissal forgets whatever hint the conversation has for the name.
- **Hint reads are isolated from the wake.** A failed hint read had aborted the whole poll, and with it the mail the session holds.
- **The scan for mail at old names is one grouped read** of the newest id and count per name. A paged scan had stopped short and lost names past it. A name is told again only when newer mail arrives.
- **Automatic grants clear the hint for their name**, looking before taking the writer.
- **Hints are limited to the agent this process serves**, since `register_session` could ask only for that agent's name.

**Longer term:** both reviews point at the root cause, one name serving as process, conversation and role. That's the stable-address question in CLAUDE.md, a routing migration rather than a continuity fix.

Also corrected in passing: the docs claimed acquisition needs positive evidence that nobody holds a lane. `sessions._refusal` grants when no registered holder and no live owed work exist, so an unregistered consumer with no jobs is invisible to it.

**Rejected:**
- **Parsing `--fork-session --resume` from the host command line.** It's undocumented and per-host, and it proves ancestry, not move-versus-copy.
- **A SessionEnd hook releasing the old lanes.** It does not fire on a move.

## Operator note

The left arrow moves a conversation easily by accident. `leftArrowOpensAgents: false` in `/config` disables it for foreground sessions; `/bg` remains a deliberate act.
