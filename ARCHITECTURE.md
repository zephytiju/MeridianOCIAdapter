<!-- SPDX-License-Identifier: Apache-2.0 -->

# Architecture

This package is the Meridian V1 Object Adapter for an Open Container
Initiative Distribution 1.1 registry. It implements the released
`meridian-storage-object-common==1.0.2` contract and has the stable Adapter id
`oci-distribution`.

## Authority boundary

Consumers submit mapping-first Expressions that Core normalizes into serialized
Object Operations. They never select this Adapter or receive OCI repository
names, tags, upload URLs, registry manifest digests, credentials, or transport
objects. Platform/Vangu IaC owns Adapter selection, endpoint and repository
binding, identity and ACLs, encryption and WORM controls, lifecycle, migration
orchestration, backup, and recovery.

The package owns only data-plane translation within one already-provisioned
repository. It neither creates repositories nor changes registry policy.

## Logical representation

| Meridian concept | Private OCI representation |
| --- | --- |
| Object Resource | one IaC-bound OCI repository |
| logical Object id | SHA-256-derived anchor and latest tags |
| immutable Object version | OCI image manifest with one payload layer |
| Object metadata | canonical JSON config blob |
| payload identity | OCI blob digest (`sha256`) |
| version relationship | version manifest `subject` points to the anchor manifest |
| maintenance inventory | bounded tag listing of private version tags |

Object ids are never embedded in tags. The full id lives only in the portable
metadata config blob. Exact version tags combine a truncated object-id hash with
the complete content digest. This makes version publication deterministic while
keeping tag names within the OCI grammar.

The latest tag is a private digest-less lookup convenience, not a Meridian
release channel. Mutable release channels remain separate Meridian Records.

## Operation flow

`put` streams the source through SHA-256 while sending ordered PATCH requests to
an OCI upload session. The Adapter verifies declared length and digest before
finalizing the blob. It then creates or verifies the logical anchor, uploads the
canonical metadata config, publishes the exact version manifest, and updates the
private latest tag. Metadata is returned only after those commits succeed.

`get` and `read_range` return process-local `PayloadReference` values. Opening a
reference starts a registry response stream; payload bytes never enter a Core
JSON envelope. `stat` resolves and verifies the same manifest/config chain
without opening the payload. `list` scans a configured bounded tag window and
returns a signed opaque cursor. `delete` requires an exact digest and removes
the version manifest by digest only when the private binding enables deletion
and recorded retention intent permits it.

Multipart upload uses the registry's resumable upload session directly. Parts
must arrive in order, every non-final part must meet the advertised minimum,
and the final aggregate identity is checked before manifest publication.

## Consistency and immutability

`single-writer` conditional create is process-local and is appropriate only
where IaC guarantees one writer for a logical Object Resource. A deployment may
declare `registry-enforced` only after its registry policy has independently
verified immutable tags or an equivalent conditional-write control. The Adapter
never claims a stronger guarantee merely because a registry accepts an OCI
manifest.

Exact version tags are deterministic and never intentionally overwritten with
different content. A partial commit can leave content-addressed blobs or an
exact manifest without the latest alias; retrying a normal non-create-only put
repairs the latest alias. Content-addressed orphan cleanup remains lifecycle
authority owned by Platform IaC.

Retention intent is stored in portable metadata and checked before delete.
`retention_enforced=True` is a deployment assertion that external registry
controls were verified; this library does not configure WORM, certify
compliance, or represent a legal hold release decision.

## Failure and security model

All HTTP status and OCI error codes are translated to the released Object Common
error taxonomy. Provider diagnostics appear only in credential-free
`adapterProvenance`. Response bodies, endpoints, repositories, credentials, and
upload locations are not copied into public errors.

Authorization is added only when a request origin exactly matches the bound
registry origin. Cross-origin upload locations and blob redirects do not receive
the registry Authorization header. HTTPS is mandatory unless loopback or another
explicitly trusted HTTP endpoint is enabled by IaC. Digests, descriptor sizes,
manifest media types, metadata shape, subject links, ranges, and response
identities are verified at every resolution boundary.

Read-only HTTP requests have bounded retries. A streaming put is retried only
when the payload source is replayable and the failure is a stable transient
Object error. Validation, digest, and authorization failures are never retried.
