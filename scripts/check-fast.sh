#!/usr/bin/env bash

set -euo pipefail

repository_root="$(git rev-parse --show-toplevel)"
cd "$repository_root"

for executable in .venv/bin/ruff .venv/bin/mypy node_modules/.bin/markdownlint-cli2; do
    [[ -x "$executable" ]] || {
        printf 'Missing repository-local tool: %s; run scripts/rebuild-env.sh\n' \
            "$executable" >&2
        exit 1
    }
done

.venv/bin/ruff check --no-cache .
.venv/bin/ruff format --no-cache --check .
.venv/bin/mypy --config-file pyproject.toml --no-incremental --cache-dir=/dev/null
node_modules/.bin/markdownlint-cli2
