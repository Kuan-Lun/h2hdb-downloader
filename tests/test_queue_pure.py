from h2hdb_downloader._queue import (
    TodownloadEntry,
    compute_pass_gids,
    parse_todownload_csv_rows,
    random_wocount_max,
    should_attempt_download,
)


def test_compute_pass_gids_excludes_pending_and_queued() -> None:
    pass_gids = compute_pass_gids(
        downloaded_gids=[1, 2, 3, 4],
        pending_download_gids=[2],
        todownload_gids=[TodownloadEntry(3, "")],
    )
    assert set(pass_gids) == {1, 4}


def test_compute_pass_gids_with_nothing_downloaded() -> None:
    assert compute_pass_gids([], [], []) == []


def test_parse_todownload_csv_rows_blank_gid_becomes_zero() -> None:
    rows = [["", "https://exhentai.org/g/1/abc/"], ["42", ""]]
    assert parse_todownload_csv_rows(rows) == [
        TodownloadEntry(0, "https://exhentai.org/g/1/abc/"),
        TodownloadEntry(42, ""),
    ]


def test_should_attempt_download_skips_settled_gid() -> None:
    assert should_attempt_download(1, {1}, wocount=0, wocount_max=5) is False


def test_should_attempt_download_attempts_unsettled_gid() -> None:
    assert should_attempt_download(1, set(), wocount=0, wocount_max=5) is True


def test_should_attempt_download_forces_reverify_past_wocount_max() -> None:
    assert should_attempt_download(1, {1}, wocount=6, wocount_max=5) is True


def test_random_wocount_max_within_expected_range() -> None:
    for _ in range(200):
        value = random_wocount_max()
        assert 1 <= value <= 19
