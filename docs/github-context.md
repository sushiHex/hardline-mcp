# GitHub context for cross-reviews — design (rev 4)

> **Slice 1 is implemented (#43).** The user-facing contract is in
> [configuration](configuration.md#github-evidence-for-reviews). Where the
> implementation departs from rev 4, the next section says so and why.
>
> **Rev 4 supersedes the sections below wherever they conflict.** It folds in the final claude-fable-5-1 review of rev 3 and the CLI probes of 2026-10-06.

## As built: departures from rev 4

1. **No separate pin recheck.** Collection runs in the adapter immediately before the reviewer spawns, and it already re-reads the PR after the file list. That after-read is the recheck; a further call would only re-measure a gap of milliseconds. A snapshot *id* is deliberately not rechecked: it names fixed evidence, labelled with its `head_sha`.
2. **`write=True` with `github` is refused.** A reviewer holding third-party text must not also hold write access that an instruction hidden in it could use.
3. **One result object, `github`,** instead of a top-level `github_coverage`. It carries `requested`, `snapshot_id`, `stored`, `head_sha`, `base_sha`, `coverage`, `not_shown`, `files_listed`, `changed_files`, `statuses`, `delivery_hash` and `delivery_bytes`. `delivery_hash` includes the per-call nonce, so two reviewers of one snapshot have different delivery hashes and the same `snapshot_id`.
4. **Budgets count characters.** The delivery cap is `HARDLINE_GITHUB_MAX_CHARS` (not `_BYTES`); the snapshot cap counts real bytes of the canonical JSON.
5. **`github_tools` is Claude-only.** Codex keeps its read-only sandbox shell either way; `"read"` for Codex is rejected rather than silently meaningless.
6. **Codex framing replaces any configured `developer_instructions`** for the call. Codex has no append form, and the framing must reach the system channel.
7. **`GH_HOST=github.com` is forced** for every `gh` child, so an Enterprise default cannot redirect collection.

Verified live on 2026-10-06 against `sushiHex/hardline-mcp#44`: a real Codex and a real Claude both named the PR's title and its 7 files from stdin, reported no tool use, and received the same `snapshot_id`.

## Rev 4 changes (normative)

1. **No dependency on the Claude dispatch lock.** That lock exists only on the quota-routed path (`server.py:1683-1707` dispatches straight to the adapter when no router is configured), and Codex never takes it.
   - **Collection is a pre-step inside the adapter.** Every `gh` child goes through `_run_cmd` with the **same `on_spawn`**, so the existing spawn claim (`adapters.py:748-779`) covers cancellation on every path. A cancel landing between `gh` exiting and the reviewer spawning finds the dead `gh` pid (`ALREADY_GONE`, `jobs.py:474-477`), and the reviewer's spawn claim then returns False, which kills the reviewer.
   - **The pin recheck** is one call immediately before the reviewer spawn, with its own short deadline (10 s), not the 60 s collection budget.
2. **`merge_base_sha` is dropped from slice 1.** The PR payload doesn't carry it. `base.sha` is recorded **as GitHub reported it**, never presented as the live base tip, and "base moved" detection is not claimed.
3. **Snapshot layering.**
   - **What the snapshot holds:** the **full** evidence (full patches as GitHub returns them), bounded only by a snapshot cap (`HARDLINE_GITHUB_SNAPSHOT_MAX_BYTES`, default 4 MB). Over the cap it refuses with per-file sizes.
   - **What's decided at delivery:** per-file budgets, `github_exclude`, and coverage. `snapshot_id` means "the PR as GitHub showed it".
   - **What the result carries:** `snapshot_id`, plus a `delivery_hash` over the exact bytes piped to the reviewer.
4. **Windows-safe storage.**
   - **No overwrites.** If the content-addressed target already exists, that is success; it's never overwritten, since `os.replace` fails on an open target.
   - **Privacy** relies on the profile directory's ACL; POSIX mode bits are a no-op on Windows, and that's documented.
   - **Use tracking.** Reads call `os.utime`, because NTFS last-access is unreliable.
   - **Pruning** is opportunistic on write, bounded per call, and tolerant of PermissionError: no reaper and no background thread (CLAUDE.md).
   - **Opt-out.** `HARDLINE_GITHUB_SNAPSHOT_DIR=""` disables disk storage. A single call still works and still reports its id, but the id can't be reused.
   - **Coverage is advisory, stated honestly.** The server reports `github_coverage`; it cannot stop a reviewer or orchestrator from ignoring it.
5. **File statuses.** Add `rename_only` and `binary`, since both arrive without `patch` and are not `omitted_by_github`. New fixtures:
   - the `--paginate` output shape;
   - oversized-diff error responses;
   - a fork PR with a null `head.repo`.
6. **Reviewer isolation, now verified live (2026-10-06):**
   - **Claude:** `--tools ""` together with `--strict-mcp-config --setting-sources "" --disallowedTools … --append-system-prompt … --no-session-persistence` starts with `"tools":[]` (stream-json init event).
   - **Claude opt-in reads:** `--restricted --tools "Read,Grep,Glob"` refused `Read` on an absolute path outside the cwd ("--restricted confines the file tools to the working directory"). Symlinks and junctions are unverified.
   - **Codex:** `--ignore-user-config --disable apps -c 'web_search="disabled"'` made **zero tool calls** and answered `NO_WEB`. Each part matters:
     - with user config loaded, Codex called MCP tools from `~/.codex/config.toml`;
     - with only `--ignore-user-config`, the `apps` feature's `codex_apps` → `search_service.web_run` still searched the web;
     - without `web_search="disabled"`, the built-in web search ran.
   - **Codex with no `workdir`** reuses the advisory neutral root (`adapters.py:1655-1659`), so it can't read hardline's own checkout. The shell remains, inside the read-only sandbox.
7. **Forwarding: five points.**
   - the plan's `claude_kwargs` (`server.py:1676-1682`), which feeds the explicit list at `:2167-2171`;
   - the reserve guard (`:1608-1616`);
   - the Codex redirect (`:1806-1812`);
   - the async extras (`:2173-2181`);
   - and also `_ask_async_impl`'s signature, `_is_plain_call`, and `_reserve_override_audit` (`:1477-1500`), so routing metadata records `github`.
8. **Additional tests:** an existing target is not overwritten (with a mutation case); pruning tolerates an open file; rename-only and binary files.

The sections below are rev 3 and remain the baseline where rev 4 is silent.

**History:**
- **Rev 1** was reviewed by gpt-6-astra and claude-fable-5-1.
- **Rev 2** folded both reviews in and was reviewed again by gpt-6-astra.
- **Rev 3** settles that pass. The review record is at the end.

## Problem (verified 2026-10-05)

| Adapter | Observed | Cause |
|---|---|---|
| `ask_codex` | `curl` and `git ls-remote` can't connect to github.com:443; `gh`: "config.yml: Access is denied" | `--sandbox read-only` (`adapters.py:151`, `:1627`, `:1653`) |
| `ask_claude` | `gh`, `git`: "requires approval"; WebFetch "not granted" | `--setting-sources ""` plus `-p` denying prompting tools (`adapters.py:2101-2114`, `:2156-2161`) |

The hardline server runs in the user's normal environment, where `gh` is authenticated.

**Delivery facts (verified 2026-10-06):**
- `claude -p "<prompt>"` with piped stdin received both the prompt and the stdin text.
- `codex exec --help` documents piped stdin being appended as a `<stdin>` block.
- A Windows/Python 3.14 probe: text-mode stdin turned `\n` into `\r\n`; `proc.stdin.reconfigure(newline="\n")` with `communicate(input=...)` delivered LF. Python 3.10 and 3.13 are untested.

## Decision: an immutable, content-addressed snapshot, collected once by the host and delivered on stdin

### Snapshots (one evidence set for every reviewer)

- **`github_snapshot(ref)`**, a new tool. It collects the evidence for `ref = "owner/repo#123"` or `"owner/repo#123@<head_sha>"` once and returns `{snapshot_id, manifest}`.
  - `snapshot_id` is the sha256 of the snapshot's **canonical evidence bytes**: canonical JSON of the evidence, excluding `collected_at` and any nonce.
- **`ask_codex` / `ask_claude` / `*_async` take `github=`** with either a `snapshot_id` or a ref. A ref collects implicitly and returns the new `snapshot_id`.
  - **Two reviewers, one evidence set:** call `github_snapshot` once and pass the same id to both. That gives identical bytes, which pinning alone can't guarantee, because the base, comments and checks keep moving.
- **Storage.** Snapshots live as files under `~/.cache/hardline-mcp/github/<snapshot_id>.json`, created with user-only permissions and written atomically: a temp file, then a rename.
  - Content addressing makes them coherent across every hardline process, unlike an in-process cache.
  - They're pruned 24 h after last use. A missing id is an explicit error, never a silent re-collection under that id.
  - A snapshot is private repository content on disk. That's documented, and it is the price of a shared evidence set.

### Contents (first slice: metadata and diff only)

- **Metadata:** repo, number, title, body, author, state, draft, base and head refs, `base_sha`, `head_sha`, `merge_base_sha`.
- **Diff:** `GET /repos/{o}/{r}/pulls/{n}/files`, paginated. It is the PR's own diff (merge-base..head, what GitHub shows), structured per file:
  - `filename`, `previous_filename`, `status` (renamed and so on), `additions`, `deletions`, `changes` and `patch`;
  - GitHub omits `patch` for very large files, recorded as `patch_omitted_by_github`;
  - the endpoint's 3000-file ceiling is recorded when hit.
- **Head consistency.** `head_sha` and `base_sha` are resolved before and after collection.
  - **Unpinned:** if either moved, collection fails with both values, and the caller can retry or pin.
  - **Pinned:** a head that isn't the pin, before or after, fails.
- **Verify before depending on it.** The endpoint's merge-base semantics, its large-file and file-count behaviour, and fork PRs are verified with fixtures recorded from real responses (one fork PR, one rename, one oversized file) before implementation relies on them.
- **Deferred to slice 2:** conversation comments, review submissions, and inline threads with line mapping (side, old and new line, original commit, outdated). Resolved state is reported as `resolved: "unknown"` until GraphQL is added. Check runs come in slice 2 too.

### Size and coverage: bounded, never silently partial

- **Budgets.** Every patch is included up to a per-file budget, 40 KB by default. Lockfiles and generated files are **not** excluded by default; they're simply capped like any other file.
- **Coverage.** The manifest records, for every file, `included` / `truncated` / `omitted_by_github` / `excluded_by_caller`, and an overall `coverage: "complete" | "partial"`.
- **Partial coverage blocks approval.** The framing instructs the reviewer to limit its verdict to the files it actually saw. The result carries `github_coverage`, so an orchestrator can refuse to treat a partial review as whole-PR approval.
- **Narrowing.** `github_exclude=[glob]` narrows a review deliberately; excluded files are listed, not hidden.
- **Refusal only for the impossible.** If the file list plus per-file headers alone exceed the total budget (`HARDLINE_GITHUB_MAX_BYTES`, 400 KB default), collection fails with per-file sizes, so the caller can narrow.

### Collection mechanics

- **Where it runs.** In the worker **before** the Claude dispatch lock (`server.py:1577`), never inside it. Inside the lock, just before spawn, cancellation and the pin are rechecked in one cheap call.
- **How `gh` runs:** `-R owner/repo`, `GH_PROMPT_DISABLED=1`, `NO_COLOR=1`, `stdin=DEVNULL`, UTF-8.
- **Cancellable and bounded.** Each `gh` child is registered so `job_cancel` kills its tree. One deadline covers every page and retry (`HARDLINE_GITHUB_TIMEOUT_S`, default 60); on expiry the child is killed and reaped.
- **github.com only** in this slice.

### Delivery

- **The pipe.** `stdin=PIPE` only when a snapshot is attached; otherwise `DEVNULL` as today (`adapters.py:735`). It's written with `communicate(input=...)` after `proc.stdin.reconfigure(newline="\n")`.
- **Cancellation.** One that interrupts the write reports cancelled.
- **Routing.** Setting `github` forces the telemetry path (`_is_plain_call`, `adapters.py:1029`), which already passes `--no-session-persistence` (`:2125`). Codex already runs `--ephemeral`.
- **Forwarding.** `github` and `github_exclude` are forwarded through **all three** whitelists: the reserve guard (`server.py:1608-1616`), the Codex redirect (`:1806-1812`) and the async extras (`:2173-2181`). Each path gets a test, and both are validated in `validate_request`.

### Reviewer isolation (with `github` set)

- **Claude: zero tools by default** (`--tools ""`). The reviewer reasons over the snapshot only, so it can't open local files.
  - **Opt-in `github_tools="read"`** gives `--restricted --tools "Read,Grep,Glob"` with `workdir`. `--restricted` confines file tools to the working directories; whether that holds against absolute paths, symlinks and junctions is unverified and gets a test before the option ships.
- **Codex:** the read-only sandbox plus `-c 'web_search="disabled"'`. That's a documented config key; local enforcement is unverified and gets a test.
  - Codex always has a shell, and whether its Windows sandbox confines reads is unverified (it denied `AppData\Roaming` in the probe). That's documented as a residual exposure.
- **Framing in the system channel.** It goes in Claude `--append-system-prompt` (on the telemetry path; advisory uses `--system-prompt`, `adapters.py:2147`) and Codex `developer_instructions` (`:1669`).
  - **Delimiters:** the snapshot is wrapped in random-nonce markers, and HTML comments are stripped from bodies.
  - **Returned text is untrusted:** the orchestrator must treat a reviewer's prose as untrusted before posting it anywhere.

### Persistence

- **Request rows** keep the `github` ref or id plus `snapshot_id`, never content (`server.py:1886-1895`, `jobs.py:140`).
- **Results are persisted** (`jobs.py:248`) and readable through `job_result`. Reviewer quotes and the stdout/stderr excerpts in error results (`adapters.py:803`, `:841`) can contain evidence. That's documented as a same-user local store.

## Tests the slice cannot ship without

- **Adversarial fixtures:**
  - the decisive change in a truncated file must produce `coverage: partial` and a scoped verdict;
  - base or head moving during collection;
  - cancellation during collection;
  - Unicode and CRLF bytes reaching the child unchanged, with the hash matching;
  - a fork PR;
  - a rename.
- **Forwarding:** each of the three paths.
- **Isolation:** zero tools for Claude; the Codex web-search flag present.
- **Mutation cases** for every guard, per CLAUDE.md.

## Deferred

- slice 2: comments, reviews, inline threads with mapping, and check runs;
- resolved state through GraphQL;
- issues and multiple PRs;
- CI logs;
- Enterprise hosts;
- a live read-only GitHub MCP tool for the child, pinned to caller-named objects.

## Review record

**Round 1 (astra, fable):** canonical SHAs; per-section completeness; stdin transport; no bundle in request rows; the live `gh` allowlist rejected; redirect wiring; the CRLF fix; no refusal of large diffs; a collection timeout; cancel mapping.

**Round 2 (astra):**
- **Tool narrowing is not containment.** Hence zero tools by default, and `--restricted` for opt-in reads.
- **Pinning is not one evidence set.** Hence the content-addressed snapshot. This reverses the rev 2 "no cache" resolution: content addressing is coherent across processes, and an in-process cache was not.
- **"Never refuse" could produce a confidently wrong review.** Hence `coverage` and partial blocking approval. Lockfiles are capped, not excluded.
- **The compare API was unverified.** Replaced with the PR files endpoint plus fixtures.
- **The lock.** Collection moved outside it, made cancellable, under one deadline.
- **A third forwarding whitelist** (`server.py:2173`).
- **Rev 2's internal contradictions** (the unreachable plain-path flag, the system-prompt citation) are fixed.
- **Scope narrowed** to metadata and diff.
