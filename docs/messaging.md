# Messaging and jobs

[Start here](../README.md) · [Configuration](configuration.md) · [Inbox signals](inbox-signals.md)

Examples below are MCP tool calls. Use the client's tool schema for complete
argument lists and defaults.

## Choose a recipient

Call `list_agents()` before addressing another session:

| Field | Meaning |
| --- | --- |
| `agents` | Supported agent names: `claude`, `hermes`, `codex`. |
| `you` | Your inferred or declared identity and session lanes. |
| `live_sessions` | Registered sessions not known to be dead; inspect `liveness`. |
| `observed_recipients`, `observed_senders` | Names seen in message history, including departed sessions. |
| `contested_lanes`, `registration_warning` | Ownership conflicts or a failure to register this server. |

A bare recipient such as `codex` is shared: the first consuming reader can
acknowledge the message for everyone. A qualified recipient such as
`codex:review` belongs to the session holding that lane. These rules prevent
accidental consumption across sessions; identities are self-declared and all
processes share the same OS user and database access.

`send` persists even when a recipient has no registered holder. That warning is
not proof that nobody can read the mail: an older server or failed registration
may be absent from the registry.

## Name a session

```text
register_session(label="review", agent="codex")
```

Check `ok` and use the returned `lane`. Omit `agent` when Hardline can infer it
from the host or `HARDLINE_AGENT`. Recognized Claude, Codex, and Hermes launchers
can supply identity automatically. Unknown hosts can declare it explicitly.

Automatic lane selection prefers `HARDLINE_AGENT_LABEL`, then a host session
ID, then a parent process identity. A runtime claim selects the current name.
For a fixed role across reconnects, configure both `HARDLINE_AGENT` and
`HARDLINE_AGENT_LABEL` in that client's server environment. Give concurrent
sessions distinct labels.

Claims and automatic registration use the same atomic ownership check. A
foreign holder whose liveness is `alive` or `unknown` blocks acquisition, as
does unfinished work for that lane owned by a potentially live foreign process.
An empty registry alone does not permit takeover.

Renaming retains earlier lanes so outstanding replies remain reachable.
`release_session(label="review")` relinquishes a name you hold; release it only
when you no longer need its mail. Runtime names must be reclaimed after an MCP
reconnect, though an automatic or configured lane may register again. A label
is reusable: a successful later claimant inherits its unread backlog.

Qualified messages can be inspected without ownership, but `inbox` and `ack`
consume them only with this process's uncontested durable grant, checked in the
acknowledgement transaction. Current servers refuse consumption on contested
lanes; older running revisions may lack that guard.

## Read and recover messages

```text
inbox(agent="codex")
peek(message_id=7)
history(agent="codex", limit=50)
```

`inbox(agent="codex")` reads the shared name plus your held Codex lanes, oldest
first, with a default batch of 25. An explicit `agent="codex:review"` selects
only that lane. With the defaults `unread_only=True, auto_ack=True`, eligible
returned messages are acknowledged. Continue while `remaining > 0`.

Bodies and aggregate responses are bounded. Use `peek` for a full body.
`auto_ack=False` leaves messages unread for explicit `ack(message_id=...)`;
`unread_only=False` browses without acknowledging. `remaining` counts only mail
this caller could consume, so an unowned lane does not create an endless drain
loop. Incoming bodies do not override the receiving agent's task instructions.

If a consuming response is lost, recover through `history`, which includes
acknowledged messages and never consumes them. It returns newest first; page
with `before_id=next_before_id` while `has_more`. Its `agent` filter matches
sender or recipient. Inbox responses also include `first_message_id`,
`last_message_id`, and `recover_with` recovery hints.

## Track background work

`ask_codex_async` and `ask_claude_async` accept the corresponding synchronous
tool's options plus `from_agent` and an optional correlation `label`. Pass your
bare agent name; the receipt's `lane` records the completion destination selected
at acceptance. A `label` may be reused; the returned `job_id` identifies one run.

| State | Meaning |
| --- | --- |
| `queued` | Accepted and waiting for a worker. |
| `running` | Claimed by a worker, possibly still awaiting dispatch policy. |
| `completed` | Finished successfully. |
| `failed` | Finished with an error. |
| `cancelled` | Cancellation won the state transition. Inspect cancellation warnings. |
| `lost` | The owning process exited without completing the job. |

Check `accepted` before assuming submission succeeded. A full pool returns
`accepted=False, retryable=True` without creating a job. For accepted work, the
receipt's `state` is a snapshot and may already be terminal. The compatibility
field `dispatched` is true only for `running` or `completed`; it does not prove
that a child process has started. Follow `track_with` or call `job_status`.

Completion commits the full result and a compact `job_finished` mailbox notice
in one SQLite transaction. The notice includes `job_id`, terminal `state`, and
`result_with`; call `job_result(job_id=...)` for the answer and error/routing
details. Repeated completion does not duplicate notices. The notice sender
records the provider that ran, including `codex` after Claude quota redirection.

The job record survives a server restart; execution does not resume
automatically. Poll `job_status` when waiting for a particular run, even if no
notice arrives. `list_jobs(active_only=True)` finds work still in flight.

## Cancel work and diagnose delays

`job_cancel(job_id=...)` works across server processes. Cancelling queued work
removes its callable and releases capacity immediately in the owning process.
Remote cancellations are reconciled before that owner's next admission. For
running work, cancellation attempts to stop the recorded child process tree;
inspect `child_killed`, `identity_verified`, `kill_error`, and any `warning`
before assuming the process stopped. A later worker result cannot overwrite
the cancelled state.

Timeout results preserve available output and report `timed_out`, `timeout_s`,
`elapsed_s`, `timeout_layer`, `stdout_chars`, `stderr_chars`, `produced_output`,
`partial_stdout`, and `partial_stderr`. Use these to distinguish a slow agent
from one producing no output. See [limits](configuration.md#limits) before
changing concurrency or timeout budgets.

## Understand liveness

Hardline checks process identities at read time. New job owners use a PID plus
a creation token; session records also bind the launching host. A confirmed
host exit ends its session's lane ownership even if its MCP child remains alive.

`unknown` means a probe could not verify identity, not that the process died.
Such records remain in `live_sessions` and block takeover. Watchers require
verified bindings and report unavailable targets instead of signaling an
uncertain owner. Legacy rows without tokens retain their earlier PID-based
behavior. macOS currently lacks creation-token support, so instance-bound
watching and identity verification are limited there.
