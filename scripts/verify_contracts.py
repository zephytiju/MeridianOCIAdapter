# SPDX-License-Identifier: Apache-2.0
"""Verify locked design compatibility and the one-package public contract."""

from __future__ import annotations

import hashlib
import json
import tomllib
from importlib import metadata
from pathlib import Path
from typing import Any

from meridian_storage.adapters.oci import (
    ADAPTER_CONTRACT_VERSION,
    ADAPTER_ID,
    OCI_DISTRIBUTION_VERSION,
    OciDistributionBinding,
    __version__,
    compatibility_document,
    configured_capability_manifest,
)
from meridian_storage.spi import AdapterFactory

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_COMMON_WHEEL = "26422de8bce6dcf5e1a3cbf12614ec85ab99384cfcd64da9d91717aa849a8f4d"
EXPECTED_COMMON_SDIST = "074d6f58a8aec2eb7977758c2eb1f6aaea039dc9c8c58742ebe8778b7c1f9764"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _binding(**changes: object) -> OciDistributionBinding:
    values: dict[str, object] = {
        "resource": "object:evidence.objects",
        "endpoint": "https://registry.invalid",
        "repository": "meridian/evidence",
        "cursor_signing_key": b"deterministic-evidence-key",
    }
    values.update(changes)
    return OciDistributionBinding(**values)  # type: ignore[arg-type]


def verify() -> dict[str, object]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    assert project["name"] == "meridian-storage-oci"
    assert project["version"] == __version__ == "1.0.3"
    assert project["license"] == "Apache-2.0"
    assert project["dependencies"] == [
        "httpx==0.28.1",
        "meridian-storage-object-common==1.0.2",
    ]
    project_manifests = sorted(
        path for path in ROOT.rglob("pyproject.toml") if ".venv" not in path.relative_to(ROOT).parts
    )
    assert project_manifests == [ROOT / "pyproject.toml"]
    adapter_roots = sorted(
        item.name for item in (ROOT / "src/meridian_storage/adapters").iterdir() if item.is_dir()
    )
    assert adapter_roots == ["oci"]

    root_compatibility = _load(ROOT / "compatibility.json")
    packaged_compatibility = dict(compatibility_document())
    assert root_compatibility == packaged_compatibility
    assert root_compatibility["adapterId"] == ADAPTER_ID == "oci-distribution"
    assert root_compatibility["adapterContractVersion"] == ADAPTER_CONTRACT_VERSION
    assert root_compatibility["version"] == __version__
    assert root_compatibility["design"] == {
        "catalogsRevision": 70,
        "hldRevision": 56,
        "objectLldRevision": 12,
    }
    common = root_compatibility["objectCommon"]
    assert isinstance(common, dict)
    assert common == {
        "distribution": "meridian-storage-object-common",
        "sdistSha256": EXPECTED_COMMON_SDIST,
        "version": "1.0.2",
        "wheelSha256": EXPECTED_COMMON_WHEEL,
    }
    assert root_compatibility["standards"] == {
        "ociDistribution": OCI_DISTRIBUTION_VERSION,
        "ociImage": "1.1.1",
    }

    contract = _load(ROOT / "contracts/meridian-storage-oci.v1.json")
    assert contract["distribution"] == project["name"]
    assert contract["version"] == project["version"]
    assert contract["adapterId"] == ADAPTER_ID
    assert contract["objectCommon"] == "1.0.2"
    assert contract["operations"] == ["delete", "get", "list", "put", "read_range", "stat"]

    entry_points = metadata.entry_points(group="meridian_storage.adapters")
    selected = [entry for entry in entry_points if entry.name == ADAPTER_ID]
    assert len(selected) == 1
    assert selected[0].value == "meridian_storage.adapters.oci._factory:OciAdapterFactory"
    assert isinstance(selected[0].load()(), AdapterFactory)
    assert metadata.version("meridian-storage-oci") == __version__
    assert metadata.version("meridian-storage-object-common") == "1.0.2"
    assert metadata.version("meridian-storage-core") == "1.0.1"
    assert metadata.version("meridian-storage-semantics") == "2.0.0"

    source_files = sorted((ROOT / "src").rglob("*.py"))
    test_files = sorted((ROOT / "tests").rglob("*.py"))
    script_files = sorted((ROOT / "scripts").rglob("*.py"))
    for path in (*source_files, *test_files, *script_files):
        first_lines = path.read_text(encoding="utf-8").splitlines()[:3]
        assert any("SPDX-License-Identifier: Apache-2.0" in line for line in first_lines)

    restricted = configured_capability_manifest(_binding())
    managed = configured_capability_manifest(
        _binding(deletion_enabled=True, retention_enforced=True, referrers_required=True)
    )
    assert "meridian.object.delete" not in restricted.available_operation_contracts
    assert "meridian.object.delete" in managed.available_operation_contracts

    return {
        "formatVersion": "meridian-storage-oci-verification.v1",
        "adapterId": ADAPTER_ID,
        "version": __version__,
        "contractSha256": _sha256(ROOT / "contracts/meridian-storage-oci.v1.json"),
        "compatibilitySha256": _sha256(ROOT / "compatibility.json"),
        "restrictedCapabilityFingerprint": restricted.fingerprint,
        "managedCapabilityFingerprint": managed.fingerprint,
        "sourceFileCount": len(source_files),
        "testFileCount": len(test_files),
        "status": "passed",
    }


def main() -> None:
    print(json.dumps(verify(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
