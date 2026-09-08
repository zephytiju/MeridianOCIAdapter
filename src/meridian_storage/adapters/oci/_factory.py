# SPDX-License-Identifier: Apache-2.0
"""Private bridge from the released Core factory SPI to the OCI data plane."""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from meridian_storage.object_common import (
    ObjectCapabilityMismatch,
    ObjectInvalidRequest,
    ObjectUnavailable,
    default_payload_registry,
)
from meridian_storage.semantics import JsonValue, sha256_fingerprint
from meridian_storage.spi import (
    AdapterCreateContext,
    AdapterProbe,
    AdapterSession,
    ExecutionRequest,
    ExecutionResult,
    PhysicalResource,
    PhysicalVerification,
)

from ._version import __version__
from .adapter import OciDistributionAdapter
from .descriptor import (
    ADAPTER_CONTRACT_VERSION,
    ADAPTER_ID,
    ENGINE_PROFILE,
    OCI_DISTRIBUTION_VERSION,
    AnonymousCredentials,
    BasicCredentials,
    BearerCredentials,
    OciDistributionBinding,
)
from .probe import OciDistributionProbe

# These settings map only to the existing private binding fields. Endpoints,
# namespaces, credentials and TLS material come from their Core-owned fields.
_SETTINGS = {
    "resource": "resource",
    "registryRelease": "registry_release",
    "registryImage": "registry_image",
    "chunkSize": "chunk_size",
    "maxObjectBytes": "max_object_bytes",
    "maxRangeBytes": "max_range_bytes",
    "maxManifestBytes": "max_manifest_bytes",
    "maxMetadataBytes": "max_metadata_bytes",
    "maxListPageSize": "max_list_page_size",
    "maxScanTags": "max_scan_tags",
    "maxMultipartParts": "max_multipart_parts",
    "conditionalCreateMode": "conditional_create_mode",
    "deletionEnabled": "deletion_enabled",
    "retentionEnforced": "retention_enforced",
    "referrersRequired": "referrers_required",
}


class OciAdapterFactory:
    """No-argument discovery target; Core supplies the private create context."""

    adapter_id = ADAPTER_ID

    def create(self, context: AdapterCreateContext) -> _Runtime:
        if not isinstance(context, AdapterCreateContext):
            raise TypeError("OCI factory requires AdapterCreateContext")
        binding = context.binding
        if binding.adapter_id != ADAPTER_ID or binding.endpoint is None:
            raise ObjectInvalidRequest(
                "OCI factory requires an OCI binding with a resolved endpoint"
            )
        if binding.adapter_contract != ADAPTER_CONTRACT_VERSION:
            raise ObjectCapabilityMismatch("unsupported OCI Adapter SPI contract")
        if binding.engine_profile != ENGINE_PROFILE:
            raise ObjectCapabilityMismatch("unsupported OCI engine profile")
        # Legacy engineVersion is the Distribution protocol, never registry software.
        if binding.engine_version != OCI_DISTRIBUTION_VERSION:
            raise ObjectCapabilityMismatch("OCI requires Distribution specification 1.1.1")
        settings = binding.settings
        if set(settings) - {*_SETTINGS, "authMode"} or "resource" not in settings:
            raise ObjectInvalidRequest("OCI binding requires resource and only supported settings")
        mode = binding.tls.mode
        scheme = urlsplit(binding.endpoint).scheme
        if mode == "mutual" or (mode == "disabled" and scheme != "http"):
            raise ObjectInvalidRequest("OCI binding TLS policy is unsupported for this endpoint")
        if mode != "disabled" and scheme != "https":
            raise ObjectInvalidRequest("OCI TLS requires an HTTPS endpoint")
        if mode == "server" and context.tls_ca is None:
            raise ObjectInvalidRequest("OCI server TLS requires resolved CA material")
        credentials: AnonymousCredentials | BasicCredentials | BearerCredentials
        try:
            auth = settings.get("authMode", "basic")
            if auth == "anonymous":
                credentials = AnonymousCredentials()
            elif auth == "basic":
                credentials = BasicCredentials(
                    context.identity.reveal().decode("utf-8"),
                    context.credential.reveal().decode("utf-8"),
                )
            elif auth == "bearer":
                credentials = BearerCredentials(context.credential.reveal().decode("utf-8"))
            else:
                raise ValueError("unsupported authentication mode")
            values = {_SETTINGS[key]: value for key, value in settings.items() if key in _SETTINGS}
            config = OciDistributionBinding(
                **values,
                endpoint=binding.endpoint,
                repository=binding.physical_namespace,
                credentials=credentials,
                allow_insecure_http=mode == "disabled",
                verify_tls=mode != "disabled",
                timeout_seconds=binding.client.operation_timeout_ms / 1000,
            )
        except (TypeError, ValueError):
            raise ObjectInvalidRequest(
                "OCI private binding or credential configuration is invalid"
            ) from None
        return _Runtime(config, context.tls_ca.reveal() if context.tls_ca is not None else None)


class _Runtime:
    def __init__(self, binding: OciDistributionBinding, tls_ca: bytes | None) -> None:
        self._binding = binding
        self._tls_ca = tls_ca
        self._ca_directory: tempfile.TemporaryDirectory[str] | None = None
        self._adapter: OciDistributionAdapter | None = None
        self._probe: AdapterProbe | None = None
        self._verified = False
        self._payloads = default_payload_registry()

    def open(self) -> None:
        if self._adapter is not None:
            return
        try:
            binding = self._binding
            if self._tls_ca is not None:
                self._ca_directory = tempfile.TemporaryDirectory(prefix="meridian-oci-ca-")
                ca_path = Path(self._ca_directory.name) / "ca.pem"
                ca_path.touch(mode=0o600)
                ca_path.write_bytes(self._tls_ca)
                binding = replace(binding, verify_tls=str(ca_path))
            self._adapter = OciDistributionAdapter(binding)
            report = OciDistributionProbe(self._adapter.transport).authenticated()
            if not report.passed:
                raise ObjectUnavailable("OCI authenticated startup probe failed")
            self._probe = AdapterProbe(
                report.capability_manifest,
                {
                    "authenticated": "true",
                    "apiVersion": report.api_version,
                    "distributionSpec": OCI_DISTRIBUTION_VERSION,
                    "registryReleaseObservation": "unavailable",
                },
            )
        except BaseException:
            self.close()
            raise

    def _require_open(self) -> OciDistributionAdapter:
        if self._adapter is None or self._probe is None:
            raise ObjectInvalidRequest("OCI runtime is not open")
        return self._adapter

    def probe(self) -> AdapterProbe:
        self._require_open()
        assert self._probe is not None
        return self._probe

    def verify_physical(self, resources: tuple[PhysicalResource, ...]) -> PhysicalVerification:
        adapter = self._require_open()
        self._verified = False
        if len(resources) != 1 or resources[0].resource_ref != self._binding.resource_ref:
            raise ObjectInvalidRequest(
                "OCI binding must select its single configured Object Resource"
            )
        adapter.transport.ping()
        adapter.transport.list_tags(limit=1)
        resource = resources[0]
        namespace = sha256_fingerprint(
            {"endpoint": self._binding.endpoint, "repository": self._binding.repository}
        )
        fingerprint = sha256_fingerprint(
            {
                "formatVersion": "meridian.oci-physical-verification.v1",
                "namespaceFingerprint": namespace,
                "resource": str(resource.resource_ref),
                "resourceFingerprint": resource.resource_fingerprint,
                "schemaFingerprint": resource.schema_fingerprint,
                "profile": resource.profile,
            }
        )
        self._verified = True
        return PhysicalVerification(
            fingerprint,
            {str(resource.resource_ref): f"oci:{fingerprint}"},
            {"authenticated": "true", "resourceCount": "1"},
        )

    def open_session(self, *, transactional: bool) -> AdapterSession:
        self._require_open()
        if transactional:
            raise ObjectCapabilityMismatch("OCI Object Operations are not transactional")
        if not self._verified:
            raise ObjectInvalidRequest("OCI physical binding has not been verified")
        return _Session(self)

    def close(self) -> None:
        adapter, self._adapter = self._adapter, None
        self._probe = None
        self._verified = False
        try:
            if adapter is not None:
                adapter.close()
        finally:
            if self._ca_directory is not None:
                self._ca_directory.cleanup()
                self._ca_directory = None


class _Session:
    def __init__(self, runtime: _Runtime) -> None:
        self._runtime = runtime
        self._closed = False

    def _require_open(self) -> OciDistributionAdapter:
        if self._closed:
            raise ObjectInvalidRequest("OCI session is closed")
        return self._runtime._require_open()

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        adapter = self._require_open()
        if not isinstance(request, ExecutionRequest):
            raise TypeError("OCI session requires ExecutionRequest")
        result = adapter.execute(request.operation, self._runtime._payloads)
        manifest = self._runtime.probe().manifest
        return ExecutionResult(
            data=cast(JsonValue, result),
            provenance={
                "adapterId": ADAPTER_ID,
                "adapterVersion": __version__,
                "capabilityFingerprint": manifest.fingerprint,
                "engineProfile": manifest.engine_profile,
                "engineVersion": manifest.engine_version,
                "distributionSpec": OCI_DISTRIBUTION_VERSION,
                "registryReleaseObservation": "unavailable",
                **(
                    {"selectedRegistryRelease": self._runtime._binding.registry_release}
                    if self._runtime._binding.registry_release is not None
                    else {}
                ),
                **(
                    {"selectedRegistryImage": self._runtime._binding.registry_image}
                    if self._runtime._binding.registry_image is not None
                    else {}
                ),
            },
        )

    def begin(self) -> None:
        self._require_open()
        raise ObjectCapabilityMismatch("OCI Object Operations are not transactional")

    def commit(self) -> None:
        self.begin()

    def rollback(self) -> None:
        self.begin()

    def close(self) -> None:
        self._closed = True
