from __future__ import annotations

import hashlib
import runpy
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def test_audit_writer_and_validator_share_manifest_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(_ROOT / "scripts")
    audit = runpy.run_path(str(_ROOT / "scripts/audit-dependencies.py"))
    gate = runpy.run_path(str(_ROOT / "scripts/check-version.py"))
    serialize = audit["dependency_manifest"]
    assert serialize is gate["dependency_manifest"]


def test_manifest_has_stable_canonical_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(_ROOT / "scripts")
    namespace = runpy.run_path(str(_ROOT / "scripts/dependency_manifest.py"))
    document = {
        "project": {
            "version": "0.1.0",
            "description": "ignored",
            "dependencies": ["runtime>=1"],
            "optional-dependencies": {"test": ["test>=2"]},
        },
        "dependency-groups": {"dev": ["dev>=3"]},
        "build-system": {"requires": ["build>=4"], "build-backend": "ignored"},
        "tool": {"ignored": True},
    }
    package = {"devDependencies": {"工具": ">=5"}, "name": "ignored"}
    expected = (
        '{"build":["build>=4"],"dependency-groups":{"dev":["dev>=3"]},'
        '"node-dev":{"工具":">=5"},"optional":{"test":["test>=2"]},'
        '"runtime":["runtime>=1"]}'
    ).encode()
    actual = namespace["dependency_manifest"](document, package)
    assert actual == expected
    assert hashlib.sha256(actual).hexdigest() == (
        "deb24113ac54658c7f09e53a2a899d511944758bc0de720e8cec9bd8a45b7e07"
    )
    assert (
        namespace["dependency_manifest"](
            dict(reversed(list(document.items()))), package
        )
        == expected
    )


@pytest.mark.parametrize(
    ("document", "package"),
    [
        ({"build-system": {"requires": ["build>=1"]}}, {}),
        ({"project": {"dependencies": ["runtime>=1"]}}, {}),
        ({"project": {"optional-dependencies": {"extra": ["optional>=1"]}}}, {}),
        ({"dependency-groups": {"dev": ["dev>=1"]}}, {}),
        ({}, {"devDependencies": {"node": ">=1"}}),
    ],
)
def test_each_dependency_group_changes_manifest(
    document: dict[str, object],
    package: dict[str, object],
) -> None:
    namespace = runpy.run_path(str(_ROOT / "scripts/dependency_manifest.py"))
    serialize = namespace["dependency_manifest"]
    assert (
        hashlib.sha256(serialize(document, package)).digest()
        != hashlib.sha256(serialize({}, {})).digest()
    )
