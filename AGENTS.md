# Agent Instructions

The repository keeps provider-neutral finalization scripts in `scripts/hooks/`.
Do not create separate copies under an individual agent's configuration
directory.

- After changing Python, run `bash scripts/hooks/finalize-python.sh`.
- After changing Markdown, run `bash scripts/hooks/finalize-markdown.sh`.

## Core boundary

- Inject h2hdb's public `DownloadCoordinator` into `Downloader` and
  `GalleryQueue`. Do not construct the core facade or load its configuration in
  this package.
- Use only top-level public h2hdb ports and domain models. Do not import SQL
  connectors, repositories, backend modules, or manage the database gate here.
- Keep browser/network awaits outside synchronous coordinator calls.
- Retry `CoordinatorUnavailableError` only at turn-claim and completed-ingest
  polling boundaries. Backend-specific lock detection belongs to h2hdb core.
- A blank GID in the optional CSV queue is resolved from its URL with
  `GalleryURLParser` before calling the coordinator.
