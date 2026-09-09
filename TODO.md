# TODO

Open work, roughly in the order it is worth doing. Everything here is known and
deliberate — nothing below is a surprise waiting to be discovered.

## Verify session-to-session addressing for real

Two live agent sessions have never exchanged a message through a claimed lane.
It is proven by tests, by 75 mutations, and by a smoke run against the real
store — but nobody has watched it work.

The check is small:

1. Restart a Codex terminal session so it picks up current code. It should
   appear in `list_agents()` under `live_sessions` **on its own** — no
   `register_session` call, no config change.
2. From another session: `send(from_agent="claude", to_agent="codex:<lane>",
   message="ping")`.
3. In the Codex session: `inbox(agent="codex")` should return it, and
   `acked_at` should be set — consumable, not merely visible.

If step 1 shows nothing, that is the finding. Check `server_info()` for
`code_revision` and `registration_warning`.

## Commit the mutation harness

`CLAUDE.md` says to mutation-test every fix, and the harness that did it is not
in this repo — it lived in a scratch directory and is gone. That makes the most
important habit here unreproducible.

It is a small script: a list of `(name, file, old_text, new_text, tests that
must fail)`, applying each edit, running the named tests, asserting they FAIL,
and restoring in a `finally`. Two things it must do, both learned the hard way:
assert each anchor appears **exactly once** before applying (a stale anchor
otherwise reports a kill for a mutation never applied), and refuse a `-k`
selector that matches no tests (it "passes" vacuously).

## Decide the ownership model

The design review argued that absence-based ownership is the wrong foundation:
that a stable *instance* address should be separate from a human *display*
label, so a name is never transferred and absence never grants ownership. It was
not adopted — absence was made insufficient instead, requiring positive evidence
before a claim.

That is a hardening, not an answer. Worth settling before more is built on it.

## Known limits, each a deliberate trade

**Mixed revisions are half-blind.** Older processes read and write the `sessions`
table; current code uses `agent_sessions` and consults both for ownership. That
closes this side only — an older process will not read the new table whatever we
write. It resolves itself as old processes exit. The orphan `sessions` table can
be dropped once none remain.

**An orphaned server holds its lane.** The registry records the hardline
process's pid, not the session's. If an agent session exits and leaves its
hardline running, that process stays alive, stays registered, and holds a lane
nobody is reading. `atexit` does not cover it — the parent's exit does not run
the child's handlers. Fix would be to store the session anchor's pid and token
beside the server's, and require both live.

**macOS has no process identity.** `procid.process_key` returns `None` there
(no `/proc`, and `proc_pidinfo` is not wired up), so identity degrades to the
pid alone and reuse is undetectable. Windows and Linux are covered.

**A job owner has no creation token.** `jobs.owner_pid` has no `owner_key`
beside it the way `child_pid` has `child_key`, so a reused owner pid can block a
lane claim. It fails in the safe direction — refusing rather than stealing.

**`claim` probes the OS inside `BEGIN IMMEDIATE`.** It holds the single writer
while checking job owners. The row count is normally zero or one, so it is
microseconds, but it is the writer lock.

## Test-infrastructure fix

The mailbox seat belt in `tests/conftest.py` counts messages in the operator's
real store around the whole run, so any other live agent session writing during
a local run reads as a leak. It should compare *identity* rather than count —
record `MAX(id)` before, then inspect what actually arrived — so a real leak is
still caught and concurrent traffic is not misreported.
