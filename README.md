<!-- SPDX-License-Identifier: Apache-2.0 -->

# Meridian Storage OCI

`meridian-storage-oci` is the Meridian V1 Object Adapter for an OCI
Distribution 1.1 registry. Its stable Adapter id is `oci-distribution`. It
implements streaming blob upload and download, digest verification, OCI image
manifests for non-container artifacts, exact versions, range reads, bounded
maintenance inventory, policy-aware deletion, resumable multipart upload,
capability probes, and logical migration helpers behind the released
`meridian-storage-object-common==1.0.0` contract.

This repository publishes exactly one Python distribution. Provider endpoints,
repository names, credentials, upload locations, tags, and manifest digests are
private binding state and never appear in consumer Object results or errors.

## Install

```console
python -m pip install meridian-storage-oci==1.0.0
```

Python 3.12 or newer is required.

## Composition-root use

Consumers continue to call `meridian.catalog("object")`. Platform composition
creates one adapter per Object Resource binding:

```python
from meridian_storage import ResourceRef
from meridian_storage.adapters.oci import (
    AnonymousCredentials,
    OciDistributionAdapter,
    OciDistributionBinding,
)

binding = OciDistributionBinding(
    resource=ResourceRef("object", "assets", "releases"),
    endpoint="https://registry.example.com",
    repository="meridian/releases",
    credentials=AnonymousCredentials(),
    conditional_create_mode="registry-enforced",
    deletion_enabled=True,
)
adapter = OciDistributionAdapter(binding)
```

The binding belongs to Platform/Vangu IaC. Application code must not construct
this adapter, persist its physical identifiers, or receive its configuration.

## OCI representation

Each logical Object id owns a deterministic anchor manifest. Every immutable
Object version is an OCI image manifest whose `subject` is that anchor, whose
config blob contains the exact portable Object metadata, and whose single layer
is the Object payload. Immutable version tags are derived from hashes of the
logical id and content digest. A private latest tag provides digest-less lookup;
it is never a consumer release-channel API.

The Adapter records retention intent and enforces `retainUntil` before delete.
It does not claim WORM or regulatory certification. Registry-side immutable-tag
and retention enforcement are advertised only when the deployment explicitly
declares those verified controls.

## Verification

```console
uv sync --all-extras
uv run ruff format --check src tests scripts
uv run ruff check src tests scripts
uv run mypy src
uv run pytest
uv run python scripts/verify_contracts.py
uv build
```

The integration suite uses the official Distribution reference registry. Set
`MERIDIAN_OCI_TEST_ENDPOINT` and `MERIDIAN_OCI_TEST_REPOSITORY` to run it against
another genuine OCI-compatible registry. Owner-managed registry variants are an
explicit gate; mocks are used only for deterministic transport unit tests and
are never reported as real-service acceptance.

See [architecture](ARCHITECTURE.md),
[configuration](docs/configuration.md), [conformance](docs/conformance.md), and
[migration](docs/migration.md).

## License

Copyright 2026 Meridian contributors. Licensed under Apache License 2.0; see
[`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
