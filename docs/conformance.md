<!-- SPDX-License-Identifier: Apache-2.0 -->

# Conformance and evidence

Acceptance is layered so deterministic tests are not misrepresented as a real
service result.

## Compatible release set

OCI 1.0.2 consumes the published Object Common 1.0.2 fixtures and resolves
Core 1.0.1 with Semantics 2.0.0. The package gate installs the built wheel
with normal registry dependency resolution and checks the installed closure.
No sibling source, dependency override or metadata patch is used.

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

## Deployment-owned release repair: 1.1.0

The compatibility document's exact Object Common coordinate and hashes are the
release-validation recipe. Runtime installation uses compatible public API bounds:
Core `>=1.1,<2` (released provenance/contract separation), Object Common
`>=1.0.3,<2` (compatible Core closure, unchanged Object API), and HTTPX
`>=0.28.1,<0.29` (the transport API used here). `uv.lock` keeps the tested closure
exact with public artifact hashes: Core 1.1.0, Common 1.0.3, Semantics 2.0.1 and
HTTPX 0.28.1. The contract evidence script verifies the selected repository lock;
it is not called by runtime startup and imposes no compiled engine release list.

| Surface | Classification | Retained behavior |
| --- | --- | --- |
| Descriptor `supportedEngineVersions` and manifest `engineVersion` | Legacy protocol identity | Distribution spec 1.1.1, never registry software |
| Factory Adapter SPI/profile/protocol | Real contract requirement | Reject unsupported SPI, profile and protocol before opening transport |
| `registryRelease` / `registryImage` | Selected provenance / artifact integrity | Accept unlisted labels; validate shape and immutable image digest |
| Package API bounds | Public API compatibility | Clean public resolver installation and package check |
| Compatibility recipe, `uv.lock`, SBOM | Release evidence and lock integrity | Exact artifact hashes and installed-lock checks at release validation |
| Configured manifest / required fingerprint | Deployment drift | Like-for-like canonical comparison; changed selected provenance requires deployment update |
| Physical verification | Identity and schema drift | Resource, namespace and Schema fingerprints preserved |
| Auth/TLS/namespace | Provider security and identity | Real request authentication, TLS policy and scoped access checks preserved |
| Blob/manifest/media/range/referrers/deletion | Real feature requirements | Deep and operation probes use actual behavior; required failures remain failures |
| Object immutable publication, metadata and cleanup | Public semantic contract | Existing provider-neutral fixtures and Core execution regressions preserved |

The two exact CI registry selections are independent from the Python release:

| Registry software | Immutable multi-platform image digest |
| --- | --- |
| Distribution 3.0.0 | `sha256:6c5666b861f3505b116bb9aa9b25175e71210414bd010d92035ff64018f9457e` |
| Distribution 3.1.1 | `sha256:1be55279f18a2fe1a74edf2664cac61c1bea305b7b4642dab412e7affdcb3e33` |

Both support the tested single-writer Object configuration with deletion enabled.
Both lack native referrers discovery: `referrersRequired=true` is explicitly
**unsupported**, and the real negative test requires rejection after successful
push/pull and range checks. No native-referrers support is inferred from the
registry software release or Distribution specification label. Other providers,
registry-enforced immutable tags, WORM, and untested releases remain unverified.
The owner-service suite remains separate; its credentials are not needed for
these disposable reference-registry configurations.

The installed-wheel CI suite runs public Object conformance, normal Core
startup/execution with both legacy and selected provenance, immutable publication,
range/digest/delete regressions, required-referrers rejection and released S3
coexistence. CI retains JUnit, actual registry binary version/image identity and
installed package closure artifacts. Deterministic negative fixtures additionally
cover API/media/range/referrers/deletion/auth/TLS failures, malformed image locks,
unsupported protocols, and fingerprint drift despite a listed registry label.
The legacy manifest fixture was generated using the public OCI 1.0.3 wheel.

Historical ConfigArtifact 1.1.0 consumes OCI 1.0.3/Core 1.0.1/Common 1.0.2; that
history does not verify a new Core 1.1.0 combination. The new public ConfigArtifact
closure is verified by its downstream compatibility task after this OCI release.
No dependency overrides, sibling implementation imports, or skipped required
reference-registry tests substitute for that downstream evidence.
