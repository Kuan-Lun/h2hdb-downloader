# H2HDB Downloader

H2HDB Downloader submits E-Hentai and ExHentai galleries to H@H and keeps
resumable download requests in an h2hdb database. It can process a manual CSV
queue, revisit galleries marked for redownload, and download related works by
artist or group.

This is a Python library. It has no standalone command-line application or
background service: use it from a Python program that chooses what to download
and when to run. Browser access comes from `hbrowser`; an h2hdb ingest process
handles the files delivered by H@H.

## Requirements and installation

- Python 3.14 or newer.
- An E-Hentai account with access to the site you use, an available H@H client,
  and any funds required for archive downloads.
- A configured h2hdb database and a running ingest process for coordinated
  downloads. This package does not create or upgrade the database.
- A working browser environment supported by
  [HBrowser](https://github.com/Kuan-Lun/hbrowser#readme).

Install into your Python environment:

```bash
python -m pip install h2hdb-downloader
```

To install this checkout instead, run `python -m pip install .` from its root.
The package declares compatible `h2hdb` and `hbrowser` dependency versions;
let the installer resolve them together. This release uses h2hdb schema version
7 (epoch 3). Follow the [h2hdb setup instructions](https://github.com/Kuan-Lun/h2hdb#readme)
to prepare the database and configuration file before running the example.

Set browser credentials in your script's environment. For Bash or Zsh:

```bash
export EH_USERNAME='your_username'
export EH_PASSWORD='your_password'
export USE_TOR=0
```

For PowerShell:

```powershell
$env:EH_USERNAME = 'your_username'
$env:EH_PASSWORD = 'your_password'
$env:USE_TOR = '0'
```

`USE_TOR=0` selects a direct connection. If omitted, HBrowser may use a detected
Tor installation. Keep credentials out of your Python files and version
control. Start with a visible browser so you can handle login challenges;
unattended setup and optional FlareSolverr configuration are described in the
HBrowser README.

## Process your download queue

Save this as `download_queue.py` beside your configured `h2hdb-config.json`,
then run `python download_queue.py`:

```python
import asyncio

from h2hdb import VNextDownloadQueueFacade, load_config
from h2hdb_downloader import Downloader, TagCascadePolicy
from hbrowser import ExHDriver


async def main() -> None:
    facade = VNextDownloadQueueFacade(load_config("h2hdb-config.json"))
    try:
        async with Downloader(
            ExHDriver(headless=False),
            facade=facade,
            csv_path="todownload_gids.csv",
            wait4client=30 * 60,
            retry2download=4 * 60 * 60,
            download_submissions_per_ingest=100,
        ) as downloader:
            # Empty filters process only the galleries you queued.
            policy = TagCascadePolicy(filters=(), conditions=())
            results = await downloader.drain_queue(policy)
            for gid, submitted in results.items():
                print(gid, submitted)
    finally:
        facade.close()


if __name__ == "__main__":
    asyncio.run(main())
```

`Downloader` opens and closes the browser session. Pass it a driver that has
not already been entered. If your application already manages an active driver,
pass that driver and call downloader methods without entering a second context.

The example processes one snapshot of queued work, waits for ingest after each
batch, and exits. New requests added after that snapshot wait for the next run.
A returned `True` means the gallery submission was accepted; it is not a local
file path. `False` means no submission succeeded in that operation, including
cases where a gallery was skipped or unavailable.

## Add galleries with a CSV file

Create `todownload_gids.csv` with the following two columns. Replace the sample
IDs and URL with your targets:

```csv
gid,url
123,https://exhentai.org/g/123/456/
666,
,https://exhentai.org/g/789/abc/
```

- A GID alone is looked up through the browser.
- A URL alone supplies its GID automatically.
- When both are present, they must identify the same gallery.
- Each row must have exactly two fields. GIDs must be positive integers.

The CSV is an inbox: imported rows move into the database queue and disappear
from the inbox. A missing inbox file is created automatically; its parent
directory must already exist. Hidden files named `.todownload_gids.csv.claim-*`
are interrupted imports and are replayed automatically on the next run. Keep
those files until replay succeeds. A malformed row raises an error; correct the
reported inbox or claim file before trying again.

Set `csv_path=None` if you only use requests already in the database.

## Download related works and redownloads

To follow the queued gallery's artist and group tags, replace the empty policy
with:

```python
policy = TagCascadePolicy(
    filters=("artist", "group"),
    conditions=("language:chinese$", "language:speechless$"),
)
```

Each condition runs as a separate search under each matching tag. Empty
`conditions` means no additional search restriction, so check the policy before
starting a potentially large download.

Use these methods inside the active downloader context:

| Task | Call |
| --- | --- |
| Process the current durable and CSV queue | `await downloader.drain_queue(policy)` |
| Process the current pending-redownload list | `await downloader.drain_pending_redownloads(policy)` |
| Download one GID and its related works | `await downloader.deep_download_by_gid(gid, policy)` |
| Download a known gallery URL and related works | `await downloader.deep_download_by_gallery(gallery, policy)` |
| Inspect pending redownload IDs without downloading | `downloader.pending_redownload_gids()` |

For URL-based methods, construct `gallery` with
`h2h_galleryinfo_parser.GalleryURLParser(url)`. Deep methods normally follow
related tags only when the root gallery was downloaded. Pass `skip_check=True`
to follow its tags even if the root was already downloaded; queue-draining
methods use this setting by default.

Coordinated methods take turns with ingest and wait for it to finish. With the
default `download_submissions_per_ingest=100`, a queue batch hands off after at
least 100 unique submissions or when its snapshot is exhausted. The current
gallery and all its related downloads finish before that threshold is checked,
so a batch can exceed 100. This setting counts accepted submissions, not
completed files or elapsed time.

For applications that manage their own ingest scheduling,
`download_by_gallery(gallery)`, `download_by_gid(gid)`, and
`download_by_tag(tag, conditions)` submit directly without claiming a download
turn or waiting for ingest. Prefer the coordinated methods above when sharing
a database with ingest.

## Retry settings and recovery

The two required retry settings are in seconds:

| Setting | Effect |
| --- | --- |
| `wait4client` | Delay before retrying when the H@H client is offline. Use `0` to raise immediately. |
| `retry2download` | Delay before retrying when the account has insufficient funds. Use `0` to raise immediately. |
| `turn_poll_seconds` | Interval while waiting for a download turn or ingest completion; default `5`. |
| `turn_lease_seconds` | Recoverable download-turn lease; default `300`. |
| `turn_heartbeat_seconds` | Lease renewal interval; default `60`, and must be shorter than the lease. |

The turn timing values must be positive and finite; the lease and
`download_submissions_per_ingest` must be positive integers. Usually the defaults
need no adjustment.

Interrupted or failed queued work remains available for a later run. Only a
confirmed missing-gallery result marks a gallery removed; login, challenge,
search, or navigation errors are failures to retry after their cause is fixed.
Already downloaded galleries are usually skipped unless requested again or
marked for redownload; periodic rechecks can still submit a settled gallery.

H@H may accept a request immediately before the program stops. Restarting can
therefore submit that gallery again. Submission is at least once; the library
does not promise that a gallery is submitted exactly once.

| Symptom | What to do |
| --- | --- |
| Waiting indefinitely for a turn or ingest | Check that the ingest process is running against the same database and is making progress. |
| `DownloadTurnLostError` | Stop the current operation; unfinished queued work can be retried in a later run. Check for competing workers or delayed lease renewal. |
| Login or challenge error | Retry with `headless=False` and verify the account, route, and HBrowser settings. |
| H@H offline or insufficient funds | Restore the client or balance, or set the corresponding retry delay to `0` to handle the error in your application. |
| CSV parsing error | Check the two-column format, positive GIDs, and URL/GID agreement, including any retained claim file. |
| Database schema rejected | Follow h2hdb's setup or offline upgrade instructions. Downloader does not upgrade an existing database. |

For browser diagnostics and optional logging, see the HBrowser README. When
reporting a problem, include package versions, the failing operation, and a
sanitized exception; do not include credentials or private account pages.
Report issues through the
[issue tracker](https://github.com/Kuan-Lun/h2hdb-downloader/issues).

## License

Licensed under GPL-3.0-only. See [LICENSE](LICENSE).
