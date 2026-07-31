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

Public `deep_download_by_gallery()` and `deep_download_by_gid()` calls are
download/ingest coordination roots. `drain_queue()` gives every root request in
its live snapshot a separate turn. Before network work, a root repeatedly calls
`claim_download_turn()` until h2hdb reports that downloading is ready. It
renews the lease in an asynchronous heartbeat, requests gallery ingest on every
exit, and waits for `completed_generation >= turn.generation` before beginning
another root after a normal return. A normally resolved root must call
`finish_download_turn(turn, request)`, which atomically performs the explicit
handoff and exact-token request deletion. An unresolved normal return,
exception, or cancellation calls `request_gallery_ingest(turn)` without
deleting the root. These are short calls through `_database_operation()`; never
hold `database_gate()` or a transaction across a network await.

GID resolution consumes hbrowser's typed `lookup_gid()` result. Only
`ConfirmedGalleryMissing` may create a removed marker; an empty, malformed,
challenge, authentication, navigation, pagination, or bounded-search failure
must raise and keep the durable request. A direct confirmed-missing lookup calls
`complete_missing_download_request(request, gid)`. A coordinated one calls
`finish_missing_download_turn(turn, request, gid)`, which fences the turn,
records the handoff, and—only if that exact request token remains
current—inserts the removed marker and deletes the request in one transaction.
A newer token fences both missing mutations while the valid turn still hands
off. A successful `GalleryFound` result clears stale removed markers for the
requested and resolved GIDs before downloading. Once any handoff for a turn has
already committed, later finish calls are mutation-free idempotent replays.

Another h2hdb process may hold SQLite's exclusive lock during `VACUUM`. Only
`_claim_download_turn()` and `_wait_for_gallery_ingest()` are liveness polling
boundaries: catch `sqlite3.OperationalError` there and retry only when the
primary `sqlite_errorcode` (the extended code masked with `0xFF`) is
`SQLITE_BUSY` or `SQLITE_LOCKED`. Sleep for `turn_poll_seconds` before retrying.
Do not apply this policy to heartbeat renewal, handoff, atomic finish, queue
mutation, browser work, other SQLite errors, or MariaDB exceptions.

The root `DownloadRequest` is completed conditionally only after the root
gallery is resolved and its full related-tag cascade returns successfully.
Do not complete it before handoff: only the atomic finish operation may delete
it. A stale or expired worker's failed finish therefore leaves the root queued.
If the turn is valid but the root token was replaced or is already absent,
finish still hands off successfully and treats exact-token deletion as a no-op;
it must never remove the newer request.
Keep it queued if the cascade raises, is cancelled, or the process exits, so a
later `drain_queue()` can repeat the root traversal. Related galleries may use
their own requests normally. A queued URL's gid-search fallback belongs to the
same root turn. Keep coordinated public wrappers separate from internal
traversal helpers so a cascade never attempts to claim a nested turn.

Every exceptional root attempts `request_gallery_ingest(turn)`. If that
conditional handoff returns `False`, raise `DownloadTurnLostError` and preserve
the original exception as `__cause__`; never re-raise the original as though
handoff succeeded.

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
