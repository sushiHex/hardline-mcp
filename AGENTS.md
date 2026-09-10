# Repository Guidelines

## Project Structure & Module Organization

- `hardline_mcp/` contains the Python package: `server.py` wires MCP tools; `mailbox.py` handles SQLite messaging; `adapters.py` invokes agent CLIs; `jobs.py`, `sessions.py`, and `procid.py` manage dispatches and process identity.
- `tests/` contains unit, headless MCP integration, and optional live-agent tests; shared fixtures live in `conftest.py`.
- `docs/architecture.md` explains design decisions. Read `CLAUDE.md` for development gotchas and `README.md` for configuration. `TODO.md` tracks open work.
- `pyproject.toml` defines packaging and dependencies; `.github/workflows/ci.yml` defines CI. Build artifacts belong in ignored `dist/`.

## Build, Test, and Development Commands

Use Python 3.10+ in a virtual environment.

- `python -m pip install -e ".[dev]"`: install editable source and pytest.
- `python -m pytest -q -rs`: run the default suite and report skip reasons, matching CI.
- `python -m pytest tests/test_mailbox.py -q`: run a focused test module.
- `python -m pip wheel --no-deps . -w dist`: build a wheel using Hatchling.
- `hardline-mcp`: start the stdio MCP server; it waits for client input.

CI tests Python 3.10 and 3.13 on Ubuntu and Windows.

## Coding Style & Naming Conventions

Follow existing Python style: four-space indentation, type annotations, descriptive docstrings, `snake_case` functions/modules, `PascalCase` classes, and `UPPER_SNAKE_CASE` constants. Keep MCP imports in `server.py`; offload blocking tool work through AnyIO worker threads. Specify subprocess text encodings for Windows compatibility. No formatter or linter is configured in project metadata or CI.

## Testing Guidelines

Use pytest, with `@pytest.mark.anyio` for async tests. Name files `test_*.py` and functions `test_<behavior>`. Use `tmp_path` databases and `monkeypatch`; wait for background deliveries before fixture teardown.

For bug fixes, verify the regression test fails when the original defect is deliberately restored, then restore the fix. No numeric coverage threshold is configured.

Live CLI tests require `HARDLINE_LIVE_TESTS=1` or `HARDLINE_TEST_SPAWN=1`, depending on the module, and consume plan tokens.

## Commit & Pull Request Guidelines

Follow history's prefixes: `feat:`, `fix:`, and `docs:`, followed by a concise description. Keep PRs focused; explain the problem, resulting behavior, validation, and relevant issues. Document configuration or schema changes.

## Configuration & Architecture Safeguards

Use `HARDLINE_DB` for isolated manual runs. Never use the operator's live mailbox for tests. Preserve compatibility with concurrently running revisions: avoid reshaping tables in place. Derive liveness from OS probes and pair PIDs with creation-time tokens.
