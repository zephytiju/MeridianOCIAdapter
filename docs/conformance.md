<!-- SPDX-License-Identifier: Apache-2.0 -->

# Conformance and evidence

Acceptance is layered so deterministic tests are not misrepresented as a real
service result.

## Deterministic layer

`tests/support/registry.py` is an HTTP-level OCI Distribution harness. It
implements upload sessions, blob HEAD/GET/ranges, manifests, tags, referrers,
deletion, authentication, and OCI error envelopes behind `httpx.MockTransport`.
It is used for unit and contract determinism only.

The released Object Common runner executes nine provider-neutral checks:
unknown-length streaming put, stat identity, streaming get, inclusive range,
bounded prefix list, conditional-create conflict, digest mismatch, exact-version
delete, and missing-object behavior.

```console
uv run pytest -m 'not integration'
```

The configured gate requires at least 95% branch coverage. The initial 1.0.0
evidence records 130 deterministic tests passing at 97.89% branch coverage.

## Genuine reference-registry layer

The `integration` test runs the same released conformance runner and deep probe
against the official Distribution registry 3.1.1 image. CI pins the image by
multi-platform digest. The test appends a cryptographically random repository
component to the configured prefix. Its cleanup can therefore touch only
manifests created by that run; it never clears a pre-existing repository.

```console
MERIDIAN_OCI_TEST_ENDPOINT=http://127.0.0.1:5000 \
MERIDIAN_OCI_TEST_REPOSITORY=meridian-ci \
uv run pytest -m 'integration and not owner_service'
```

This layer is genuine OCI-compatible service evidence. It is not a claim about
any managed registry's IAM, retention, immutable-tag, redirect, or rate-limit
behavior.

## Owner-managed service gate

Managed registry verification is intentionally separate and opt-in:

```console
MERIDIAN_OCI_OWNER_ENDPOINT=https://registry.example.com \
MERIDIAN_OCI_OWNER_REPOSITORY=dedicated-meridian-test-prefix \
MERIDIAN_OCI_OWNER_TOKEN=... \
MERIDIAN_OCI_OWNER_REQUIRE_REFERRERS=true \
uv run pytest -m owner_service
```

The configured repository value is a prefix; the test creates a random child
repository. Credentials must allow push, pull, range, referrers, and manifest
delete in that child scope. This owner-only gate is the place to verify a
particular provider's IAM, immutable-tag, retention, redirect, and throttling
configuration. A skipped owner gate is reported as skipped, never replaced by a
mock or claimed as managed-service acceptance.

## Artifact evidence

`scripts/verify_contracts.py` checks the locked design revisions, released
Object Common version and artifact digests, packaged compatibility document,
public contract, package namespace, and capability fingerprints.
`scripts/generate_sbom.py` creates a deterministic SPDX 2.3 runtime SBOM from
the locked dependency closure. `scripts/verify_artifacts.py` checks wheel and
sdist contents and metadata. CI uploads all three outputs with the distributions.
