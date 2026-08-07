#!/usr/bin/env bash
# Recreate the repository-local environment with full dev dependencies.
set -euo pipefail
cd "$(dirname "$0")/.."

uv venv --clear --python 3.14

EDITABLES=(-e ".[dev]")
for sibling in \
    ../h2hdb.clone \
    ../h2h-galleryinfo-parser.clone \
    ../hbrowser.clone; do
    if [[ -f "$sibling/pyproject.toml" ]]; then
        EDITABLES+=(-e "$sibling")
    fi
done
uv pip install --python .venv/bin/python "${EDITABLES[@]}"
