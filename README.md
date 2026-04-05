# H@HDB Downloader (h2hdb-downloader)

## Usage

Here's a quick example of how to use H@HDB Downloader:

```python
import asyncio
from h2hdb_downloader import PreLinks, Downloader
from hbrowser import ExHDriver

async def main():
    gallery = GalleryURLParser("https://exhentai.org/g/123/456/")
    prelinks = PreLinks()
    async with ExHDriver(headless=True) as driver:
        downloader = Downloader(driver, prelinks)
        await downloader.download_gallery(gallery)
        await downloader.deep_download_gid(gallery,
            filters=["artist", "group"],
            conditions=["language:chinese$", "language:speechless$"],
            )
        await downloader.download_gid(666) # download gid:666

asyncio.run(main())
```

## License

This project is distributed under the terms of the GNU General Public Licence (GPL). For detailed licence terms, see the `LICENSE` file included in this distribution.
