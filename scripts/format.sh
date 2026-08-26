#!/usr/bin/env bash

set -euo pipefail

repository_root="$(git rev-parse --show-toplevel)"
cd "$repository_root"

[[ -x .venv/bin/ruff ]] || {
    printf 'Missing .venv tooling; run scripts/rebuild-env.sh\n' >&2
    exit 1
}
[[ -x node_modules/.bin/markdownlint-cli2 ]] || {
    printf 'Missing Node tooling; run scripts/rebuild-env.sh\n' >&2
    exit 1
}

.venv/bin/ruff check --fix .
.venv/bin/ruff format .
node_modules/.bin/markdownlint-cli2 --fix
