"""Regression coverage for dependency conflicts hidden by --no-deps smoke installs."""

from __future__ import annotations

import runpy
import tomllib
from collections.abc import Callable, Iterable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import cast

import pytest
from packaging.requirements import Requirement

_REPOSITORY = Path(__file__).parents[1]
_Check = Callable[[Iterable[str]], None]


def _check() -> _Check:
    return cast(
        _Check,
        runpy.run_path(str(_REPOSITORY / "scripts/check-installed-wheel.py"))[
            "check_runtime_requirements"
        ],
    )


def test_current_manifest_accepts_the_installed_core_cohort() -> None:
    manifest = tomllib.loads((_REPOSITORY / "pyproject.toml").read_text())
    dependencies = manifest["project"]["dependencies"]
    core = next(
        Requirement(item) for item in dependencies if Requirement(item).name == "h2hdb"
    )
    assert "0.36.0" in core.specifier
    assert "0.35.5" not in core.specifier
    assert "0.37.0" not in core.specifier
    assert version("h2hdb") in core.specifier
    _check()(dependencies)


def test_smoke_rejects_the_previous_upper_bound_with_installed_new_core() -> None:
    with pytest.raises(
        RuntimeError, match=r"does not satisfy h2hdb>=0\.35\.0,<0\.36\.0"
    ):
        _check()(("h2hdb>=0.35.0,<0.36.0",))


def test_smoke_rejects_missing_runtime_dependency_and_ignores_inactive_dev_extra() -> (
    None
):
    missing = "h2hdb-downloader-missing-dependency-probe>=1"
    _check()((f'{missing}; extra == "dev"',))
    with pytest.raises(PackageNotFoundError):
        _check()((missing,))
