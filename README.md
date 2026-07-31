# H2HDB Downloader (h2hdb-downloader)

Automates downloading galleries from exhentai/e-hentai (via `hbrowser`) and
recording their state in an `h2hdb` database. It has no CLI or standalone
runtime of its own — it's a library consumed by another project that owns
the browser session and the overall process lifecycle.

## Concepts

- **Gallery** — a single exhentai/e-hentai gallery, identified by a `gid`
  (numeric id) and represented as `h2h_galleryinfo_parser.GalleryURLParser`
  once its URL is known.
- **Dedup** — before issuing a real network download, the package reads live
  h2hdb state to see if the gid is already settled (downloaded, with no
  redownload flag or durable request). Settled gids are skipped — except
  periodically, at a random interval (1 to 19 attempts), when one is
  force-redownloaded as an integrity re-check.
- **Durable requests** — immediately before a real download starts, the
  package creates a tokenized request in h2hdb's `todownload_gids` table.
  It conditionally completes that exact token only after success. A `False`
  result, exception, cancellation, or process termination leaves resumable
  work behind, while a newer request for the same gid cannot be erased by
  an older attempt finishing late. h2hdb also uses this table to publish a
  redownload request after all active deletion-candidate folders for a gid
  have actually disappeared. For a deep root job, success means that the
  root gallery has resolved and its entire related-tag cascade has returned
  successfully; the root request remains queued throughout that traversal.
  Its exact-token deletion and download-to-ingest handoff then occur in one
  atomic h2hdb transaction. A late worker that has lost its turn therefore
  cannot delete the recovered root request. If another caller has replaced
  the request token while the turn is still valid, that newer request remains
  queued while the completed turn still hands off successfully. A GID is
  recorded as removed only from hbrowser's explicit
  `ConfirmedGalleryMissing` result, never by interpreting an empty or
  malformed page. For a coordinated root, the removed marker, exact-token
  deletion, and fenced handoff are one transaction. The marker and deletion
  occur only if that exact request token is still current; a newer request
  fences both mutations.
- **Database coordination** — every short h2hdb read/write section enters
  h2hdb's cross-process maintenance gate with a five-minute wait interval.
  Browser search, downloads, retry sleeps, and tag traversal stay outside the
  gate, so maintenance is never blocked by network work.
- **Ingest backpressure** — each public deep-download root claims h2hdb's
  durable download turn before doing network work. An asynchronous heartbeat
  renews its lease while the complete related-tag cascade runs. On success,
  a resolved root atomically finishes its exact request and requests ingest.
  A confirmed-missing root atomically records the removed marker, finishes
  its exact request, and requests ingest only while that token remains current;
  the handoff still succeeds without those mutations when a newer token exists.
  An unresolved, failed, or cancelled root requests ingest without removing its
  durable request. Once this generic handoff commits, a later finish replay
  cannot convert it into a success or missing mutation. If the exception
  handoff is rejected because the turn was lost, the downloader raises
  `DownloadTurnLostError` with the original failure as its cause. After a normal
  root return, the downloader waits until h2hdb has completed that turn's
  generation before another root may start. If the process is killed, the lease
  expires so h2hdb can ingest files already written to disk. This coordination
  is logical state made of short database calls: it never holds a transaction
  or database maintenance gate across browser work. SQLite may temporarily
  report `BUSY` or `LOCKED` while another h2hdb process holds the exclusive lock
  needed by `VACUUM`; only the ready-turn claim and completed-generation polling
  boundaries retry those lock codes at `turn_poll_seconds`. Other SQLite errors
  and all non-polling operation failures still propagate immediately.
- **Manual queue** — add a `(gid, url)` row to the CSV configured by
  `csv_path`. It is converted into the same durable request and picked up
  the next time the queue is drained. Before replay, the inbox is atomically
  rotated to a same-directory hidden claim file; interrupted claims are
  replayed automatically on the next run.
- **Deep download** — download a gallery, then look at its `artist`/`group`
  tags and download sibling galleries that match a set of search conditions
  (e.g. other-language releases of the same work).

## API

`Downloader` is the public service object. `TagCascadePolicy` and
`DownloadTurnLostError` are the other public exports. Every method either acts
on a target you explicitly pass in or, for the two queue-reading methods below,
hands back a plain value with no further bookkeeping required from you.
There is no "run the whole thing" method: deciding when to stop, what order
to process things in, and how to report progress is the calling
application's job, not the library's.

```python
Downloader(
    driver: ExHDriver,         # an un-entered driver; see below
    config_path: str,          # path to the h2hdb JSON config
    csv_path: str | None = None,  # path to the manual download-queue CSV
    *,
    wait4client: int,       # seconds to wait before retrying after ClientOfflineException
    retry2download: int,    # seconds to wait before retrying after InsufficientFundsException
    turn_poll_seconds: float = 5,       # wait interval for a turn / ingest completion
    turn_lease_seconds: int = 300,      # recoverable ownership lease
    turn_heartbeat_seconds: float = 60, # renewal interval; shorter than the lease
)
```

`csv_path` only enables the optional "queue a gid/url by editing a CSV file"
feature described above. Leave it as `None` if you don't need that; durable
database requests and live deduplication still work.

The turn timing defaults normally need no adjustment. All three values must be
positive and finite, `turn_lease_seconds` must be an integer, and the heartbeat
interval must be shorter than the lease.

Coordinated methods raise `DownloadTurnLostError` if their lease can no longer
be renewed or the conditional handoff proves that another process owns the
turn. Callers may catch it separately from browser/download failures; the
durable root request for unfinished work remains available for a later retry.

`Downloader` is itself an async context manager that opens and closes the
browser session for you, so `driver` is expected un-entered:

```python
async with Downloader(ExHDriver(headless=False), ...) as downloader:
    ...
```

If you'd rather manage the driver's lifecycle yourself, pass an
already-entered driver and skip `async with downloader`.

Method names follow one rule throughout: no suffix means it operates
directly on a `GalleryURLParser` you already have; `_by_gid` means it
resolves a bare gid through hbrowser's exact typed lookup first, then does the same
thing.

- `await download_by_gallery(target)` — download one `GalleryURLParser`, or
  an iterable of them. Returns `{gid: downloaded}` for each. Retries
  automatically on `ClientOfflineException` (waits `wait4client` seconds)
  and `InsufficientFundsException` (waits `retry2download` seconds); a wait
  of `0` means "don't retry, raise immediately." This is a direct API and
  does not claim a download turn or wait for h2hdb ingest.
- `await download_by_gid(gid)` — resolve a bare gid through hbrowser's exact
  lookup, then download it. Only an explicit, independently confirmed missing
  result is recorded as removed in h2hdb; challenge, authentication, malformed,
  pagination, navigation, and bounded-search failures raise and leave the
  request retryable. A later successful lookup clears any stale removed
  marker. If the gid resolves to a *different* gid (the gallery was
  merged/redirected), the original gid is flagged for deletion after the
  replacement downloads successfully. This is also a direct, uncoordinated
  API.
- `await download_by_tag(tag, conditions)` — download every gallery under a
  `hbrowser` `Tag`, once per search condition in `conditions` (or
  unconditionally if `conditions` is empty). This is also a direct,
  uncoordinated API.
- `await deep_download_by_gallery(gallery, policy, skip_check=False)` —
  download `gallery`, then for each tag in `policy.filters` (e.g.
  `"artist"`, `"group"`) on that gallery, call `download_by_tag` with
  `policy.conditions`. The cascade only runs if the initial download
  actually happened, unless `skip_check=True` forces it to run regardless
  (useful when you already know the gallery is downloaded from a separate
  call and just want the cascade). `policy` is a
  `TagCascadePolicy(filters, conditions)` — both fields always travel
  together, so they're grouped into one frozen value object rather than two
  parallel parameters. The whole call is one coordinated root: it claims a
  turn, keeps its durable root request until the cascade finishes, atomically
  finishes the exact request while handing the turn to h2hdb, and waits for
  that generation to be ingested.
- `await deep_download_by_gid(gid, policy, skip_check=False)` — same
  gid-resolution as `download_by_gid`, but deep and coordinated as one root.
- `await drain_queue(policy, skip_check=True)` — absorb the manual CSV and
  process one live snapshot of durable database requests. A request is
  removed only after a successful root and complete related-tag cascade,
  confirmed removal, or successful redirect. Each request receives its own
  download turn and h2hdb ingest wait before the next snapshot entry begins.
  A URL-to-gid fallback remains part of the same turn. The method doesn't loop
  for newly queued work after its snapshot.
- `pending_redownload_gids()` — a snapshot list of gids h2hdb currently
  flags as needing a periodic redownload. Every call reads live database
  state; read-only and safe to call repeatedly as you work through it.

## Example

The calling application owns the loop. A typical one drains the queue once,
then walks the pending-redownload list, deep-downloading anything that actually
got (re)downloaded. The queue drain and each deep download in that pending loop
apply ingest backpressure between root jobs:

```python
import asyncio
from h2hdb_downloader import Downloader, TagCascadePolicy
from hbrowser import ExHDriver
from h2h_galleryinfo_parser import GalleryURLParser

policy = TagCascadePolicy(
    filters=("artist", "group"),
    conditions=("language:chinese$", "language:speechless$"),
)


async def main():
    async with Downloader(
        ExHDriver(headless=True),
        config_path="h2hdb-config.json",
        csv_path="todownload_gids.csv",
        wait4client=30 * 60,
        retry2download=4 * 60 * 60,
    ) as downloader:
        gallery = GalleryURLParser("https://exhentai.org/g/123/456/")
        await downloader.download_by_gallery(gallery)
        await downloader.download_by_gid(666)
        await downloader.deep_download_by_gallery(gallery, policy)

        await downloader.drain_queue(policy, skip_check=True)
        for gid in downloader.pending_redownload_gids():
            await downloader.deep_download_by_gid(gid, policy, skip_check=True)


asyncio.run(main())
```

## License

This project is distributed under the terms of the GNU General Public Licence
(GPL). For detailed licence terms, see the `LICENSE` file included in this
distribution.
