<!-- SPDX-License-Identifier: Apache-2.0 -->

# Security policy

Version 1.x receives security fixes while it is the current stable major.

Report vulnerabilities privately through GitHub Security Advisories for
`zephytiju/meridian-storage-oci`. Do not include live registry credentials,
tokens, private endpoints, tenancy identifiers, or customer payloads in an
issue, test fixture, or log excerpt.

Security reports should describe the affected version, attack preconditions,
provider behavior, and a minimal credential-free reproduction. Maintainers will
acknowledge a report, assess scope, coordinate a fix and advisory, and credit
the reporter when requested.

The Adapter deliberately scopes Authorization by origin, bounds all metadata
and maintenance reads, verifies SHA-256 identities, and emits provider-neutral
errors. Platform operators remain responsible for TLS trust, registry patching,
identity and ACLs, network policy, encryption, immutable tags, retention/WORM,
audit logging, backup, and recovery.
