# SPDX-License-Identifier: Apache-2.0
"""OCI Distribution Adapter descriptor and negotiated runtime capabilities."""

from __future__ import annotations

from meridian_storage.object_common import (
    GUARANTEE_BOUNDED_PREFIX_LIST,
    GUARANTEE_CONDITIONAL_CREATE,
    GUARANTEE_DIGEST_SHA256,
    GUARANTEE_DIGEST_VERIFICATION,
    GUARANTEE_EXACT_VERSION_DELETE,
    GUARANTEE_IMMUTABILITY_INTENT,
    GUARANTEE_METADATA_AFTER_COMMIT,
    GUARANTEE_MULTIPART,
    GUARANTEE_RANGE_READ,
    GUARANTEE_RETENTION_ENFORCEMENT,
    GUARANTEE_RETENTION_INTENT,
    GUARANTEE_STREAMING,
    LIMIT_MAX_LIST_PAGE_SIZE,
    LIMIT_MAX_MULTIPART_PART_BYTES,
    LIMIT_MAX_MULTIPART_PARTS,
    LIMIT_MAX_OBJECT_BYTES,
    LIMIT_MAX_RANGE_BYTES,
    LIMIT_MAX_USER_METADATA_ENTRIES,
    OBJECT_OPERATION_VERSION,
)
from meridian_storage.spi import AdapterDescriptor, CapabilityManifest, OperationCapability

from .config import OciDistributionBinding

ADAPTER_ID = "oci-distribution"
ADAPTER_CONTRACT_VERSION = "1.0.0"
ENGINE_PROFILE = "oci-distribution"
OCI_DISTRIBUTION_VERSION = "1.1.1"


def oci_distribution_descriptor(binding: OciDistributionBinding) -> AdapterDescriptor:
    put_guarantees = [
        GUARANTEE_CONDITIONAL_CREATE,
        GUARANTEE_DIGEST_SHA256,
        GUARANTEE_IMMUTABILITY_INTENT,
        GUARANTEE_METADATA_AFTER_COMMIT,
        GUARANTEE_MULTIPART,
        GUARANTEE_RETENTION_INTENT,
        GUARANTEE_STREAMING,
    ]
    if binding.retention_enforced:
        put_guarantees.append(GUARANTEE_RETENTION_ENFORCEMENT)
    common_extensions = {
        "design.hldRevision": 56,
        "design.catalogRevision": 70,
        "design.objectLldRevision": 12,
        "oci.distributionSpec": OCI_DISTRIBUTION_VERSION,
        "oci.imageSpec": "1.1.1",
    }
    capabilities = (
        OperationCapability(
            "meridian.object.put",
            (OBJECT_OPERATION_VERSION,),
            guarantees=tuple(put_guarantees),
            limits={
                LIMIT_MAX_OBJECT_BYTES: binding.max_object_bytes,
                LIMIT_MAX_USER_METADATA_ENTRIES: 128,
                LIMIT_MAX_MULTIPART_PARTS: binding.max_multipart_parts,
                LIMIT_MAX_MULTIPART_PART_BYTES: binding.chunk_size,
            },
            migration_behavior="logical-export-import",
            health_probes=("authenticated", "push-pull", "range"),
            extensions={
                **common_extensions,
                "conditionalCreateMode": binding.conditional_create_mode,
                "retentionEnforcement": binding.retention_enforced,
            },
        ),
        OperationCapability(
            "meridian.object.get",
            (OBJECT_OPERATION_VERSION,),
            guarantees=(GUARANTEE_DIGEST_VERIFICATION, GUARANTEE_STREAMING),
            limits={LIMIT_MAX_OBJECT_BYTES: binding.max_object_bytes},
            migration_behavior="logical-export-import",
            health_probes=("authenticated", "pull"),
            extensions=common_extensions,
        ),
        OperationCapability(
            "meridian.object.stat",
            (OBJECT_OPERATION_VERSION,),
            guarantees=(GUARANTEE_DIGEST_VERIFICATION,),
            health_probes=("authenticated", "pull"),
            extensions=common_extensions,
        ),
        OperationCapability(
            "meridian.object.read_range",
            (OBJECT_OPERATION_VERSION,),
            guarantees=(GUARANTEE_DIGEST_VERIFICATION, GUARANTEE_RANGE_READ),
            limits={LIMIT_MAX_RANGE_BYTES: binding.max_range_bytes},
            health_probes=("authenticated", "range"),
            extensions=common_extensions,
        ),
        OperationCapability(
            "meridian.object.list",
            (OBJECT_OPERATION_VERSION,),
            guarantees=(GUARANTEE_BOUNDED_PREFIX_LIST,),
            limits={LIMIT_MAX_LIST_PAGE_SIZE: binding.max_list_page_size},
            cursor_behavior="opaque-stable",
            health_probes=("authenticated", "content-discovery"),
            extensions=common_extensions,
        ),
        OperationCapability(
            "meridian.object.delete",
            (OBJECT_OPERATION_VERSION,),
            guarantees=(GUARANTEE_EXACT_VERSION_DELETE, GUARANTEE_RETENTION_INTENT),
            migration_behavior="external-policy",
            health_probes=("authenticated", "content-management"),
            extensions=common_extensions,
        ),
    )
    return AdapterDescriptor(
        adapter_id=ADAPTER_ID,
        adapter_contract_version=ADAPTER_CONTRACT_VERSION,
        driver="httpx",
        supported_engine_versions={ENGINE_PROFILE: (OCI_DISTRIBUTION_VERSION,)},
        capabilities=capabilities,
    )


def configured_capability_manifest(binding: OciDistributionBinding) -> CapabilityManifest:
    operations = [
        "meridian.object.put",
        "meridian.object.get",
        "meridian.object.stat",
        "meridian.object.read_range",
        "meridian.object.list",
    ]
    if binding.deletion_enabled:
        operations.append("meridian.object.delete")
    selected: dict[str, str] = {}
    if binding.registry_release is not None:
        selected["release"] = binding.registry_release
    if binding.registry_image is not None:
        selected["image"] = binding.registry_image
    return CapabilityManifest(
        descriptor=oci_distribution_descriptor(binding),
        engine_profile=ENGINE_PROFILE,
        engine_version=OCI_DISTRIBUTION_VERSION,
        available_operation_contracts=tuple(operations),
        extensions={
            "verification": "configured",
            "bindingFingerprint": binding.public_fingerprint,
            "referrersRequired": binding.referrers_required,
            # Preserve legacy fingerprints when no new provenance is selected.
            **({"selectedRegistry": selected} if selected else {}),
        },
    )


__all__ = [
    "ADAPTER_CONTRACT_VERSION",
    "ADAPTER_ID",
    "ENGINE_PROFILE",
    "OCI_DISTRIBUTION_VERSION",
    "configured_capability_manifest",
    "oci_distribution_descriptor",
]
