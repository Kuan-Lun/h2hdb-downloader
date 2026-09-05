from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parents[1]
_CHECKER = _PROJECT_ROOT / "scripts" / "check-publish-version.py"


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_version(repository: Path, version: str) -> None:
    (repository / "pyproject.toml").write_text(
        f'[project]\nname = "publish-version-fixture"\nversion = "{version}"\n',
        encoding="utf-8",
    )


def _commit(repository: Path, message: str) -> str:
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Publish Test")
    _git(repository, "config", "user.email", "publish-test@example.invalid")

    _write_version(repository, "0.13.0")
    before = _commit(repository, "chore: seed release")
    (repository / "runtime.txt").write_text("changed\n", encoding="utf-8")
    _commit(repository, "fix: change runtime")
    _write_version(repository, "0.13.1")
    version_commit = _commit(repository, "chore(release): bump version to 0.13.1")
    (repository / "test.txt").write_text("evidence\n", encoding="utf-8")
    _commit(repository, "test: add release evidence")
    return repository, before, version_commit


def _check(
    repository: Path,
    before_revision: str,
    output: Path,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["GITHUB_OUTPUT"] = str(output)
    return subprocess.run(
        (
            sys.executable,
            str(_CHECKER),
            "--before-revision",
            before_revision,
        ),
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_publish_version_uses_the_entire_multi_commit_push(
    tmp_path: Path,
) -> None:
    repository, before, _version_commit = _repository(tmp_path)
    output = tmp_path / "push-output"

    result = _check(repository, before, output)

    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8").splitlines() == [
        "current=0.13.1",
        "previous=0.13.0",
        "bumped=true",
    ]
    assert _git(repository, "show", "HEAD~1:pyproject.toml").endswith(
        'version = "0.13.1"'
    )


def test_publish_version_does_not_publish_without_a_version_transition(
    tmp_path: Path,
) -> None:
    repository, _before, version_commit = _repository(tmp_path)
    output = tmp_path / "push-output"

    result = _check(repository, version_commit, output)

    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8").splitlines() == [
        "current=0.13.1",
        "previous=0.13.1",
        "bumped=false",
    ]


def test_publish_version_fetches_a_pre_push_revision_missing_from_shallow_clone(
    tmp_path: Path,
) -> None:
    repository, before, _version_commit = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    shallow = tmp_path / "shallow"
    subprocess.run(
        ("git", "clone", "--bare", str(repository), str(remote)),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ("git", "clone", "--depth=1", remote.as_uri(), str(shallow)),
        check=True,
        capture_output=True,
        text=True,
    )
    missing = subprocess.run(
        ("git", "cat-file", "-e", f"{before}^{{commit}}"),
        cwd=shallow,
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing.returncode != 0
    output = tmp_path / "push-output"

    result = _check(shallow, before, output)

    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8").splitlines() == [
        "current=0.13.1",
        "previous=0.13.0",
        "bumped=true",
    ]


def test_publish_version_rejects_a_decrease(tmp_path: Path) -> None:
    repository, _before, version_commit = _repository(tmp_path)
    _write_version(repository, "0.12.0")
    _commit(repository, "chore: decrease version")

    result = _check(repository, version_commit, tmp_path / "push-output")

    assert result.returncode == 1
    assert "project.version decreased from 0.13.1 to 0.12.0" in result.stderr


def test_publish_version_does_not_publish_an_initial_branch_seed(
    tmp_path: Path,
) -> None:
    repository, _before, _version_commit = _repository(tmp_path)
    output = tmp_path / "push-output"

    result = _check(repository, "0" * 40, output)

    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8").splitlines() == [
        "current=0.13.1",
        "previous=",
        "bumped=false",
    ]


def test_publish_workflow_passes_the_pre_push_revision_to_the_checker() -> None:
    workflow = (_PROJECT_ROOT / ".github" / "workflows" / "publish.yml").read_text(
        encoding="utf-8"
    )

    assert "BEFORE_SHA: ${{ github.event.before }}" in workflow
    assert '--before-revision "${BEFORE_SHA}"' in workflow
    assert "HEAD~1:pyproject.toml" not in workflow
    assert "github.ref ==" in workflow
    assert (
        "format('refs/heads/{0}', github.event.repository.default_branch)" in workflow
    )
    assert "github.ref_name == github.event.repository.default_branch" not in workflow


def test_publish_workflow_uses_declared_packaging_dependencies() -> None:
    workflow = (_PROJECT_ROOT / ".github" / "workflows" / "publish.yml").read_text(
        encoding="utf-8"
    )

    assert "python scripts/install-ci-dependencies.py packaging build" in workflow
    assert "pip install --upgrade packaging build" not in workflow
