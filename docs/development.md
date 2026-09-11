# Development

[Start here](../README.md) · [Contributor guidelines](../AGENTS.md) · [Working notes](../CLAUDE.md)

## Local checks

From the repository root, with Python 3.10+ in an activated virtual environment:

```sh
python -m pip install -e ".[dev,codex-watch]"
python -m pytest -q -rs
python scripts/mutate.py
```

The default suite uses isolated temporary mailboxes, mocked agent processes,
real loopback WebSockets, and headless MCP subprocesses. It does not require
agent accounts or start model turns. Never point manual tests at the operator's
mailbox; use a separate `HARDLINE_DB`.

CI runs on Ubuntu and Windows with Python 3.10 and 3.13. The `codex-watch` extra
enables adapter tests that otherwise skip. Keep `-rs` so missing dependencies or
unavailable features remain visible. No numeric coverage threshold is configured.

The mutation runner verifies regression tests against defects catalogued in
`tests/mutations.json`. Each case runs a passing baseline and an exact mutation
in a temporary source copy; only a regression assertion failure counts. Pass
case names to run a subset. CI runs the full catalog on Ubuntu/Python 3.13.
Follow [working notes](../CLAUDE.md#mutation-test-every-fix) when fixing behavior.

## Optional live tests

These tests launch real clients, require their dependencies and account setup,
and consume plan tokens. They are off by default.

| Enable | Test module | Exercises |
| --- | --- | --- |
| `HARDLINE_LIVE_WATCH=1` | `tests/test_live_watch.py` | Claude Monitor and Codex app-server wake acceptance with isolated mailboxes. |
| `HARDLINE_LIVE_TESTS=1` | `tests/test_live_agents.py` | Real agent CLI replies and model/effort telemetry through MCP. |
| `HARDLINE_TEST_SPAWN=1` | `tests/test_spawn_behaviour.py` | Real CLI spawn behavior. |

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
