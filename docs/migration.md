<!-- SPDX-License-Identifier: Apache-2.0 -->

# Logical migration

Migration is provider-neutral export/import. OCI repository replication, DNS,
identity, ACLs, lifecycle, rollback, and cutover orchestration remain
Platform/Vangu IaC responsibilities.

`OciLogicalMigration.export_logical()` resolves an exact or latest Object
reference and returns:

- portable `ObjectMetadata`;
- a replayable process-local payload reference;
- immutability intent; and
- retention intent.

No endpoint, repository, tag, manifest digest, upload URL, credential, tenancy,
or OCI SDK type enters the export. The source Adapter must remain available
until the payload reference has been consumed.

`import_logical()` submits a normal Object put through the target Adapter. It
preserves content digest, byte length, media type, user metadata, creation
context, producer provenance, immutability, and retention intent. Source
`meridianAdapter` provenance is removed and replaced with target Adapter
provenance after commit. The target Resource reference is always the target
binding, never the source physical or logical Resource.

The default `create_only=True` prevents silent overwrite during migration.
Orchestration should inventory exact versions, export/import each version,
verify target stat/get identities, switch consumer binding through IaC, retain a
rollback window, and apply source lifecycle only after independent acceptance.

Content-addressed blobs and manifests that become unreachable during a failed
migration are lifecycle cleanup concerns. This library does not delete them
automatically or claim repository-level recovery.
