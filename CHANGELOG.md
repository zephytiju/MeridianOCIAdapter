<!-- SPDX-License-Identifier: Apache-2.0 -->

# Changelog


## 1.1.0 — 2026-09-08

- Separate deployment-selected registry release/image provenance from the legacy
  Distribution 1.1.1 protocol identity; report unavailable observations honestly.
- Preserve legacy canonical manifests, validate actual SPI/profile/protocol and
  image integrity, and retain required provider feature and drift failures.
- Resolve Core 1.1.0/Object Common 1.0.3 through public API compatibility bounds.
- Test two independently selected registry releases, public Object/Core paths,
  negative required-referrers behavior, and released S3 coexistence.

## 1.0.3

- Restore normal Core discovery using a private no-argument AdapterFactory and the released runtime/session SPI.
- Bind the existing OCI data plane through deployment settings and resolved secrets, retaining Object semantics and authenticated read-only startup probes.
- Verify normal Core/Object execution against Distribution 3.1.1, including installed coexistence with S3.

## 1.0.2

- Consume released Object Common 1.0.2 with Core 1.0.1 and Semantics 2.0.0; align dependency locks, compatibility hashes and release evidence inputs.
- Preserve the Object operations, OCI wire formats, provider conformance and immutable publication behavior.

## 1.0.1

- Accept the released Object Common 1.0.1 compatibility fix so ResourceStore can retain its S3 and OCI installation extras in one tested dependency set. No OCI operation or wire-format changes.


## 1.0.0 - 2026-08-26

- Implement the Meridian V1 Object contract over OCI Distribution 1.1.
- Add streaming, digest verification, exact versions, ranges, bounded listing,
  exact-version delete, retention intent, multipart upload, probes, and logical
  migration.
- Add released Object Common conformance, deterministic protocol tests, genuine
  Distribution 3.1.1 integration, packaging checks, CI, SPDX SBOM generation,
  and Apache-2.0 release material.
