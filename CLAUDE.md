# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

`h2hdb-downloader` is a small Python library that automates downloading galleries (from exhentai/e-hentai via the `hbrowser` package) and recording their state in an `h2hdb` database. It is published to PyPI and consumed as a dependency by other projects — it has no CLI entry point or standalone runtime of its own.

The package has exactly two public exports (`src/h2hdb_downloader/__init__.py`): `PreLinks` and `Downloader`, both defined in `src/h2hdb_downloader/downloader.py`.

## Build & Development Commands

```bash
# Install dependencies
uv pip install -e .

# Type checking (strict mode configured in mypy.ini)
uv run mypy src

# Linting with ruff (rules in pyproject.toml: E, F, I, UP)
uv run ruff check .

# Formatting with black (88 char line length)
uv run black .
```

## Coding Guidelines

This is a solo, pre-1.0 project with no external consumers pinned to current APIs. Do not optimize for minimal diffs or backward compatibility:

- Freely rename, restructure, or delete code when it improves the design — there are no external callers to break.
- Do not keep deprecated aliases, compatibility shims, or old code paths "just in case."
- Prefer the cleanest end state over the smallest diff to get there.

Follow SOLID principles when writing code:

- **Single Responsibility** - Each class/module should have one reason to change
- **Open/Closed** - Open for extension, closed for modification (use inheritance/composition)
- **Liskov Substitution** - Subtypes must be substitutable for their base types
- **Interface Segregation** - Prefer small, specific interfaces over large ones
- **Dependency Inversion** - Depend on abstractions (ABC), not concrete implementations

## Code Style

- **Sync obligation for tooling configuration:** the IDE save pipeline and the Stop hook pipeline are kept in lockstep across the locations below. Any change to one of them requires matching updates to the others in the same change.
  - Python formatting/lint/type-check: [.vscode/settings.json](.vscode/settings.json) (`[python]` block), the `[tool.ruff]` section of [pyproject.toml](pyproject.toml), [mypy.ini](mypy.ini), and [.claude/hooks/finalize-python.sh](.claude/hooks/finalize-python.sh).
  - Markdown formatting: [.vscode/settings.json](.vscode/settings.json) (`[markdown]` block) and [.claude/hooks/finalize-markdown.sh](.claude/hooks/finalize-markdown.sh).
  - Tool versions: the `dev` group of `[project.optional-dependencies]` in [pyproject.toml](pyproject.toml) pins `black`, `ruff`, `mypy`, and `pymarkdownlnt`. Both the IDE pipeline (when invoked via `uv run`) and the Stop hooks resolve to these venv-installed versions, so bumping any of them must be done here — not via Homebrew or any other system-wide install.
- Python version range: refer to `requires-python` in [pyproject.toml](pyproject.toml)
