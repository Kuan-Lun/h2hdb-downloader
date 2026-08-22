# Agent Instructions

## Branch Policy

- Do not create a development branch or switch to any non-primary branch.
- Make all development changes directly on `master`.

The repository keeps provider-neutral finalization scripts in `scripts/hooks/`.
Do not create separate copies under an individual agent's configuration
directory.

- After changing Python, run `bash scripts/hooks/finalize-python.sh`.
- After changing Markdown, run `bash scripts/hooks/finalize-markdown.sh`.

## Core boundary

- Inject h2hdb's public `VNextDownloadQueueFacade` into `Downloader` and
  `GalleryQueue`. Do not construct the core facade or load its configuration in
  this package.
- Use only top-level public h2hdb ports and domain models. Do not import SQL
  connectors, repositories, backend modules, or manage the database gate here.
- Keep browser/network awaits outside synchronous facade calls.
- Retry `DownloadIngestUnavailableError` only at turn-claim and completed-ingest
  polling boundaries. Backend-specific lock detection belongs to h2hdb core.
- A blank GID in the optional CSV queue is resolved from its URL with
  `GalleryURLParser` before calling the facade.
