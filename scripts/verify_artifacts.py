# SPDX-License-Identifier: Apache-2.0
"""Verify wheel/sdist metadata, contents, and deterministic release checksums."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

EXPECTED_NAME = "meridian-storage-oci"
EXPECTED_VERSION = "1.1.0"
PACKAGE_PATH = "meridian_storage/adapters/oci/"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(directory: Path) -> dict[str, object]:
    wheels = sorted(directory.glob("meridian_storage_oci-*.whl"))
    sdists = sorted(directory.glob("meridian_storage_oci-*.tar.gz"))
    assert len(wheels) == 1, "exactly one meridian-storage-oci wheel is required"
    assert len(sdists) == 1, "exactly one meridian-storage-oci sdist is required"
    wheel, sdist = wheels[0], sdists[0]

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        assert len(metadata_names) == 1
        parsed = BytesParser().parsebytes(archive.read(metadata_names[0]))
        assert parsed["Name"] == EXPECTED_NAME
        assert parsed["Version"] == EXPECTED_VERSION
        assert parsed["License-Expression"] == "Apache-2.0"
        assert PACKAGE_PATH + "py.typed" in names
        assert PACKAGE_PATH + "compatibility.json" in names
        assert any(name.endswith(".dist-info/licenses/LICENSE") for name in names)
        assert any(name.endswith(".dist-info/licenses/NOTICE") for name in names)
        python_roots = {
            name.split("/", 1)[0]
            for name in names
            if name.endswith(".py") and ".dist-info/" not in name
        }
        assert python_roots == {"meridian_storage"}

    with tarfile.open(sdist, mode="r:gz") as archive:
        names = set(archive.getnames())
        prefix = f"meridian_storage_oci-{EXPECTED_VERSION}/"
        assert prefix + "LICENSE" in names
        assert prefix + "NOTICE" in names
        assert prefix + "pyproject.toml" in names
        assert prefix + "src/" + PACKAGE_PATH + "compatibility.json" in names
        assert not any("../" in name or name.startswith("/") for name in names)

    return {
        "formatVersion": "meridian-storage-oci-artifacts.v1",
        "name": EXPECTED_NAME,
        "version": EXPECTED_VERSION,
        "artifacts": [
            {"filename": wheel.name, "sha256": _sha256(wheel)},
            {"filename": sdist.name, "sha256": _sha256(sdist)},
        ],
        "status": "passed",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(verify(arguments.directory), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
