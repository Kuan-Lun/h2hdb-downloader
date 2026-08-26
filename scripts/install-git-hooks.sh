#!/usr/bin/env bash

set -euo pipefail

repository_root="$(git rev-parse --show-toplevel)"
cd "$repository_root"

primary="$(scripts/detect-primary-branch.sh)"
git config --local core.hooksPath .githooks
git config --local "branch.$primary.mergeOptions" --no-ff

printf 'Installed repository hooks; primary branch: %s\n' "$primary"
