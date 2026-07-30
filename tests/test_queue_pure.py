from h2hdb_downloader._queue import (
    ManualDownloadRequest,
    parse_todownload_csv_rows,
    random_wocount_max,
    should_attempt_download,
)


def test_parse_todownload_csv_rows_blank_gid_becomes_zero() -> None:
    rows = [["", "https://exhentai.org/g/1/abc/"], ["42", ""]]
    assert parse_todownload_csv_rows(rows) == [
        ManualDownloadRequest(0, "https://exhentai.org/g/1/abc/"),
        ManualDownloadRequest(42, ""),
    ]


def test_should_attempt_download_skips_settled_gid() -> None:
    assert (
        should_attempt_download(
            is_downloaded=True,
            is_pending=False,
            is_requested=False,
            wocount=0,
            wocount_max=5,
        )
        is False
    )


def test_should_attempt_download_attempts_missing_gid() -> None:
    assert should_attempt_download(
        is_downloaded=False,
        is_pending=False,
        is_requested=False,
        wocount=0,
        wocount_max=5,
    )


def test_should_attempt_download_attempts_pending_or_requested_gid() -> None:
    assert should_attempt_download(
        is_downloaded=True,
        is_pending=True,
        is_requested=False,
        wocount=0,
        wocount_max=5,
    )
    assert should_attempt_download(
        is_downloaded=True,
        is_pending=False,
        is_requested=True,
        wocount=0,
        wocount_max=5,
    )


def test_should_attempt_download_forces_reverify_past_wocount_max() -> None:
    assert should_attempt_download(
        is_downloaded=True,
        is_pending=False,
        is_requested=False,
        wocount=6,
        wocount_max=5,
    )


def test_random_wocount_max_within_expected_range() -> None:
    for _ in range(200):
        value = random_wocount_max()
        assert 1 <= value <= 19
