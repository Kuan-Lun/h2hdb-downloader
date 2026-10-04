from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def _require_success(
    command: tuple[str, ...],
    result: subprocess.CompletedProcess[str],
) -> subprocess.CompletedProcess[str]:
    if result.returncode != 0:
        raise AssertionError(
            f"command failed with exit {result.returncode}: {command!r}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def _clean_git_environment() -> dict[str, str]:
    command = ("git", "rev-parse", "--local-env-vars")
    result = _require_success(
        command,
        subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        ),
    )
    environment = os.environ.copy()
    for name in result.stdout.splitlines():
        environment.pop(name, None)
    return environment


def _git(
    worktree: Path,
    environment: dict[str, str],
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    command = ("git", "-C", str(worktree), *arguments)
    return _require_success(
        command,
        subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            env=environment,
        ),
    )


def _operation_path(
    worktree: Path,
    environment: dict[str, str],
    name: str,
) -> Path:
    result = _git(
        worktree,
        environment,
        "rev-parse",
        "--path-format=absolute",
        "--git-path",
        name,
    )
    return Path(result.stdout.strip())


@pytest.mark.parametrize("primary_branch", ("main", "master"))
def test_failed_merge_gate_aborts_the_primary_worktree_and_retains_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary_branch: str,
) -> None:
    polluted = {
        "GIT_DIR": str(tmp_path / "outer.git"),
        "GIT_INDEX_FILE": str(tmp_path / "outer.index"),
        "GIT_PREFIX": "outer-prefix/",
        "GIT_WORK_TREE": str(tmp_path / "outer-worktree"),
    }
    for name, value in polluted.items():
        monkeypatch.setenv(name, value)
    git_environment = _clean_git_environment()
    assert polluted.keys().isdisjoint(git_environment)

    source_root = Path(__file__).resolve().parents[1]
    primary_worktree = tmp_path / "primary"
    task_worktree = tmp_path / "task"
    primary_worktree.mkdir()
    _git(primary_worktree, git_environment, "init", "-b", primary_branch)
    _git(primary_worktree, git_environment, "config", "user.name", "Merge Test")
    _git(
        primary_worktree,
        git_environment,
        "config",
        "user.email",
        "merge@example.invalid",
    )
    _git(
        primary_worktree,
        git_environment,
        "config",
        "core.hooksPath",
        ".githooks",
    )
    _git(
        primary_worktree,
        git_environment,
        "config",
        "workflow.primaryBranch",
        primary_branch,
    )

    scripts = primary_worktree / "scripts"
    hooks = primary_worktree / ".githooks"
    scripts.mkdir()
    hooks.mkdir()
    for name in ("detect-primary-branch.sh", "git-flow-merge.sh"):
        shutil.copy2(source_root / "scripts" / name, scripts / name)
    pre_merge_hook = hooks / "pre-merge-commit"
    pre_merge_hook.write_text("#!/usr/bin/env bash\nexit 42\n", encoding="utf-8")
    pre_merge_hook.chmod(0o755)
    (primary_worktree / "payload.txt").write_text("primary\n", encoding="utf-8")
    _git(primary_worktree, git_environment, "add", ".")
    _git(
        primary_worktree,
        git_environment,
        "commit",
        "-m",
        "chore: initialize fixture",
    )
    primary_head = _git(
        primary_worktree,
        git_environment,
        "rev-parse",
        "HEAD",
    ).stdout.strip()

    _git(
        primary_worktree,
        git_environment,
        "worktree",
        "add",
        "-b",
        "perf/task",
        str(task_worktree),
    )
    (task_worktree / "payload.txt").write_text("task\n", encoding="utf-8")
    _git(task_worktree, git_environment, "add", "payload.txt")
    _git(
        task_worktree,
        git_environment,
        "commit",
        "-m",
        "feat: change payload",
    )
    task_head = _git(
        task_worktree,
        git_environment,
        "rev-parse",
        "HEAD",
    ).stdout.strip()

    result = subprocess.run(
        (str(task_worktree / "scripts" / "git-flow-merge.sh"),),
        cwd=task_worktree,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=git_environment,
    )

    assert result.returncode != 0
    assert "merge or merge gate failed; task branch was retained" in result.stderr
    assert not _operation_path(
        primary_worktree,
        git_environment,
        "MERGE_HEAD",
    ).exists()
    assert (
        _git(
            primary_worktree,
            git_environment,
            "status",
            "--porcelain=v1",
        ).stdout
        == ""
    )
    assert (
        _git(primary_worktree, git_environment, "rev-parse", "HEAD").stdout.strip()
        == primary_head
    )
    assert (primary_worktree / "payload.txt").read_text(encoding="utf-8") == (
        "primary\n"
    )

    assert _git(
        primary_worktree,
        git_environment,
        "show-ref",
        "--verify",
        "refs/heads/perf/task",
    ).stdout
    assert (
        _git(task_worktree, git_environment, "rev-parse", "HEAD").stdout.strip()
        == task_head
    )
    assert (
        _git(
            task_worktree,
            git_environment,
            "status",
            "--porcelain=v1",
        ).stdout
        == ""
    )
    worktrees = _git(
        primary_worktree,
        git_environment,
        "worktree",
        "list",
        "--porcelain",
    ).stdout
    assert f"worktree {task_worktree}\n" in worktrees
    assert "branch refs/heads/perf/task\n" in worktrees
