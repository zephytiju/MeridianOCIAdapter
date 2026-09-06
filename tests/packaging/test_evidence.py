# SPDX-License-Identifier: Apache-2.0
"""Repository contract and SBOM evidence is deterministic."""

from __future__ import annotations

import json

from scripts.generate_sbom import generate
from scripts.verify_contracts import verify


def test_contract_verification_document() -> None:
    evidence = verify()

    assert evidence["status"] == "passed"
    assert evidence["adapterId"] == "oci-distribution"
    assert evidence["version"] == "1.0.2"
    assert evidence["sourceFileCount"] == 17
    assert evidence["testFileCount"] >= 18
    assert str(evidence["contractSha256"]).isalnum()


def test_runtime_sbom_is_deterministic(monkeypatch: object) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1787702400")  # type: ignore[attr-defined]

    first = generate()
    second = generate()

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["spdxVersion"] == "SPDX-2.3"
    packages = {item["name"]: item["versionInfo"] for item in first["packages"]}
    assert packages["meridian-storage-oci"] == "1.0.2"
    assert packages["meridian-storage-object-common"] == "1.0.2"
    assert packages["httpx"] == "0.28.1"
    assert "pytest" not in packages
    assert first["creationInfo"]["created"] == "2026-08-26T00:00:00Z"
