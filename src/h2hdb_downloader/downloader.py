import csv
import os
from random import random
from time import sleep
from typing import Callable, Any


from h2h_galleryinfo_parser import GalleryURLParser
from h2hdb import H2HDB, load_config
from hbrowser import ExHDriver, Tag


class PreLinks:
    def __init__(self, config_path: str) -> None:
        """
        downloaded_gids: 已下載的 GID (存於資料庫中)
        pending_download_gids: 待重新下載的 GID (存於資料庫中)
        todownload_gids: 待下載的 GID (存於 todownload_gids.csv)
        pass_gids: 已下載且已確認被更新的 GID
        """
        self.config = load_config(config_path)
        self.todownload_gids_filename = os.path.join(".", "todownload_gids.csv")
        self._check_todownload_gids_filename()

        with H2HDB(config=self.config) as connector:
            self.downloaded_gids = connector.get_gids()
            self.pending_download_gids = connector.get_pending_download_gids()
        self.todownload_gids = self.load_todownload_gids()
        self.pass_gids = list(
            set(self.downloaded_gids)
            - set(self.pending_download_gids)
            - {gpair[0] for gpair in self.todownload_gids}
        )

    def _new_todownload_gids_filename(self) -> None:
        with open(
            self.todownload_gids_filename,
            mode="w",
            newline="",
            encoding="utf-8",
        ) as file:
            writer = csv.writer(file)
            writer.writerow(["gid", "url"])

    def _check_todownload_gids_filename(self) -> None:
        if not os.path.exists(self.todownload_gids_filename):
            self._new_todownload_gids_filename()

    def load_todownload_gids(self) -> list[tuple[int, str]]:
        with H2HDB(config=self.config) as connector:
            with open(
                self.todownload_gids_filename, mode="r", newline="", encoding="utf-8"
            ) as file:
                reader = csv.reader(file)
                for i, row in enumerate(reader):
                    if i == 0:
                        continue
                    if row[0] == "":
                        connector.insert_todownload_gid(0, str(row[1]))
                    else:
                        connector.insert_todownload_gid(int(row[0]), str(row[1]))
        self._new_todownload_gids_filename()
        with H2HDB(config=self.config) as connector:
            todownload_gids = connector.get_todownload_gids()
        self._check_todownload_gids_filename()
        return todownload_gids

    def remove_todownload_gid(self, gid: int) -> None:
        with H2HDB(config=self.config) as connector:
            connector.remove_todownload_gid(gid)

    def renew(self):
        self.load_todownload_gids()

    def append(self, gid: int) -> None:
        if gid not in self.downloaded_gids:
            self.downloaded_gids.append(gid)
        if gid in self.pending_download_gids:
            self.pending_download_gids.remove(gid)
        for gpair in self.todownload_gids:
            if gid == gpair[0]:
                self.todownload_gids.remove(gpair)
                self.remove_todownload_gid(gid)
                break
        self.pass_gids.append(gid)


def merged_downloaded_galleries(
    dict1: dict[GalleryURLParser, bool], dict2: dict[GalleryURLParser, bool]
) -> dict[GalleryURLParser, bool]:
    merged_dict = {
        key: dict1.get(key, False) or dict2.get(key, False)
        for key in set(dict1) | set(dict2)
    }
    return merged_dict


class Downloader:
    def __init__(self, driver: ExHDriver, prelinks: PreLinks) -> None:
        self.driver = driver
        self.prelinks = prelinks
        self.download: list[int] = list()
        self.remove: list[int] = list()
        self.new: list[int] = list()
        self.wocount = 0
        self._reset_wocount()

    def _reset_wocount(self) -> None:
        self.wocount = 0
        self.wocount_max = int(19 * random()) + 1

    def _clear_todownload_gids(
        self, download_pairs: dict[str, tuple[Callable, dict[str, Any]]]
    ) -> dict[GalleryURLParser, bool]:
        gb = dict[GalleryURLParser, bool]()
        with H2HDB(config=self.prelinks.config) as connector:
            gpair = connector.get_todownload_gids()

        # Case 1: GIDs with URL
        galleries_withurl = [GalleryURLParser(url=gpair[1]) for gpair in gpair]
        for gallery in galleries_withurl:
            gb[gallery] = download_pairs["download_gallery"][0](
                gallery, **download_pairs["download_gallery"][1]
            )
            with H2HDB(config=self.prelinks.config) as connector:
                connector.remove_todownload_gid(gallery.gid)

        # Case 2: GIDs without URL
        gids_withouturl = [gpair[0] for gpair in gpair if gpair[1] == ""]
        for gid in gids_withouturl:
            gb = merged_downloaded_galleries(
                gb,
                download_pairs["download_gid"][0](
                    gallery, **download_pairs["download_gid"][1]
                ),
            )
            with H2HDB(config=self.prelinks.config) as connector:
                connector.remove_todownload_gid(gid)

        return gb

    def clear_todownload_gids(self) -> dict[GalleryURLParser, bool]:
        deep_download_parameters = dict[str, Any]()
        download_pairs = dict[str, tuple[Callable, dict[str, Any]]]()
        download_pairs["download_gallery"] = (
            self.download_gallery,
            deep_download_parameters,
        )
        download_pairs["download_gid"] = (
            self.download_gid,
            deep_download_parameters,
        )
        return self._clear_todownload_gids(download_pairs)

    def deep_clear_todownload_gids(
        self,
        filters: list[str],
        conditions: list[str],
        skip_check: bool,
    ) -> dict[GalleryURLParser, bool]:
        deep_parameters = dict[str, Any](
            filters=filters, conditions=conditions, skip_check=skip_check
        )
        download_pairs = dict[str, tuple[Callable, dict[str, Any]]]()
        download_pairs["download_gallery"] = (
            self.deep_download_gallery,
            deep_parameters,
        )
        download_pairs["download_gid"] = (
            self.deep_download_gid,
            deep_parameters,
        )
        return self._clear_todownload_gids(download_pairs)

    def download_gallery(self, gallery: GalleryURLParser) -> bool:
        with H2HDB(config=self.prelinks.config) as connector:
            connector.insert_todownload_gid(gallery.gid, gallery.url)
        if (gallery.gid not in self.prelinks.pass_gids) or (
            self.wocount > self.wocount_max
        ):
            if self.driver.download(gallery):
                with H2HDB(config=self.prelinks.config) as connector:
                    if connector.check_gid_by_gid(gallery.gid):
                        connector.update_redownload_time_to_now_by_gid(gallery.gid)
                self.prelinks.append(gallery.gid)
                self.download.append(gallery.gid)
                sleep(random())
                self._reset_wocount()
                isdownloaded = True
            else:
                self.wocount += 1
                isdownloaded = False
        else:
            self.wocount += 1
            isdownloaded = False
        with H2HDB(config=self.prelinks.config) as connector:
            connector.remove_todownload_gid(gallery.gid)
        return isdownloaded

    def _download_gid(
        self, gid: int, download_pair: tuple[Callable, dict[str, Any]]
    ) -> dict[GalleryURLParser, bool]:
        gb = dict[GalleryURLParser, bool]()
        galleries = self.driver.search(f"gid:{gid}", isclear=True)
        match len(galleries):
            case 0:
                with H2HDB(config=self.prelinks.config) as connector:
                    connector.insert_removed_gallery_gid(gid)
            case 1:
                gallery = galleries[0]
                gb[gallery] = download_pair[0](gallery, **download_pair[1])
                if gallery.gid != gid:
                    with H2HDB(config=self.prelinks.config) as connector:
                        if connector.check_gid_by_gid(gallery.gid):
                            connector.insert_todelete_gid(gid)
            case _:
                raise ValueError("There can only be one gallery or none.")
        return gb

    def download_gid(self, gid: int) -> dict[GalleryURLParser, bool]:
        return self._download_gid(gid, (self.download_gallery, dict[str, Any]()))

    def deep_download_gid(
        self, gid: int, filters: list[str], conditions: list[str], skip_check: bool
    ) -> dict[GalleryURLParser, bool]:
        deep_parameters = dict[str, Any](
            filters=filters, conditions=conditions, skip_check=skip_check
        )
        return self._download_gid(gid, (self.deep_download_gallery, deep_parameters))

    def download_galleries(
        self, galleries: list[GalleryURLParser]
    ) -> dict[GalleryURLParser, bool]:
        gb = dict[GalleryURLParser, bool]()
        for gallery in galleries:
            gb[gallery] = self.download_gallery(gallery)
        return gb

    def download_tag(
        self, tag: Tag, conditions: list[str]
    ) -> dict[GalleryURLParser, bool]:
        gb = dict[GalleryURLParser, bool]()
        if len(conditions) == 0:
            self.driver.get(tag.href)
            galleries = self.driver.search("", isclear=False)
            gb = merged_downloaded_galleries(gb, self.download_galleries(galleries))
        else:
            for condition in conditions:
                self.driver.get(tag.href)
                galleries = self.driver.search(condition, isclear=False)
                gb = merged_downloaded_galleries(gb, self.download_galleries(galleries))
        return gb

    def deep_download_gallery(
        self,
        gallery: GalleryURLParser,
        filters: list[str],
        conditions: list[str],
        skip_check: bool,
    ) -> dict[GalleryURLParser, bool]:
        """
        Example
        g = GalleryURLParser("https://exhentai.org/g/xxxx/xxxx/")
        deep_download_gallery(g, ["artist", "group"], ["language:chinese$", "language:speechless$"])
        """
        gb = dict[GalleryURLParser, bool]()
        if self.download_gallery(gallery) or skip_check:
            for filter in filters:
                taglist: list[Tag] = self.driver.gallery2tag(gallery, filter=filter)
                for tag in taglist:
                    gb = merged_downloaded_galleries(
                        gb, self.download_tag(tag, conditions)
                    )
        return gb
