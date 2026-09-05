# SPDX-License-Identifier: Apache-2.0
"""Generate a deterministic SPDX 2.3 JSON SBOM from the locked runtime closure."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tomllib
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ROOT_PACKAGE = "meridian-storage-oci"


def _spdx_id(name: str) -> str:
    return "SPDXRef-Package-" + re.sub(r"[^A-Za-z0-9.-]", "-", name)


def _created() -> str:
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _locked_runtime_packages() -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    indexed = {item["name"]: item for item in lock["package"]}
    selected: dict[str, dict[str, Any]] = {}
    relationships: set[tuple[str, str]] = set()
    pending = deque([ROOT_PACKAGE])
    while pending:
        name = pending.popleft()
        if name in selected:
            continue
        package = indexed[name]
        selected[name] = package
        for dependency in package.get("dependencies", []):
            dependency_name = dependency["name"]
            relationships.add((name, dependency_name))
            pending.append(dependency_name)
    return [selected[name] for name in sorted(selected)], sorted(relationships)


def generate() -> dict[str, Any]:
    locked, dependencies = _locked_runtime_packages()
    identity = json.dumps(
        [(item["name"], item["version"]) for item in locked],
        separators=(",", ":"),
    ).encode()
    namespace_hash = hashlib.sha256(identity).hexdigest()
    packages: list[dict[str, Any]] = []
    for item in locked:
        name = item["name"]
        package = {
            "SPDXID": _spdx_id(name),
            "name": name,
            "versionInfo": item["version"],
            "downloadLocation": (
                "NOASSERTION"
                if name == ROOT_PACKAGE
                else f"https://pypi.org/project/{name}/{item['version']}/"
            ),
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "Apache-2.0"
            if name.startswith("meridian-storage-")
            else "NOASSERTION",
            "copyrightText": "NOASSERTION",
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": f"pkg:pypi/{name}@{item['version']}",
                }
            ],
        }
        sdist = item.get("sdist")
        if isinstance(sdist, dict) and isinstance(sdist.get("hash"), str):
            algorithm, value = sdist["hash"].split(":", 1)
            if algorithm == "sha256":
                package["checksums"] = [{"algorithm": "SHA256", "checksumValue": value}]
        packages.append(package)
    relationships = [
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": _spdx_id(ROOT_PACKAGE),
        }
    ]
    relationships.extend(
        {
            "spdxElementId": _spdx_id(source),
            "relationshipType": "DEPENDS_ON",
            "relatedSpdxElement": _spdx_id(target),
        }
        for source, target in dependencies
    )
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "meridian-storage-oci-1.0.1-runtime",
        "documentNamespace": (
            "https://github.com/zephytiju/MeridianOCIAdapter/sbom/" + namespace_hash
        ),
        "creationInfo": {
            "created": _created(),
            "creators": ["Tool: meridian-storage-oci/scripts/generate_sbom.py"],
            "licenseListVersion": "3.27.0",
        },
        "packages": packages,
        "relationships": relationships,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    encoded = json.dumps(generate(), indent=2, sort_keys=True) + "\n"
    if arguments.output is None:
        print(encoded, end="")
    else:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded, encoding="utf-8")


if __name__ == "__main__":
    main()
