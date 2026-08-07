import ast
from pathlib import Path

PRODUCTION_ROOT = Path(__file__).parents[1] / "src" / "h2hdb_downloader"
ALLOWED_H2HDB_EXPORTS = {
    "CoordinatorUnavailableError",
    "DownloadCoordinator",
    "DownloadRequest",
    "DownloadTurn",
    "EnsureDownloadRequestResult",
}


def test_production_uses_only_the_download_coordinator_public_boundary() -> None:
    violations: list[str] = []

    for path in sorted(PRODUCTION_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative_path = path.relative_to(PRODUCTION_ROOT.parent)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "h2hdb" or alias.name.startswith("h2hdb."):
                        violations.append(
                            f"{relative_path}:{node.lineno}: import {alias.name}"
                        )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith("h2hdb."):
                    violations.append(
                        f"{relative_path}:{node.lineno}: from {module} import ..."
                    )
                elif module == "h2hdb":
                    unexpected = {
                        alias.name for alias in node.names
                    } - ALLOWED_H2HDB_EXPORTS
                    for name in sorted(unexpected):
                        violations.append(
                            f"{relative_path}:{node.lineno}: "
                            f"non-boundary h2hdb export {name}"
                        )
            elif isinstance(node, ast.Attribute) and node.attr == "database_gate":
                violations.append(
                    f"{relative_path}:{node.lineno}: consumer-owned database_gate"
                )

    assert violations == []
