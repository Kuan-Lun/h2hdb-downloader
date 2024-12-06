# H@HDB Downloader (h2hdb-downloader)

## Usage

Here's a quick example of how to use H@HDB Downloader:

```python
from h2hdb_downloader import PreLinks, Downloader
from hbrowser import ExHDriver

gallery = GalleryURLParser("https://exhentai.org/g/123/456/")
prelinks = PreLinks()
with ExHDriver("username", "password", headless=True) as driver:
    downloader = Downloader(driver, prelinks)
    downloader.download_gallery(gallery)
    downloader.deep_download_gid(gallery,
        filters=["artist", "group"],
        conditions=["language:chinese$", "language:speechless$"],
        )
    downloader.download_gid(666) # download gid:666
```

## License

This project is distributed under the terms of the GNU General Public Licence (GPL). For detailed licence terms, see the `LICENSE` file included in this distribution.
