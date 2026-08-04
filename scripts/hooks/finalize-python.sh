#!/usr/bin/env bash
# Stop-hook: run formatters and type checker on the whole project before
# Claude or Codex finishes responding. Mirrors VS Code's on-save pipeline:
#   1. Black                 — pre-format pass; also catches syntax errors.
#   2. Ruff `--fix`          — auto-fixes safe lints (imports, pyupgrade, …).
#   3. Black                 — second pass: re-format whatever ruff rewrote
#                              so the file is guaranteed black-stable, even
#                              when ruff's UP rules produce code that black
#                              would reformat (single-pipeline convergence).
#   4. Mypy                  — type check on the final formatted code.
#
# Why a Stop hook (not PostToolUse): incremental edits routinely produce
# transient broken states (e.g. add import, then add usage in next edit).
# Running checkers between every edit would block legitimate workflows.
# At Stop time the codebase is supposed to be coherent, so a full check is
# the right gate.
#
# Error handling: any tool failing causes the script to exit 2, which makes
# the agent surface the captured stderr on the next turn.
# stderr from a successful tool is hidden by the hook runner, so the noisy
# "All done! ✨ 🍰 ✨" lines from Black do not pollute the transcript.

set -eu
trap 'exit 2' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

PY_FILES=()
while IFS= read -r -d '' file; do
    if [[ -f "$file" ]]; then
        PY_FILES+=("$file")
    fi
done < <(
    git ls-files --cached --others --exclude-standard -z -- '*.py' '*.pyi'
)

if [[ ${#PY_FILES[@]} -eq 0 ]]; then
    exit 0
fi

uv run black "${PY_FILES[@]}" >&2
uv run ruff check --fix "${PY_FILES[@]}" >&2
uv run black "${PY_FILES[@]}" >&2
uv run mypy "${PY_FILES[@]}" >&2
