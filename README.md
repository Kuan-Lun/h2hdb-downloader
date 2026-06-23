# H2HDB Downloader (h2hdb-downloader)

Automates downloading galleries from exhentai/e-hentai (via `hbrowser`) and
recording their state in an `h2hdb` database. It has no CLI or standalone
runtime of its own — it's a library consumed by another project that owns
the browser session and the overall process lifecycle.

## Concepts

- **Gallery** — a single exhentai/e-hentai gallery, identified by a `gid`
  (numeric id) and represented as `h2h_galleryinfo_parser.GalleryURLParser`
  once its URL is known.
- **Dedup** — before issuing a real network download, the package checks
  h2hdb to see if the gid is already settled (downloaded, and not flagged
  for redownload). Settled gids are skipped — except periodically, at a
  random interval (1 to 19 attempts), when one is force-redownloaded anyway
  as an integrity re-check.
- **Durable queue** — every download attempt is logged to the h2hdb
  `todownload_gids` table *before* it starts and cleared *after* it
  finishes, so a process killed mid-download leaves a trace that gets
  retried on the next run instead of silently disappearing. The same table
  doubles as a manual work queue: drop a `(gid, url)` row into the CSV file
  you configure as `csv_path` and it will be picked up the next time the
  queue is drained.
- **Deep download** — download a gallery, then look at its `artist`/`group`
  tags and download sibling galleries that match a set of search conditions
  (e.g. other-language releases of the same work).

## API

`Downloader` is the sole public export. Every method either acts on a
target you explicitly pass in, or — for the two queue-reading methods below
— hands back a plain value with no further bookkeeping required from you.
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
)
```

`csv_path` only enables the optional "queue a gid/url by editing a CSV file"
feature described above — leave it as `None` if you don't need that; the
durable in-flight log and dedup cache work identically either way.

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
resolves a bare gid to its gallery via search first, then does the same
thing.

- `await download_by_gallery(target)` — download one `GalleryURLParser`, or
  an iterable of them. Returns `{gallery: downloaded}` for each. Retries
  automatically on `ClientOfflineException` (waits `wait4client` seconds)
  and `InsufficientFundsException` (waits `retry2download` seconds); a wait
  of `0` means "don't retry, raise immediately."
- `await download_by_gid(gid)` — resolve a bare gid to its gallery via
  search, then download it. If the gid no longer resolves to anything, it's
  recorded as removed in h2hdb; if it resolves to a *different* gid (the
  gallery was merged/redirected), the original gid is flagged for deletion.
  Either way, `gid` is fully settled in the pending-redownload queue before
  this returns — callers never need to do that bookkeeping themselves.
- `await download_by_tag(tag, conditions)` — download every gallery under a
  `hbrowser` `Tag`, once per search condition in `conditions` (or
  unconditionally if `conditions` is empty).
- `await deep_download_by_gallery(gallery, policy, skip_check=False)` —
  download `gallery`, then for each tag in `policy.filters` (e.g.
  `"artist"`, `"group"`) on that gallery, call `download_by_tag` with
  `policy.conditions`. The cascade only runs if the initial download
  actually happened, unless `skip_check=True` forces it to run regardless
  (useful when you already know the gallery is downloaded from a separate
  call and just want the cascade). `policy` is a
  `TagCascadePolicy(filters, conditions)` — both fields always travel
  together, so they're grouped into one frozen value object rather than two
  parallel parameters.
- `await deep_download_by_gid(gid, policy, skip_check=False)` — same
  gid-resolution as `download_by_gid`, but deep.
- `await drain_queue(policy, skip_check=True)` — process everything
  currently queued *right now*: anything queued manually via the CSV, plus
  anything left in-flight by a previous interrupted run. Doesn't loop or
  wait for more — it's a single, bounded pass over a snapshot.
- `pending_redownload_gids()` — a snapshot list of gids h2hdb currently
  flags as needing a redownload. Read-only; safe to call repeatedly as you
  work through it.

## Example

The calling application owns the loop. A typical one drains the queue once,
then walks the pending-redownload list, deep-downloading anything that
actually got (re)downloaded:

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
            gb = await downloader.download_by_gid(gid)
            for downloaded_gallery, downloaded in gb.items():
                if downloaded:
                    await downloader.deep_download_by_gallery(
                        downloaded_gallery, policy, skip_check=True
                    )


asyncio.run(main())
```

## License

This project is distributed under the terms of the GNU General Public Licence (GPL). For detailed licence terms, see the `LICENSE` file included in this distribution.
