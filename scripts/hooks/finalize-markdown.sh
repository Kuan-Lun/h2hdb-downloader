#!/usr/bin/env bash
# Stop-hook: clean up markdown files after Claude or Codex finishes responding.
# Three passes:
#   1. pymarkdown fix    — best-effort autofix for mechanical issues like
#                          missing blank lines around lists. Many lints are not
#                          auto-fixable, so this step never blocks.
#   2. ruff format       — preview mode formats Python code blocks embedded
#       --preview          inside fenced ```python sections. This is the
#                          markdown counterpart to running black on .py
#                          files: it ensures example code in docs follows
#                          the same style as the rest of the codebase.
#                          A non-zero exit here means a code block has a
#                          parse error — that's a real signal worth surfacing.
#   3. pymarkdown scan   — lint every project Markdown file after both
#                          formatters have finished.
#
# Why a Stop hook (not PostToolUse): markdown is rarely the focus of an
# edit, and chaining a fixer onto every file write would slow the agent down
# without much benefit. The Stop time check is enough.
#
# Exit codes:
#   0 — everything passed
#   2 — formatting or linting failed

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

MD_FILES=()
while IFS= read -r -d '' file; do
    if [[ -f "$file" ]]; then
        MD_FILES+=("$file")
    fi
done < <(
    git ls-files --cached --others --exclude-standard -z -- '*.md'
)

if [[ ${#MD_FILES[@]} -eq 0 ]]; then
    exit 0
fi

# Pass 1: pymarkdown fix. Always non-blocking; tool-level errors are
# unlikely and if they happen they'd cascade into the ruff pass too.
uv run --no-sync pymarkdown fix "${MD_FILES[@]}" >/dev/null 2>&1 || true

# Pass 2: ruff format --preview. Surface parse errors via exit 2.
if ! uv run --no-sync ruff format --preview "${MD_FILES[@]}" >&2; then
    exit 2
fi

# Pass 3: pymarkdown scan. Unlike the best-effort fix pass, unresolved lints
# and tool failures block finalization.
if ! uv run --no-sync pymarkdown scan "${MD_FILES[@]}" >&2; then
    exit 2
fi
