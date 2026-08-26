from __future__ import annotations

import json
import runpy
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from pytest import MonkeyPatch

AuditNode = Callable[[str, str], dict[str, object]]


def _audit_node() -> AuditNode:
    namespace = runpy.run_path(
        str(Path(__file__).parents[1] / "scripts" / "audit-dependencies.py")
    )
    return cast(AuditNode, namespace["_audit_node"])


def _install_npm_fake(
    monkeypatch: MonkeyPatch,
    payloads: list[object],
) -> list[tuple[str, ...]]:
    responses = iter(payloads)
    calls: list[tuple[str, ...]] = []

    def fake_run(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(next(responses)),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_node_audit_proves_latest_satisfies_declared_range(
    monkeypatch: MonkeyPatch,
) -> None:
    calls = _install_npm_fake(monkeypatch, ["0.23.2", ["0.23.1", "0.23.2"]])

    result = _audit_node()("markdownlint-cli2", ">=0.23.1")

    assert result["latest"] == "0.23.2"
    assert result["latest_satisfies"] is True
    assert calls == [
        ("npm", "view", "markdownlint-cli2", "version", "--json"),
        (
            "npm",
            "view",
            "markdownlint-cli2@>=0.23.1",
            "version",
            "--json",
        ),
    ]


def test_node_audit_reports_latest_outside_declared_range(
    monkeypatch: MonkeyPatch,
) -> None:
    _install_npm_fake(monkeypatch, ["0.23.2", "0.23.1"])

    result = _audit_node()("markdownlint-cli2", "0.23.1")

    assert result["latest_satisfies"] is False


def test_node_audit_rejects_invalid_registry_payload(
    monkeypatch: MonkeyPatch,
) -> None:
    _install_npm_fake(monkeypatch, [123])

    with pytest.raises(ValueError, match="invalid latest version"):
        _audit_node()("markdownlint-cli2", ">=0.23.1")
