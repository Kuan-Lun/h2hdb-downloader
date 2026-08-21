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

`Downloader` receives h2hdb's public `VNextDownloadQueueFacade` from its caller;
`GalleryQueue` delegates every durable request, candidate-state, deletion, and
turn operation to that facade. This package must not construct the
core facade, load core configuration, manage `database_gate()`, import a SQL
backend, or reach into a connector or repository. Keep browser/network awaits,
retry sleeps, and tag traversal outside the facade's synchronous calls.

Public `deep_download_by_gallery()` and `deep_download_by_gid()` calls remain
single-root download/ingest coordination turns. `drain_queue()` and
`drain_pending_redownloads()` instead group complete root traversals from one
live snapshot until at least `download_submissions_per_ingest` unique H@H
submissions have been accepted. This is a soft threshold checked only after an
indivisible root and its complete related-tag cascade return; a zero-submission
root does not advance it, and a single root may carry the batch past it. Before
network work, each drain repeatedly calls `claim_download_turn()` until h2hdb
reports `READY`, then keeps one asynchronous heartbeat alive across the entire
root or batch. `download_submissions_per_ingest` is a positive integer and
defaults to 100. Between batch roots,
`complete_download_request_in_turn()` and
`complete_missing_download_request_in_turn()` persist exact-token results while
retaining `DOWNLOADING`; `_KeepRequest` remains queued. The batch calls
`handoff_download_turn()` once at the submission-count boundary or snapshot
exhaustion and polls the returned exact handoff receipt until linked ingest
completes before the next batch. Single-root methods retain
`finish_download_turn()` and `finish_missing_download_turn()` so their final
request mutation and handoff share one core transaction. These are short
`VNextDownloadQueueFacade` calls; never place a browser or network await inside
one.

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

Only `_claim_download_turn()` and `_wait_for_gallery_ingest()` are liveness
polling boundaries. Retry the backend-neutral `DownloadIngestUnavailableError`
there after `turn_poll_seconds`. Backend lock detection and database-gate policy
belong to h2hdb core. Do not apply this retry policy to heartbeat renewal,
handoff, atomic finish, queue mutation, browser work, or unrelated exceptions.

The root `VNextDownloadRequest` is completed conditionally only after the root
gallery is resolved and its full related-tag cascade returns successfully.
Single-root calls delete it in the atomic finish operation; batch roots use the
live-turn-fenced in-turn completion immediately after traversal returns. A stale
or expired worker therefore cannot delete the root. If its token was replaced
or is already absent, exact completion is a no-op and must never remove the
newer request. Keep the request queued if the cascade raises, is cancelled, or
the process exits before the traversal returns.

Related downloads must call `ensure_download_request()` so an existing snapshot
root token is reused rather than replaced. Only a request newly created by that
related download may be completed immediately. Before
`drain_pending_redownloads()` starts any browser or network work, it must walk
the complete pending snapshot through h2hdb's typed keyset pages and call
`ensure_download_request()` for every GID. The first page pins the sealed
catalog/source revisions and time cutoff; every continuation uses the returned
cursor until a terminal page arrives. A nonterminal page may contain no GIDs
when its bounded schedule rows were unmapped or suppressed, so never use item
emptiness as end-of-scan. Keep the returned tokenized requests as the roots
processed by that drain; do not re-read or replace their tokens later. If
seeding is interrupted, roots already ensured are recoverable from the durable
queue, while roots not yet ensured remain in the pending-redownload view. This
pre-seeding also fences an earlier root's related cascade: when it reaches a
later pending GID,
`ensure_download_request()` finds that root's token and the related-download
path must not complete it. The later root therefore still executes its own
cascade before exact-token completion. One `_DownloadBatchContext`
spans the complete snapshot handled by either drain method: it suppresses
duplicate submission and counting of a GID across overlapping cascades and
later batches, while a later queued root still runs its own cascade before its
exact token is completed. The submission counter advances only when
`driver.download()` returns `True` for a previously uncounted GID. A queued
URL's gid-search fallback belongs to the same root traversal. Keep coordinated
public wrappers separate from internal traversal helpers so a cascade never
attempts to claim a nested turn.

Every exceptional root or batch attempts `handoff_download_turn(turn)`. If
that conditional handoff rejects stale authority, raise `DownloadTurnLostError` and
preserve the original exception as `__cause__`; never re-raise the original as
though handoff succeeded. Cancellation follows the same path without swallowing
`CancelledError`. SIGTERM, SIGKILL, simultaneous service shutdown, or database
unavailability may prevent the explicit handoff; the durable downloader lease
then expires and h2hdb must recover `DOWNLOADING` before returning to `READY`.
Each completed root is independently committed, the interrupted root remains
queued, and the process-local accepted-submission counter may be discarded on
restart. Snapshot exhaustion performs a final handoff even when the soft
threshold was not reached; an empty or entirely stale queue snapshot must not
claim a turn. `drain_pending_redownloads()` likewise snapshots once, so work
flagged during that call waits for the next call.

H@H submission is an uncontrollable external side effect. There is necessarily
a crash window after `driver.download()` accepts work and before the root's
database checkpoint commits. A restart may submit that GID again, so this
boundary is at-least-once, not exactly-once; durable request and ingest state
must remain correct without attempting to infer H@H's internal state.

`download_by_gallery()`, `download_by_gid()`, and `download_by_tag()` are
intentionally direct APIs: they neither claim a download turn nor wait for
h2hdb ingest. Callers that require backpressure must use a deep method or
one of the two drain methods.

The optional manual CSV queue is a downloader-owned filesystem adapter. When a
row leaves its GID blank, parse the URL with `GalleryURLParser` and pass the
normalized positive GID to core; URL parsing is not a `VNextDownloadQueueFacade`
responsibility.

## Communication

- Claude 必須以繁體中文回答所有對話內容，不論使用者以何種語言提問；
  程式碼、指令、檔名、專有名詞等仍維持原文。

## Build & Development Commands

```bash
# Install the unpublished multi-repo core, then this package
uv pip install -e ../h2hdb.clone
uv pip install -e ".[dev]"

# Run the full Python finalizer over all project Python files
bash scripts/hooks/finalize-python.sh

# Run the full Markdown finalizer over all project Markdown files
bash scripts/hooks/finalize-markdown.sh

# Linting with ruff (rules in pyproject.toml: E, F, I, UP)
uv run --no-sync ruff check .

# Formatting with black (88 char line length)
uv run --no-sync black .
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
    [.claude/settings.local.json](.claude/settings.local.json). Codex follows
    [AGENTS.md](AGENTS.md), and the [.Codex/hooks](.Codex/hooks) wrappers
    forward to those same scripts.
  - Tool versions: the `dev` group of `[project.optional-dependencies]` in
    [pyproject.toml](pyproject.toml) pins `black`, `ruff`, `mypy`, and
    `pymarkdownlnt`. Both the IDE pipeline (when invoked via
    `uv run --no-sync`) and the
    Stop hooks resolve to these venv-installed versions, so bumping any of them
    must be done here — not via Homebrew or any other system-wide install.

- Python version range: refer to `requires-python` in
  [pyproject.toml](pyproject.toml)
