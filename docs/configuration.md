<!-- SPDX-License-Identifier: Apache-2.0 -->

# Configuration

`OciDistributionBinding` is private composition-root state. Platform/Vangu IaC
constructs one binding per Meridian Object Resource; application code does not.

## Required fields

| Field | Meaning |
| --- | --- |
| `resource` | logical Meridian Object Resource reference |
| `endpoint` | registry origin only; no path, query, fragment, or userinfo |
| `repository` | already-authorized OCI repository name |

HTTPS is required by default. `allow_insecure_http=True` exists for an explicitly
trusted local test registry and must not be used to weaken production transport.
`verify_tls` accepts `True`, `False`, or a non-empty CA bundle path. Disabling
verification is an IaC security decision and is unsuitable for production.

## Credentials

The binding accepts one private `CredentialProvider`:

- `AnonymousCredentials` for an intentionally public or local repository;
- `BasicCredentials`, with its password excluded from representations;
- `BearerCredentials`, with its token excluded from representations; or
- `CallbackCredentials`, which resolves a current Authorization value at request
  time and can integrate with an external credential rotation mechanism.

Credentials are never serialized, persisted by the Adapter, included in the
capability fingerprint, or forwarded to another origin. Tenancy, identity,
secret retrieval, and token refresh policy remain outside this package.

## Bounds and behavior

| Field | Default | Effect |
| --- | ---: | --- |
| `timeout_seconds` | 30 | per-request HTTP timeout |
| `chunk_size` | 4 MiB | stream and multipart chunk size |
| `max_object_bytes` | 5 TiB | maximum accepted payload |
| `max_range_bytes` | 256 MiB | maximum one-shot range |
| `max_manifest_bytes` | 4 MiB | manifest response bound |
| `max_metadata_bytes` | 512 KiB | portable metadata/config bound |
| `max_list_page_size` | 1000 | advertised consumer page limit |
| `max_scan_tags` | 2000 | maximum tags scanned by one list call |
| `max_multipart_parts` | 10,000 | session part-count limit |
| `conditional_create_mode` | `single-writer` | declared conditional-write model |
| `deletion_enabled` | false | expose exact-version delete |
| `retention_enforced` | false | advertise externally verified retention enforcement |
| `referrers_required` | false | require referrers discovery in a deep probe |

`cursor_signing_key` should be supplied by IaC when maintenance cursors need to
remain valid across Adapter process replacement. If omitted, the Adapter creates
a random process-local key; existing cursors then expire when that Adapter is
replaced. It is not a consumer identifier.

## Deployment verification

Run `OciDistributionProbe.authenticated()` for non-mutating `/v2/` and bounded
repository tag-list checks. Run `deep()` only against a repository where the caller is authorized to push,
range-read, discover referrers, and—when enabled—delete canary manifests.
Deep-probe content uses unique private tags. It does not provision or change the
repository.

The capability manifest is configuration-sensitive. Deletion is absent until
explicitly enabled, and retention enforcement is absent until the deployment
declares that owner-managed controls were verified.
