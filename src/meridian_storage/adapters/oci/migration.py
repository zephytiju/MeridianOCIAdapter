# SPDX-License-Identifier: Apache-2.0
"""Provider-neutral logical export/import primitives; orchestration remains in IaC."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from meridian_storage.object_common import (
    ImmutabilityRequest,
    ObjectCatalogProvider,
    ObjectInvalidRequest,
    ObjectMetadata,
    ObjectReference,
    PayloadReference,
    PayloadRegistry,
    RetentionRequest,
    parse_object_reference,
)
from meridian_storage.semantics import JsonValue

from .adapter import OciDistributionAdapter
from .transport import RegistryBlobSource


@dataclass(frozen=True, slots=True)
class LogicalObjectExport:
    metadata: ObjectMetadata
    payload: PayloadReference
    immutability: ImmutabilityRequest
    retention: RetentionRequest | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "formatVersion": "meridian.object-logical-export.v1",
            "metadata": self.metadata.to_dict(),
            "payload": self.payload.to_dict(),
            "immutability": self.immutability.to_dict(),
            "retention": None if self.retention is None else self.retention.to_dict(),
        }


class OciLogicalMigration:
    def __init__(self, adapter: OciDistributionAdapter) -> None:
        self.adapter = adapter
        self._provider = ObjectCatalogProvider()
        self._surface = self._provider.create_surface()

    def export_logical(
        self,
        reference: ObjectReference | Mapping[str, object],
        payloads: PayloadRegistry,
    ) -> LogicalObjectExport:
        try:
            parsed = parse_object_reference(reference)
        except (TypeError, ValueError) as exc:
            raise ObjectInvalidRequest(f"invalid migration Object reference: {exc}") from exc
        if parsed.resource_ref != self.adapter._semantic_resource():
            raise ObjectInvalidRequest("migration Object reference does not match the binding")
        resolved = self.adapter._stored_for_reference(parsed)
        payload = payloads.register(
            RegistryBlobSource(lambda: self.adapter.transport.open_blob(resolved.payload.digest)),
            expected_length=resolved.payload.size,
            expected_digest=resolved.payload.digest,
        )
        return LogicalObjectExport(
            resolved.stored.metadata,
            payload,
            resolved.stored.immutability,
            resolved.stored.retention,
        )

    def import_logical(
        self,
        exported: LogicalObjectExport,
        payloads: PayloadRegistry,
        *,
        create_only: bool = True,
    ) -> Mapping[str, object]:
        metadata = exported.metadata
        provenance = dict(metadata.provenance)
        provenance.pop("meridianAdapter", None)
        expression = self._surface.put(
            resource=self.adapter.binding.resource_ref,
            object_id=metadata.object_ref.object_id,
            payload=exported.payload,
            media_type=metadata.media_type,
            expected_digest=metadata.digest,
            expected_length=metadata.byte_length,
            user_metadata=metadata.user_metadata,
            creation_context=metadata.creation_context,
            provenance=provenance,
            immutability=exported.immutability,
            retention=exported.retention,
            create_only=create_only,
        )
        return self.adapter.execute(self._provider.normalize(expression), payloads)


__all__ = ["LogicalObjectExport", "OciLogicalMigration"]
