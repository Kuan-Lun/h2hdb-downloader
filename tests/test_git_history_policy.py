from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass(frozen=True)
class GitFixture:
    root: Path
    environment: dict[str, str]
    primary: str

    def run(self, *arguments: str, succeeds: bool = True) -> str:
        result = subprocess.run(
            arguments,
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert (result.returncode == 0) == succeeds, (
            f"{arguments!r}: exit {result.returncode}\n{result.stdout}\n{result.stderr}"
        )
        return result.stdout if succeeds else result.stderr

    def git(self, *arguments: str, succeeds: bool = True) -> str:
        return self.run("git", *arguments, succeeds=succeeds).strip()

    def commit(self, name: str) -> str:
        (self.root / name).write_text(name, encoding="utf-8")
        self.git("add", name)
        self.git("commit", "-m", f"test: add {name}")
        return self.git("rev-parse", "HEAD")

    def install(self) -> None:
        self.run("scripts/install-git-hooks.sh")


@pytest.fixture(params=("main", "master", "trunk"))
def git_fixture(tmp_path: Path, request: pytest.FixtureRequest) -> GitFixture:
    environment = os.environ.copy()
    names = subprocess.run(
        ("git", "rev-parse", "--local-env-vars"),
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    ).stdout.splitlines()
    for name in names:
        environment.pop(name, None)
    environment.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_EDITOR="true",
        GIT_SEQUENCE_EDITOR="true",
    )
    primary = str(request.param)
    root = tmp_path / "checkout"
    root.mkdir()
    fixture = GitFixture(root, environment, primary)
    fixture.git("init", "-b", primary)
    fixture.git("config", "user.name", "History Policy Test")
    fixture.git("config", "user.email", "history@example.invalid")
    fixture.git("config", "workflow.primaryBranch", primary)
    source = Path(__file__).resolve().parents[1]
    (root / "scripts").mkdir()
    (root / ".githooks").mkdir()
    for name in ("detect-primary-branch.sh", "install-git-hooks.sh"):
        shutil.copy2(source / "scripts" / name, root / "scripts" / name)
    shutil.copy2(source / ".githooks/pre-rebase", root / ".githooks/pre-rebase")
    fixture.git("add", ".")
    fixture.commit("initial")
    fixture.git("init", "--bare", "-b", primary, str(tmp_path / "remote.git"))
    fixture.git("remote", "add", "origin", str(tmp_path / "remote.git"))
    fixture.git("push", "--set-upstream", "origin", primary)
    fixture.install()
    return fixture


def _merge_task(fixture: GitFixture) -> str:
    fixture.git("switch", "-c", "task/change")
    fixture.commit("task-change")
    fixture.git("switch", fixture.primary)
    fixture.git("merge", "--no-edit", "task/change")
    commit_and_parents = fixture.git("rev-list", "--parents", "-1", "HEAD")
    assert len(commit_and_parents.split()) == 3
    return commit_and_parents


@pytest.mark.parametrize("rebase_setting", (False, True))
def test_pull_preserves_the_exact_task_merge(
    git_fixture: GitFixture, rebase_setting: bool
) -> None:
    merge = _merge_task(git_fixture)
    if rebase_setting:
        git_fixture.git("config", "pull.rebase", "true")
        git_fixture.git("config", f"branch.{git_fixture.primary}.rebase", "true")

    git_fixture.git("pull", "--tags", "origin", git_fixture.primary)

    assert git_fixture.git("rev-list", "--parents", "-1", "HEAD") == merge


@pytest.mark.parametrize("override", ("flag", "configuration"))
def test_pull_cannot_rebase_primary(git_fixture: GitFixture, override: str) -> None:
    merge = _merge_task(git_fixture)
    command: tuple[str, ...]
    if override == "flag":
        command = ("pull", "--rebase", "origin", git_fixture.primary)
    else:
        command = (
            "-c",
            "pull.rebase=true",
            "-c",
            f"branch.{git_fixture.primary}.rebase=true",
            "-c",
            "pull.ff=true",
            "pull",
            "origin",
            git_fixture.primary,
        )

    error = git_fixture.git(*command, succeeds=False)

    assert "Rebasing primary is not allowed" in error
    assert git_fixture.git("rev-list", "--parents", "-1", "HEAD") == merge


@pytest.mark.parametrize("target", ("current", "explicit", "qualified", "tag-shadow"))
def test_direct_rebase_cannot_rewrite_primary(
    git_fixture: GitFixture, target: str
) -> None:
    merge = _merge_task(git_fixture)
    command: tuple[str, ...] = ("rebase", "origin/" + git_fixture.primary)
    match target:
        case "tag-shadow":
            git_fixture.git("tag", git_fixture.primary)
        case "current":
            pass
        case _:
            git_fixture.git("switch", "task/change")
            branch = git_fixture.primary
            if target == "qualified":
                branch = f"refs/heads/{branch}"
            command += (branch,)

    error = git_fixture.git(*command, succeeds=False)

    assert "Rebasing primary is not allowed" in error
    assert git_fixture.git("rev-list", "--parents", "-1", git_fixture.primary) == merge


@pytest.mark.parametrize("explicit", (False, True))
def test_task_branch_can_rebase_onto_primary(
    git_fixture: GitFixture, explicit: bool
) -> None:
    git_fixture.git("switch", "-c", "task/rebase")
    previous_task = git_fixture.commit("task-file")
    git_fixture.git("switch", git_fixture.primary)
    primary = git_fixture.commit("primary-file")
    if explicit:
        git_fixture.git("rebase", git_fixture.primary, "task/rebase")
    else:
        git_fixture.git("switch", "task/rebase")
        git_fixture.git("rebase", git_fixture.primary)

    assert git_fixture.git("rev-parse", "HEAD") != previous_task
    assert git_fixture.git("rev-parse", "HEAD^") == primary
    assert (git_fixture.root / "task-file").read_text(encoding="utf-8") == "task-file"
    assert (git_fixture.root / "primary-file").exists()


def test_diverged_pull_refuses_to_change_the_task_merge(
    git_fixture: GitFixture,
) -> None:
    merge = _merge_task(git_fixture)
    peer = git_fixture.root.parent / "peer"
    git_fixture.git("clone", str(git_fixture.root.parent / "remote.git"), str(peer))
    remote_writer = GitFixture(peer, git_fixture.environment, git_fixture.primary)
    remote_writer.git("config", "user.name", "Remote Test")
    remote_writer.git("config", "user.email", "remote@example.invalid")
    remote_writer.commit("remote-file")
    remote_writer.git("push")

    error = git_fixture.git("pull", succeeds=False)

    assert "fast-forward" in error
    assert git_fixture.git("rev-list", "--parents", "-1", "HEAD") == merge


def test_reinstall_replaces_unsafe_pull_settings(git_fixture: GitFixture) -> None:
    expected = {
        "core.hooksPath": ".githooks",
        f"branch.{git_fixture.primary}.mergeOptions": "--no-ff",
        f"branch.{git_fixture.primary}.rebase": "false",
        "pull.rebase": "false",
        "pull.ff": "only",
    }
    for key, value in (
        ("pull.rebase", "true"),
        (f"branch.{git_fixture.primary}.rebase", "merges"),
        ("pull.ff", "false"),
        (f"branch.{git_fixture.primary}.mergeOptions", "--ff"),
    ):
        git_fixture.git("config", "--local", key, value)

    git_fixture.install()
    git_fixture.install()

    for key, value in expected.items():
        assert git_fixture.git("config", "--local", "--get", key) == value
