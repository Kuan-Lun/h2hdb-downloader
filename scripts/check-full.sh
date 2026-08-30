#!/usr/bin/env bash

set -euo pipefail

repository_root="$(git rev-parse --show-toplevel)"
cd "$repository_root"

scripts/check-fast.sh
.venv/bin/pytest

artifact_root="$(mktemp -d "${TMPDIR:-/tmp}/h2hdb-downloader-check.XXXXXX")"
cleanup() {
    rm -rf -- "$artifact_root"
}
trap cleanup EXIT

.venv/bin/python -m build --no-isolation --outdir "$artifact_root/dist"
wheel="$(find "$artifact_root/dist" -maxdepth 1 -type f -name '*.whl' -print -quit)"
[[ -n "$wheel" ]] || {
    printf 'Wheel build did not produce an artifact\n' >&2
    exit 1
}

UV_CACHE_DIR="$artifact_root/uv-cache" \
    uv venv --python "$repository_root/.venv/bin/python" \
    "$artifact_root/smoke-venv"
smoke_python="$artifact_root/smoke-venv/bin/python"
UV_CACHE_DIR="$artifact_root/uv-cache" \
    uv pip install --python "$smoke_python" --no-deps "$wheel"
development_site="$("$repository_root/.venv/bin/python" -c \
    'import sysconfig; print(sysconfig.get_path("purelib"))')"
development_site_statement="$("$repository_root/.venv/bin/python" -c \
    'import sys; print(f"import site; site.addsitedir({sys.argv[1]!r})")' \
    "$development_site")"
smoke_site="$("$smoke_python" -c \
    'import sysconfig; print(sysconfig.get_path("purelib"))')"
printf '%s\n' "$development_site_statement" > \
    "$smoke_site/development-dependencies.pth"
(
    cd "$artifact_root"
    "$smoke_python" -I -c \
        'import h2hdb_downloader, pathlib, sys; assert pathlib.Path(h2hdb_downloader.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve()); from h2hdb_downloader import DownloadTurnLostError, Downloader, TagCascadePolicy; assert Downloader and TagCascadePolicy and DownloadTurnLostError'
)
