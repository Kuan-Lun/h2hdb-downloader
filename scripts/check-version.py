#!/usr/bin/env python3
"""Validate a task-level X.Y.Z bump against the staged merge candidate."""

from __future__ import annotations

import argparse
import copy
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_LEGACY_VERSION_PATTERN = re.compile(r"^(\d+)(?:\.(\d+)){3,}$")
_IGNORED_PATHS = (
    ".claude/**",
    ".github/**",
    ".githooks/**",
    ".gitignore",
    ".markdownlint-cli2.jsonc",
    "mypy.ini",
    ".release/**",
    ".vscode/**",
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "docs/**",
    "package.json",
    "scripts/**",
    "tests/**",
)
_AUDIT_PATH = ".release/dependency-audit.json"


def _git(*arguments: str, input_text: str | None = None) -> str:
    return subprocess.run(
        ("git", *arguments),
        check=True,
        input=input_text,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.rstrip("\n")


def _tree_file(tree: str, path: str) -> bytes:
    return subprocess.run(
        ("git", "show", f"{tree}:{path}"),
        check=True,
        stdout=subprocess.PIPE,
    ).stdout


def _load_toml(tree: str) -> dict[str, Any]:
    return tomllib.loads(_tree_file(tree, "pyproject.toml").decode())


def _release_metadata(document: dict[str, Any]) -> dict[str, Any]:
    project = copy.deepcopy(document.get("project", {}))
    project.pop("version", None)
    optional = project.get("optional-dependencies")
    if isinstance(optional, dict):
        optional.pop("dev", None)
    return {
        "build-system": document.get("build-system", {}),
        "project": project,
        "wheel": document.get("tool", {}).get("hatch", {}).get("build", {}),
    }


def _dependency_manifest(document: dict[str, Any], package: dict[str, Any]) -> bytes:
    project = document.get("project", {})
    manifest = {
        "build": document.get("build-system", {}).get("requires", []),
        "dependency-groups": document.get("dependency-groups", {}),
        "optional": project.get("optional-dependencies", {}),
        "runtime": project.get("dependencies", []),
        "node-dev": package.get("devDependencies", {}),
    }
    return json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _matches(path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _parse_version(value: str) -> tuple[int, int, int]:
    match = _VERSION_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(f"candidate version must use X.Y.Z: {value}")
    return tuple(int(part) for part in match.groups())


def _expected_version(
    base: tuple[int, int, int],
    *,
    breaking: bool,
    feature: bool,
) -> tuple[int, int, int]:
    major, minor, patch = base
    if major == 0:
        return (0, minor + 1, 0) if breaking else (0, minor, patch + 1)
    if breaking:
        return (major + 1, 0, 0)
    if feature:
        return (major, minor + 1, 0)
    return (major, minor, patch + 1)


def _validate_audit(tree: str, document: dict[str, Any], version: str) -> None:
    try:
        receipt = json.loads(_tree_file(tree, _AUDIT_PATH))
        package = json.loads(_tree_file(tree, "package.json"))
    except (subprocess.CalledProcessError, json.JSONDecodeError) as error:
        raise ValueError(
            "version bump requires a valid .release/dependency-audit.json"
        ) from error
    digest = hashlib.sha256(_dependency_manifest(document, package)).hexdigest()
    if receipt.get("schema") != "h2h.dependency-audit.v1":
        raise ValueError("dependency audit receipt has an unsupported schema")
    if receipt.get("project_version") != version:
        raise ValueError("dependency audit receipt does not match project version")
    if receipt.get("manifest_sha256") != digest:
        raise ValueError("dependency audit receipt does not match dependencies")
    review = receipt.get("review", {})
    if review.get("status") != "reviewed" or not review.get("note"):
        raise ValueError("dependency audit receipt lacks a compatibility review")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", action="store_true")
    arguments = parser.parse_args()

    if arguments.index:
        merge_ref = os.environ.get("WORKFLOW_MERGE_TASK_REF")
        if merge_ref:
            merge_commit = _git("rev-parse", "--verify", f"{merge_ref}^{{commit}}")
        else:
            merge_head = Path(_git("rev-parse", "--git-path", "MERGE_HEAD"))
            if not merge_head.exists():
                raise ValueError(
                    "--index requires WORKFLOW_MERGE_TASK_REF or an active merge"
                )
            merge_commit = _git("rev-parse", "MERGE_HEAD")
        base_tree = "HEAD"
        candidate_tree = _git("write-tree")
        message_range = f"HEAD..{merge_commit}"
    else:
        detector = Path("scripts/detect-primary-branch.sh")
        primary_name = subprocess.run(
            (str(detector),),
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        base_tree = primary_name
        merge_tree_output = _git(
            "merge-tree",
            "--write-tree",
            primary_name,
            "HEAD",
        )
        candidate_tree = merge_tree_output.splitlines()[0]
        if re.fullmatch(r"[0-9a-f]+", candidate_tree) is None:
            raise ValueError("git merge-tree did not return a candidate tree")
        message_range = f"{primary_name}..HEAD"
    base_document = _load_toml(base_tree)
    candidate_document = _load_toml(candidate_tree)
    base_version_text = str(base_document["project"]["version"])
    candidate_version_text = str(candidate_document["project"]["version"])
    candidate_version = _parse_version(candidate_version_text)

    changed_paths = tuple(
        path
        for path in _git(
            "diff", "--name-only", "--diff-filter=ACDMRT", base_tree, candidate_tree
        ).splitlines()
        if path
    )
    release_patterns = (
        candidate_document.get("tool", {})
        .get("h2h", {})
        .get("version", {})
        .get("release-paths", [])
    )
    release_changed = _release_metadata(base_document) != _release_metadata(
        candidate_document
    ) or any(_matches(path, release_patterns) for path in changed_paths)
    unknown = [
        path
        for path in changed_paths
        if path != "pyproject.toml"
        and not _matches(path, release_patterns)
        and not _matches(path, _IGNORED_PATHS)
    ]
    if unknown:
        raise ValueError(
            "unclassified version impact paths: " + ", ".join(sorted(unknown))
        )

    version_changed = base_version_text != candidate_version_text
    if release_changed and not version_changed:
        raise ValueError("release surface changed without a project version bump")
    if not release_changed and version_changed:
        raise ValueError("project version changed without a release-surface change")
    if not version_changed:
        return 0

    messages = _git("log", "--format=%B%x00", message_range)
    breaking = bool(
        re.search(r"^[a-z]+(?:\([^\n)]+\))?!:", messages, re.MULTILINE)
        or re.search(r"^BREAKING CHANGE:", messages, re.MULTILINE)
    )
    feature = bool(re.search(r"^feat(?:\([^\n)]+\))?:", messages, re.MULTILINE))

    base_match = _VERSION_PATTERN.fullmatch(base_version_text)
    if base_match is not None:
        base_version = tuple(int(part) for part in base_match.groups())
        expected = _expected_version(base_version, breaking=breaking, feature=feature)
        if candidate_version != expected:
            expected_text = ".".join(str(part) for part in expected)
            raise ValueError(
                "expected project version "
                f"{expected_text}, got {candidate_version_text}"
            )
    elif _LEGACY_VERSION_PATTERN.fullmatch(base_version_text) is None:
        raise ValueError(f"unsupported base version: {base_version_text}")
    _validate_audit(candidate_tree, candidate_document, candidate_version_text)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"check-version: {error}", file=sys.stderr)
        raise SystemExit(1) from error
