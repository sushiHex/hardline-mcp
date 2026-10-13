# Development

[Start here](../README.md) · [Contributor guidelines](../AGENTS.md) · [Working notes](../CLAUDE.md)

## Local checks

From the repository root, with Python 3.10+ in an activated virtual environment:

```sh
python -m pip install -e ".[dev,codex-watch]"
python -m pytest -q -rs -n auto
python scripts/mutate.py
```

The default suite uses isolated temporary mailboxes, mocked agent processes,
real loopback WebSockets, and headless MCP subprocesses. It does not require
agent accounts or start model turns. Never point manual tests at the operator's
mailbox; use a separate `HARDLINE_DB`.

CI runs three jobs side by side: the suite on Ubuntu and Windows with Python
3.10 and 3.14, in parallel workers (`-n auto`, from `pytest-xdist` in the `dev`
extra); the mutation catalog once; and the live sandbox tests on Linux and
macOS. The `codex-watch` extra enables adapter tests that otherwise skip. Keep
`-rs` so missing dependencies or unavailable features remain visible; CI also
sets `HARDLINE_STRICT_SKIPS=1`, under which only a skip declared with a `skip`
or `skipif` mark passes, and one decided at run time fails. A newer push to a
pull request cancels the run in flight. No numeric coverage threshold is
configured.

The mutation runner verifies regression tests against defects catalogued in
`tests/mutations.json`. It checks every anchor first, then runs one baseline in
which each case's selected tests must execute and pass, then each exact
mutation in a worker's own copy of the source; only a regression assertion
failure counts. `--jobs` sets the workers (default: one per CPU). Pass case
names to run a subset.
Follow [working notes](../CLAUDE.md#mutation-test-every-fix) when fixing behavior.

## Optional live tests

These tests launch real clients, require their dependencies and account setup,
and consume plan tokens. They are off by default.

| Enable | Test module | Exercises |
| --- | --- | --- |
| `HARDLINE_LIVE_WATCH=1` | `tests/test_live_watch.py` | Claude Monitor and Codex app-server wake acceptance with isolated mailboxes. |
| `HARDLINE_LIVE_TESTS=1` | `tests/test_live_agents.py` | Real agent CLI replies and model/effort telemetry through MCP. |
| `HARDLINE_TEST_SPAWN=1` | `tests/test_spawn_behaviour.py` | Real CLI spawn behavior. |
| `HARDLINE_LIVE_SANDBOX=1` | `tests/test_live_sandbox.py` | Hardline's read-only pin under Codex's own sandbox, with dummy secrets only. Needs `codex` but no login or model turn; CI runs it on Linux and macOS. |

For example, in a POSIX shell:

```sh
HARDLINE_LIVE_WATCH=1 python -m pytest tests/test_live_watch.py -v -s
```

In PowerShell:

```powershell
$env:HARDLINE_LIVE_WATCH = "1"
python -m pytest tests/test_live_watch.py -v -s
Remove-Item Env:HARDLINE_LIVE_WATCH
```

Set the appropriate `HARDLINE_*_CMD` if a CLI is not discoverable. Live tests may
skip per client when prerequisites are missing; inspect skip reasons. Watch
acceptance covers idle wake, recipient isolation, stopped helpers, and Codex
busy deferral and restart. See [recorded results](hardline-watch-design_2026-09-09.md#verification-and-measured-boundaries)
for the tested clients and remaining boundaries.

## Pull requests

Keep changes focused, describe the resulting behavior, and report relevant
validation and skips. Update the user guide when contracts or configuration
change. Use [architecture notes](architecture.md) for rationale and historical
evidence so the quickstart stays focused on using the project.
