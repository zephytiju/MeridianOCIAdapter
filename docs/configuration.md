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

## Normal Core startup

The installed `meridian_storage.adapters` entry point loads a private factory.
Core supplies `AdapterCreateContext`; neither consumers nor ResourceStore inject
an adapter factory. The existing `OciDistributionAdapter(binding)` composition
API remains available with the same signature and behavior.

For `meridian-config.v1` deployment configuration:

- `adapterId` is `oci-distribution`, `adapterContract` is `1.0.0`, and the existing
  engine profile/version remain `oci-distribution` / `1.1.1`.
- `endpoint` supplies the registry origin; `physicalNamespace` supplies the
  existing repository. Resolve `serviceRef` in deployment composition first.
- `settings.resource` is the one logical Object Resource served by the binding,
  for example `object:resources.objects`. Placement must select that exact
  resource. This preserves the existing one-resource-per-OCI-binding model.
- Optional settings map to the existing private fields: `chunkSize`,
  `maxObjectBytes`, `maxRangeBytes`, `maxManifestBytes`, `maxMetadataBytes`,
  `maxListPageSize`, `maxScanTags`, `maxMultipartParts`, `conditionalCreateMode`,
  `deletionEnabled`, `retentionEnforced`, and `referrersRequired`. Defaults and
  validation are unchanged. Unknown settings fail closed.
- `client.operationTimeoutMs` supplies the HTTP timeout. TLS `server` uses the
  resolved CA material in a private temporary file removed on close or failed
  startup. `disabled` requires HTTP; mutual TLS is unsupported by this bridge.
- `settings.authMode` maps to existing credentials: `basic` (default) takes the
  resolved UTF-8 identity and credential as username/password; `bearer` takes
  the resolved credential as token; `anonymous` explicitly selects anonymous
  access. Core still resolves its required opaque secret references. Credentials
  are never accepted in settings. This does not change secret provisioning or
  token refresh ownership.

Startup runs only the existing authenticated read-only probe. Deep registry
verification remains an explicit deployment job. Core checks the existing
configured capability fingerprint, and physical verification returns opaque,
configuration-sensitive fingerprints without exposing endpoints or repositories.
The bridge uses Object Common's process-local default payload registry, matching
ResourceStore's normal Object path. Explicit private binding APIs retain their
existing custom credential and cursor-key options.

## Protocol and registry release selection (1.1.0)

`Binding.engineVersion` remains the **OCI Distribution specification** `1.1.1`.
It must not contain a registry software version such as `3.1.1`. The factory
checks the Adapter SPI `1.0.0`, `oci-distribution` profile and this protocol
independently of deployment-selected releases.

The optional private settings `registryRelease` and `registryImage` map to
`OciDistributionBinding.registry_release` and `.registry_image`. The former is
a nonempty bounded provenance label, not an allowlist entry. The latter must be
an immutable image reference ending in `@sha256:<64 lowercase hex characters>`.
Deployment still owns image resolution and verifies the selected artifact.

These settings appear as `selectedRegistry` in configured capability extensions.
Selecting or changing them changes the canonical capability fingerprint; render
the deployment's expected fingerprint from that same configuration. Startup
continues to reject mismatched expectations. With both settings omitted, the
legacy manifest and fingerprint remain byte-for-byte equivalent to the public
OCI 1.0.3 golden fixture. No persisted field changes meaning.

The probe report adds `registryProvenance` to its v1 document. `observedRelease`
is null and `observationStatus` is `unavailable`: the Distribution API does not
provide a portable authenticated registry software version endpoint. The optional
`Docker-Distribution-Api-Version` header is an API marker, never a software
release or proof of Distribution 1.1.1 feature conformance. Configured release
labels are never copied into observed evidence. The CI fixture records the
actual `registry --version` and running image identity separately through the
container runtime. This out-of-band observation does not become an adapter API
claim. Probe features continue to reflect real operations.
