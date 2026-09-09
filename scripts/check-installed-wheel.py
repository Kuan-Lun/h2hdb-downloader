"""Verify the installed wheel and every active direct runtime requirement."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from importlib.metadata import distribution, version
from pathlib import Path

from packaging.requirements import Requirement


def check_runtime_requirements(requirements: Iterable[str]) -> None:
    """Reject missing or incompatible installed dependencies despite --no-deps."""
    for declared in requirements:
        requirement = Requirement(declared)
        if requirement.marker is not None and not requirement.marker.evaluate(
            {"extra": ""}
        ):
            continue
        installed = version(requirement.name)
        if not requirement.specifier.contains(installed, prereleases=True):
            raise RuntimeError(
                f"Installed {requirement.name}=={installed} does not satisfy {declared}"
            )


def main() -> None:
    import h2hdb_downloader
    from h2hdb_downloader import Downloader, DownloadTurnLostError, TagCascadePolicy

    prefix = Path(sys.prefix).resolve()
    package_path = Path(h2hdb_downloader.__file__).resolve()
    installed = distribution("h2hdb-downloader")
    if not package_path.is_relative_to(prefix) or not Path(
        installed.locate_file("")
    ).resolve().is_relative_to(prefix):
        raise RuntimeError(
            "Downloader import and metadata must come from the smoke wheel"
        )
    check_runtime_requirements(installed.requires or ())
    if not all(
        callable(item) for item in (DownloadTurnLostError, Downloader, TagCascadePolicy)
    ):
        raise RuntimeError("Downloader public exports are missing")
    print("Installed downloader wheel runtime requirements and public exports verified")


if __name__ == "__main__":
    main()
