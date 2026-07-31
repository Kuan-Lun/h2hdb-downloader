# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## Project overview

`h2hdb-downloader` is a small Python library that automates downloading
galleries (from exhentai/e-hentai via the `hbrowser` package) and recording
their state in an `h2hdb` database. It is published to PyPI and consumed as a
dependency by other projects — it has no CLI entry point or standalone runtime
of its own.

The package has exactly three public exports
(`src/h2hdb_downloader/__init__.py`): `Downloader`, `TagCascadePolicy`, and
`DownloadTurnLostError`, all defined in
`src/h2hdb_downloader/downloader.py`.

All h2hdb access must go through `GalleryQueue._database_operation()`, which
holds h2hdb's cross-process maintenance gate with `timeout_seconds=300`.
Keep browser/network awaits, retry sleeps, and tag traversal outside that
context. A timeout is one wait interval, not a terminal failure; h2hdb logs it
and continues waiting.

Public `deep_download_by_gallery()` and `deep_download_by_gid()` calls remain
single-root download/ingest coordination turns. `drain_queue()` instead groups
up to `download_roots_per_ingest` returned root traversals from one live
snapshot into a bounded turn. Before network work, it repeatedly calls
`claim_download_turn()` until h2hdb reports `READY`, then keeps one asynchronous
heartbeat alive across the entire root or batch. Between batch roots,
`complete_download_request_in_turn()` and
`complete_missing_download_request_in_turn()` persist exact-token results while
retaining `DOWNLOADING`; `_KeepRequest` remains queued. The batch calls
`request_gallery_ingest()` once at the root-count boundary or snapshot
exhaustion and waits for `completed_generation >= turn.generation` before the
next batch. Single-root methods retain `finish_download_turn()` and
`finish_missing_download_turn()` so their final request mutation and handoff
share one transaction. These are short calls through `_database_operation()`;
never hold `database_gate()` or a transaction across a network await.

GID resolution consumes hbrowser's typed `lookup_gid()` result. Only
`ConfirmedGalleryMissing` may create a removed marker; an empty, malformed,
challenge, authentication, navigation, pagination, or bounded-search failure
must raise and keep the durable request. A direct confirmed-missing lookup calls
`complete_missing_download_request(request, gid)`. A coordinated one calls
the single-root `finish_missing_download_turn(...)` or batched
`complete_missing_download_request_in_turn(...)`. Both fence the turn and write
the removed marker only when exact-token deletion succeeds; the batch variant
keeps `DOWNLOADING` for more roots, while the finish variant also hands off. A
newer token fences both missing mutations. A successful `GalleryFound` result
clears stale removed markers for the requested and resolved GIDs before
downloading. Once any handoff for a turn has already committed, later finish
calls are mutation-free idempotent replays.

Another h2hdb process may hold SQLite's exclusive lock during `VACUUM`. Only
`_claim_download_turn()` and `_wait_for_gallery_ingest()` are liveness polling
boundaries: catch `sqlite3.OperationalError` there and retry only when the
primary `sqlite_errorcode` (the extended code masked with `0xFF`) is
`SQLITE_BUSY` or `SQLITE_LOCKED`. Sleep for `turn_poll_seconds` before retrying.
Do not apply this policy to heartbeat renewal, handoff, atomic finish, queue
mutation, browser work, other SQLite errors, or MariaDB exceptions.

The root `DownloadRequest` is completed conditionally only after the root
gallery is resolved and its full related-tag cascade returns successfully.
Single-root calls delete it in the atomic finish operation; batch roots use the
live-turn-fenced in-turn completion immediately after traversal returns. A stale
or expired worker therefore cannot delete the root. If its token was replaced
or is already absent, exact completion is a no-op and must never remove the
newer request. Keep the request queued if the cascade raises, is cancelled, or
the process exits before the traversal returns.

Related downloads must call `ensure_download_request()` so an existing snapshot
root token is reused rather than replaced. Only a request newly created by that
related download may be completed immediately. One `_DownloadBatchContext`
spans the complete `drain_queue()` snapshot: it suppresses duplicate submission
of a GID across overlapping cascades and later batches, while a later queued
root still runs its own cascade before its exact token is completed. A queued
URL's gid-search fallback belongs to the same root traversal. Keep coordinated
public wrappers separate from internal traversal helpers so a cascade never
attempts to claim a nested turn.

Every exceptional root or batch attempts `request_gallery_ingest(turn)`. If
that conditional handoff returns `False`, raise `DownloadTurnLostError` and
preserve the original exception as `__cause__`; never re-raise the original as
though handoff succeeded. Cancellation follows the same path without swallowing
`CancelledError`. SIGTERM, SIGKILL, simultaneous service shutdown, or database
unavailability may prevent the explicit handoff; the durable downloader lease
then expires and h2hdb must recover `DOWNLOADING` before returning to `READY`.
Each completed root is independently committed, the interrupted root remains
queued, and the process-local batch counter may be discarded on restart.

H@H submission is an uncontrollable external side effect. There is necessarily
a crash window after `driver.download()` accepts work and before the root's
database checkpoint commits. A restart may submit that GID again, so this
boundary is at-least-once, not exactly-once; durable request and ingest state
must remain correct without attempting to infer H@H's internal state.

`download_by_gallery()`, `download_by_gid()`, and `download_by_tag()` are
intentionally direct APIs: they neither claim a download turn nor wait for
h2hdb ingest. Callers that require backpressure must use a deep method or
`drain_queue()`.

## Communication

- Claude 必須以繁體中文回答所有對話內容，不論使用者以何種語言提問；
  程式碼、指令、檔名、專有名詞等仍維持原文。

## Build & Development Commands

```bash
# Install dependencies
uv pip install -e .

# Type checking (strict mode configured in mypy.ini)
uv run mypy src/h2hdb_downloader tests

# Linting with ruff (rules in pyproject.toml: E, F, I, UP)
uv run ruff check .

# Formatting with black (88 char line length)
uv run black .
```

## Coding Guidelines

This is a solo, pre-1.0 project with no external consumers pinned to current
APIs. Do not optimize for minimal diffs or backward compatibility:

- Freely rename, restructure, or delete code when it improves the design —
  there are no external callers to break.
- Do not keep deprecated aliases, compatibility shims, or old code paths "just
  in case."
- Prefer the cleanest end state over the smallest diff to get there.

Follow SOLID principles when writing code:

- **Single Responsibility** - Each class/module should have one reason to change
- **Open/Closed** - Open for extension, closed for modification (use
  inheritance/composition)
- **Liskov Substitution** - Subtypes must be substitutable for their base types
- **Interface Segregation** - Prefer small, specific interfaces over large ones
- **Dependency Inversion** - Depend on abstractions (ABC), not concrete
  implementations

## Code Style

- **Sync obligation for tooling configuration:** the IDE save pipeline and the
  Stop hook pipeline are kept in lockstep across the locations below. Any
  change to one of them requires matching updates to the others in the same
  change.

  - Python formatting/lint/type-check:
    [.vscode/settings.json](.vscode/settings.json) (`[python]` block), the
    `[tool.ruff]` section of [pyproject.toml](pyproject.toml),
    [mypy.ini](mypy.ini), and
    [scripts/hooks/finalize-python.sh](scripts/hooks/finalize-python.sh),
    called by both Claude and Codex.
  - Markdown formatting: [.vscode/settings.json](.vscode/settings.json)
    (`[markdown]` block) and
    [scripts/hooks/finalize-markdown.sh](scripts/hooks/finalize-markdown.sh),
    called by both Claude and Codex.
  - Claude registers the shared scripts in
    [.claude/settings.local.json](.claude/settings.local.json); Codex's
    [.Codex/hooks](.Codex/hooks) wrappers forward to those same scripts.
  - Tool versions: the `dev` group of `[project.optional-dependencies]` in
    [pyproject.toml](pyproject.toml) pins `black`, `ruff`, `mypy`, and
    `pymarkdownlnt`. Both the IDE pipeline (when invoked via `uv run`) and the
    Stop hooks resolve to these venv-installed versions, so bumping any of them
    must be done here — not via Homebrew or any other system-wide install.

- Python version range: refer to `requires-python` in
  [pyproject.toml](pyproject.toml)
