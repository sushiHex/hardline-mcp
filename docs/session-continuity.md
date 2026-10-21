# Session continuity — names that outlive a process (#39)

## Problem

A lane is held by a hardline **process**, but Claude Code can move a conversation to a new one. Moving a conversation to the background (the left arrow, `/bg`) forks it into a new host: `claude.exe --session-id <new> --fork-session --resume <old transcript>`. The new hardline process derives a **different** lane from the new session id. On 2.1.287 the original host stayed alive as a viewer, and its hardline stayed connected, so the old lane stayed held, unread, until that window closed. On 2.1.296, in the one run measured ([below](#the-move-live-2026-10-13-claude-code-21296-windows)), `/bg` freed the terminal and the old host exited at once, so the old lane was free immediately. Either way, mail sent to the old lane waits there until the conversation asks for it.

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

## The move, live (2026-10-13, Claude Code 2.1.296, Windows)

An interactive session in a fresh directory, driven through a ConPTY:
- `/bg` printed `backgrounded · <id>` and freed the terminal. The old host and its hardline exited at once. The background host ran the fork command line above, under a new supervisor process.
- The background session took a turn from the `SessionStart` `fork` hook below. It called `list_agents`, then `register_session(label=<old lane>, wait=true)`, which was granted at once, since the holder was gone, with `inherited_unread: 1`.
- Mail sent to the old lane a second earlier reached it through its inbox notice. It read with the receipt and acked, 7 s after the send.

Nothing in hardline gives it that turn. No process holds the old lane, so no server wakes for its mail, and the MCP instructions reach a model only when it takes a turn.

## A turn for a moved conversation: the fork hook

A conversation sent to the background is idle. A `SessionStart` hook with matcher `fork` and `asyncRewake` gives it a turn: exit code 2 hands the hook's stderr to the model. In `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "fork",
      "hooks": [{
        "type": "command",
        "command": "python",
        "args": ["-I", "-c", "import sys; sys.stderr.write('[hardline] This conversation was just forked or moved to a new session, which starts with a new hardline address. If it uses hardline: call list_agents; if an earlier hardline result here shows a lane you no longer hold and you still answer to it (not one you released), call register_session(label=<that lane without the agent prefix>, wait=true) once.'); sys.exit(2)"],
        "asyncRewake": true
      }]
    }]
  }
}
```

It carries the MCP instructions' own condition, "and you still answer to it", so a name the conversation released is not asked for again. `/fork` and `/branch` fire it too. A copy then waits for the original's lane, and once the original exits the lane goes to whichever waiting copy is granted it first, with its backlog; nothing designates a successor. A conversation that doesn't use hardline spends one short turn on it.

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

**Longer term:** both reviews point at the root cause, one name serving as process, conversation and role. That's a routing migration rather than a continuity fix, deferred against stated criteria in [stable instance addresses](architecture.md#decisions-and-what-was-rejected).

Also corrected in passing: the docs claimed acquisition needs positive evidence that nobody holds a lane. `sessions._refusal` grants when no registered holder and no live owed work exist, so an unregistered consumer with no jobs is invisible to it.

**Rejected:**
- **Parsing `--fork-session --resume` from the host command line.** It's undocumented and per-host, and it proves ancestry, not move-versus-copy.
- **A SessionEnd hook releasing the old lanes.** It does not fire on a move.

## A Codex thread's names (2026-10-16)

A Codex session's names come back by themselves. Claude conversations still get advice only, as above.

**Why Codex, and why now.** A Codex session's hardline server changes with every restart or reconnect (one server per loaded thread), and its derived lane is parent-derived, so it changes too. Its claimed names were lost every time, and Claude sessions addressing `codex:constructicon-wave` reached nobody until a human told the session to claim the name again. Its thread does not change: Codex puts the thread id in every `tools/call` `_meta`, a resume keeps it, and a fork makes a new one. So the name is bound to the thread, not to anything about the process.

**The record.** `thread_names` (`bindings.py`): one row per name, holding the thread it belongs to, its place in that thread's list (`seq`), and the server that last held it there `(pid, process_key)`. Only attributed actions write it:
- an explicit claim from a request on the wake's pinned, unstopped top-level thread, for a Codex name, by a process with an identity token, binds the name to the thread (`bound_to_thread: true`);
- any other explicit claim unbinds it (`binding_note` says why);
- a release or a dismissal (`release_session` of a name not held) withdraws it, whoever wrote it for this thread;
- a restoration transfers it to the restoring server.

Automatic grants (startup, heartbeat, repair) never touch it, so a name another process merely re-acquired keeps its thread's claim; after that process exits, the thread's next server gets it back with its backlog, as a claim would. An explicit claim that cannot be attributed, including a repeated `register_session` from a subagent or after the wake stopped, does remove the binding: claim from the top-level thread to keep it.

**Restoration.** The wake's poll runs it, through `_regain_lanes`, never a tool. The first poll after the session's first hardline call surveys up to eight of the thread's bindings without the writer. A binding is restorable when its writer is verifiably DEAD, nothing else holds the name or owes work to it, and this server has room. Restorable bindings are then claimed in one transaction under `_identity_lock`. Attribution is checked once before any write, and each candidate is validated again inside its own savepoint before anything is written for it. A refused candidate costs nothing. Capacity is spent only on names that add a list entry. Then every row is reranked and the bindings renumbered to this server's order. The local claim list is adopted after the commit.

Restored names form a run below the explicit claims, in durable order. The address is the last held lane, so a restart answers to the same name. More than eight names take one further fulfil per eight. `list_agents().you.restoration` reports what the last sweep found.

**Async admission.** A dispatch whose result would go to the parent-derived lane is refused, retryably, while this thread has bindings this server does not hold. A later server of the thread is not guaranteed to hold that lane, so it might never read the result. The refusal names the lanes and two ways out: claim a name, or dismiss one with `release_session`. A server with a claimed name or a reproducible lane dispatches as before. The rule reads the table at that moment, under the same lock as recipient selection and `jobs.create`; another process binding after the read is accepted as the boundary.

**What it does not do.** Nothing happens before the session's first hardline call, so an idle resumed thread is not woken. Restored names reach `inbox` after the next eligible poll, not in the pinning call. Two live servers on one thread (a TUI and the desktop app) share the list, and the first eligible one wins a name. A grant by an older revision is invisible to the table. Existing names have no binding until they are claimed once on this revision. Consumption stays process-wide.

**When a Codex server is replaced (researched 2026-10-20 against `rust-v0.163.0`; not all observed).** A Codex server lives as long as its loaded thread, not its client:
- Resuming a loaded thread rejoins it, with the same MCP servers (`thread_processor.rs`, `resume_running_thread`). The exception: a resume with different settings while no other client is attached and the thread is idle cold-resumes it.
- A thread is unloaded once no client is attached and it is idle for `thread_unload_delay_secs` (default 1800 s, `thread_lifecycle.rs`). Its servers' job object is then closed, killing the hardline tree.
- The managed daemon's updater restarts the daemon at each Codex release, ending every server. Observed on 2026-10-15, after which the top-level threads got new servers within 15 s. `codex app-server daemon restart` does the same by hand.

So quitting and resuming a Codex session keeps its old hardline server, and its old code. Seen 2026-10-20: two top-level servers had run 2026-10-15's code for five days through daily use. A restored name returns when the server is replaced this way.

**Design record.** The design took nine review rounds. Two first designs were rejected: restoring from `lane_hints`, which are lossy advice; and a writer-scoped table, where a stale binding survived a takeover. Seven Fable/Astra rounds followed, recorded in `research/codex-names/`. What changed shape along the way:
- per-request attribution, not the wake's pin;
- no stored intent to cancel;
- restored names as ordinary claims replayed in durable order;
- restoration only on the poll thread, which deleted every time-budget rule;
- admission reading one fact from the table instead of a restoration state machine.

Astra approved round 7 with seven must-fix changes, applied in the build:
- claim publication under `_identity_lock`;
- one identity for admission and dismissal;
- a release withdraws the pinned thread's binding;
- durable-first release;
- repair after a partial publication;
- honest scheduling and contention wording;
- regressions for each.

The implementation was then reviewed by both, and Astra reviewed the fixes once more. What the reviews found, and where it stands:
- **Adoption without the ownership rule, fixed.** A name already held here skipped the rule and its registry grant, though held here is memory, not a grant. Every candidate is now checked and granted.
- **Publication, fixed.** It is prepared before the commit and published origins first, each origins set rebound whole, so a fault part way leaves names unlisted rather than listed without their origin. The next attempt then adopts them again. An explicit claim of such a name before the repair clears its leftover origin, so it stays explicit.
- **Missing table, fixed.** A store with no binding table reports `no binding table`.

Regressions, each with a `names-*` mutation case, cover:
- contested and grant-less adoption;
- a candidate refused after its writes;
- a publication fault and its repair;
- multi-name order across a restart;
- a dispatch and a claim interleaved by events, for explicit and fulfilled claims;
- both two-restart variants, the second with S1 admitting the job before it dies;
- paging, dismissal, a dead host, and discovery without a declared agent.

Deferred, as reporting nits:
- a held release that fails raises, where a pending one returns `ok: false`;
- `restored` lists lanes without their unread counts;
- dismissing a name another live server holds does not say that server keeps it.

## Operator note

The left arrow moves a conversation easily by accident. `leftArrowOpensAgents: false` in `/config` disables it for foreground sessions; `/bg` remains a deliberate act.
