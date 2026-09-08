# SPDX-License-Identifier: Apache-2.0
"""Authenticated and explicit write-path probes for OCI registry variance."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from meridian_storage.semantics import JsonValue
from meridian_storage.spi import CapabilityManifest

from .._json import canonical_json
from .._naming import object_id_hash
from ..descriptor import OCI_DISTRIBUTION_VERSION, configured_capability_manifest
from ..transport import (
    EMPTY_JSON,
    MERIDIAN_OBJECT_ARTIFACT_TYPE,
    MERIDIAN_OBJECT_CONFIG_MEDIA_TYPE,
    OciDescriptor,
    RegistryHttpClient,
    build_anchor_manifest,
    build_object_manifest,
)


@dataclass(frozen=True, slots=True)
class OciProbeReport:
    passed: bool
    mode: str
    api_version: str
    push_pull: bool
    range_read: bool
    referrers: bool
    deletion: bool
    capability_manifest: CapabilityManifest
    failure: str | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        selected = self.capability_manifest.extensions.get("selectedRegistry", {})
        assert isinstance(selected, Mapping)
        return {
            "formatVersion": "meridian.oci-probe.v1",
            "passed": self.passed,
            "mode": self.mode,
            "apiVersion": self.api_version,
            "checks": {
                "pushPull": self.push_pull,
                "rangeRead": self.range_read,
                "referrers": self.referrers,
                "deletion": self.deletion,
            },
            "capabilityFingerprint": self.capability_manifest.fingerprint,
            "failure": self.failure,
            "registryProvenance": {
                "distributionSpec": OCI_DISTRIBUTION_VERSION,
                "selected": dict(selected),
                "observedRelease": None,
                "observationStatus": "unavailable",
            },
        }


class OciDistributionProbe:
    def __init__(self, transport: RegistryHttpClient) -> None:
        self.transport = transport

    def authenticated(self) -> OciProbeReport:
        manifest = configured_capability_manifest(self.transport.binding)
        try:
            result = self.transport.ping()
            self.transport.list_tags(limit=1)
        except Exception as exc:
            return OciProbeReport(
                False,
                "authenticated",
                "unknown",
                False,
                False,
                False,
                False,
                manifest,
                type(exc).__name__,
            )
        return OciProbeReport(
            True,
            "authenticated",
            result["apiVersion"],
            False,
            False,
            False,
            False,
            manifest,
        )

    def deep(self) -> OciProbeReport:
        """Write and remove canary manifests; callers must opt in explicitly."""

        manifest = configured_capability_manifest(self.transport.binding)
        token = secrets.token_hex(12)
        anchor_reference = f"m-probe-a-{token}"
        version_reference = f"m-probe-v-{token}"
        api_version = "unknown"
        push_pull = range_read = referrers = deletion = False
        anchor: OciDescriptor | None = None
        version: OciDescriptor | None = None
        try:
            api_version = self.transport.ping()["apiVersion"]
            payload_value = b"meridian-oci-distribution-probe"
            payload = self.transport.upload_blob_bytes(
                payload_value,
                media_type="application/octet-stream",
            )
            self.transport.upload_blob_bytes(
                EMPTY_JSON,
                media_type="application/vnd.oci.empty.v1+json",
            )
            anchor = self.transport.put_manifest(
                anchor_reference,
                build_anchor_manifest(object_hash=object_id_hash(token)),
            )
            config_value = canonical_json({"probe": True})
            config = self.transport.upload_blob_bytes(
                config_value,
                media_type=MERIDIAN_OBJECT_CONFIG_MEDIA_TYPE,
            )
            version = self.transport.put_manifest(
                version_reference,
                build_object_manifest(
                    anchor=anchor,
                    config=config,
                    payload=payload,
                    object_hash=object_id_hash(token),
                    created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    mutability="immutable",
                ),
            )
            pulled = self.transport.get_manifest(version_reference)
            push_pull = pulled.digest == version.digest
            with self.transport.open_blob(payload.digest, start=1, end=3) as stream:
                range_read = stream.read() == payload_value[1:4]
            discovered = self.transport.get_referrers(
                anchor.digest,
                artifact_type=MERIDIAN_OBJECT_ARTIFACT_TYPE,
            )
            referrers = any(item.digest == version.digest for item in discovered)
            if self.transport.binding.referrers_required and not referrers:
                raise ValueError("registry does not provide required OCI referrers discovery")
            if self.transport.binding.deletion_enabled:
                deletion = self.transport.delete_manifest(version.digest)
                version = None
                deletion = self.transport.delete_manifest(anchor.digest) and deletion
                anchor = None
            passed = (
                push_pull
                and range_read
                and (referrers or not self.transport.binding.referrers_required)
            )
            if self.transport.binding.deletion_enabled:
                passed = passed and deletion
            return OciProbeReport(
                passed,
                "deep",
                api_version,
                push_pull,
                range_read,
                referrers,
                deletion,
                manifest,
            )
        except Exception as exc:
            return OciProbeReport(
                False,
                "deep",
                api_version,
                push_pull,
                range_read,
                referrers,
                deletion,
                manifest,
                type(exc).__name__,
            )
        finally:
            if self.transport.binding.deletion_enabled:
                _delete_safely(self.transport, version)
                _delete_safely(self.transport, anchor)


def _delete_safely(transport: RegistryHttpClient, descriptor: OciDescriptor | None) -> None:
    if descriptor is None:
        return
    try:
        transport.delete_manifest(descriptor.digest)
    except Exception:
        return


__all__ = ["OciDistributionProbe", "OciProbeReport"]
