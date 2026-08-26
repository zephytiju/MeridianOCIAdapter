# SPDX-License-Identifier: Apache-2.0
"""Meridian Object Operation execution over an OCI Distribution repository."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from typing import Any, cast

from meridian_storage import Operation
from meridian_storage.object_common import (
    ByteRange,
    ConditionalConflict,
    ContentIdentity,
    DigestMismatch,
    ImmutabilityRequest,
    ImmutableObjectConflict,
    IncompleteUpload,
    MultipartPart,
    MultipartSession,
    ObjectCapabilityMismatch,
    ObjectInvalidRequest,
    ObjectMetadata,
    ObjectNotFound,
    ObjectRateLimited,
    ObjectReference,
    ObjectUnavailable,
    PayloadReference,
    PayloadRegistry,
    PutState,
    PutStateMachine,
    RetentionRequest,
    iter_payload_chunks,
    parse_object_reference,
    require_object_capabilities,
)
from meridian_storage.semantics import CatalogName, ResourceReference
from meridian_storage.spi import CapabilityManifest

from ._naming import (
    CursorCodec,
    anchor_tag,
    latest_tag,
    object_id_hash,
    version_tag,
    version_tag_prefix,
)
from .descriptor import ADAPTER_ID, OciDistributionBinding, configured_capability_manifest
from .retention import require_delete_permitted
from .transport import (
    EMPTY_JSON,
    MERIDIAN_OBJECT_ANCHOR_TYPE,
    MERIDIAN_OBJECT_CONFIG_MEDIA_TYPE,
    OCI_MANIFEST_MEDIA_TYPE,
    ManifestDocument,
    OciDescriptor,
    RegistryBlobSource,
    RegistryHttpClient,
    StoredObject,
    UploadLocation,
    build_anchor_manifest,
    build_object_manifest,
    manifest_descriptors,
)


@dataclass(frozen=True, slots=True)
class _ResolvedObject:
    stored: StoredObject
    manifest: ManifestDocument
    payload: OciDescriptor


class OciDistributionAdapter:
    """Data-plane Adapter; Object Catalog construction and Engine selection stay in Core/IaC."""

    def __init__(
        self,
        binding: OciDistributionBinding,
        *,
        transport: RegistryHttpClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.binding = binding
        self.transport = transport or RegistryHttpClient(binding)
        self._owns_transport = transport is None
        self._clock = clock or (lambda: datetime.now(UTC))
        self._manifest = configured_capability_manifest(binding)
        self._cursor = CursorCodec(cast(bytes, binding.cursor_signing_key))
        self._write_lock = RLock()
        from .multipart import OciMultipartManager

        self._multipart = OciMultipartManager(self)

    @property
    def adapter_id(self) -> str:
        return ADAPTER_ID

    @property
    def target_id(self) -> str:
        return f"{ADAPTER_ID}:{self.binding.public_fingerprint}"

    @property
    def capability_manifest(self) -> CapabilityManifest:
        return self._manifest

    def close(self) -> None:
        if self._owns_transport:
            self.transport.close()

    def __enter__(self) -> OciDistributionAdapter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def execute(
        self,
        operation: Operation,
        payloads: PayloadRegistry,
    ) -> Mapping[str, object]:
        method = self._validate_operation(operation)
        require_object_capabilities(self._manifest, operation.requirements)
        handlers = {
            "put": self._put,
            "get": self._get,
            "stat": self._stat,
            "read_range": self._read_range,
            "list": self._list,
            "delete": self._delete,
        }
        handler = handlers.get(method)
        if handler is None:
            raise ObjectInvalidRequest(
                "OCI Distribution Adapter accepts only Object data-plane Operations",
                operation_contract=operation.operation_contract,
            )
        return handler(operation, payloads)

    def begin_multipart(self, operation: Operation) -> MultipartSession:
        return self._multipart.begin_multipart(operation)

    def upload_part(
        self,
        session: MultipartSession,
        part_number: int,
        payload: PayloadReference,
        payloads: PayloadRegistry,
    ) -> MultipartPart:
        return self._multipart.upload_part(session, part_number, payload, payloads)

    def complete_multipart(
        self,
        session: MultipartSession,
        parts: Sequence[MultipartPart],
    ) -> ObjectMetadata:
        return self._multipart.complete_multipart(session, parts)

    def abort_multipart(self, session: MultipartSession) -> None:
        self._multipart.abort_multipart(session)

    def _validate_operation(self, operation: Operation) -> str:
        if operation.catalog != "object" or not operation.operation_contract.startswith(
            "meridian.object."
        ):
            raise ObjectInvalidRequest("Operation is not an Object Catalog Operation")
        if operation.operation_version != "1.0.0":
            raise ObjectCapabilityMismatch(
                "OCI Distribution Adapter supports Object Operation version 1.0.0 only"
            )
        if operation.resources != (self.binding.resource_ref,):
            raise ObjectInvalidRequest("Operation Resource does not match the private binding")
        if operation.operation_contract not in self._manifest.available_operation_contracts:
            raise ObjectCapabilityMismatch(
                "Object Operation is unavailable in the configured OCI binding",
                operation_contract=operation.operation_contract,
            )
        return cast(str, operation.operation_contract.rsplit(".", 1)[-1])

    def _put(self, operation: Operation, payloads: PayloadRegistry) -> Mapping[str, object]:
        value = operation.input
        object_id = cast(str, value["objectId"])
        payload_value = value["payload"]
        if not isinstance(payload_value, Mapping):
            raise ObjectInvalidRequest("put payload must be a serialized PayloadReference")
        payload = PayloadReference.from_mapping(payload_value)
        create_only = cast(bool, value["createOnly"])
        immutability_value = value["immutability"]
        retention_value = value["retention"]
        immutability = (
            ImmutabilityRequest("immutable", False)
            if immutability_value is None
            else ImmutabilityRequest.from_mapping(cast(Mapping[str, object], immutability_value))
        )
        retention = (
            None
            if retention_value is None
            else RetentionRequest.from_mapping(cast(Mapping[str, object], retention_value))
        )
        if immutability.publish_once:
            create_only = True
        state = PutStateMachine()
        physical_commit = False
        with self._write_lock:
            self._require_create_available(object_id, immutability, create_only=create_only)
            try:
                state.transition(PutState.UPLOADING)
                identity = self._upload_payload(payload, payloads)
                physical_commit = True
                state.transition(PutState.VERIFYING)
                result = self._commit_object(
                    value,
                    identity,
                    immutability=immutability,
                    retention=retention,
                    create_only=create_only,
                )
                state.transition(PutState.COMMITTED)
                return {"metadata": result.to_dict()}
            except Exception:
                if state.state in {PutState.UPLOADING, PutState.VERIFYING}:
                    state.fail(physical_commit_possible=physical_commit)
                raise

    def _upload_payload(
        self,
        payload: PayloadReference,
        payloads: PayloadRegistry,
    ) -> ContentIdentity:
        attempts = 2 if payload.replayable else 1
        for attempt in range(attempts):
            upload: UploadLocation | None = None
            hasher = hashlib.sha256()
            length = 0
            existing = (
                None
                if payload.expected_digest is None
                else self.transport.head_blob(payload.expected_digest)
            )
            try:
                if existing is None:
                    upload = self.transport.start_upload()
                with payloads.open(payload) as stream:
                    for chunk in iter_payload_chunks(stream, chunk_size=self.binding.chunk_size):
                        length += len(chunk)
                        if length > self.binding.max_object_bytes:
                            raise ObjectInvalidRequest("Object exceeds the configured size limit")
                        if payload.expected_length is not None and length > payload.expected_length:
                            raise IncompleteUpload("payload exceeded its declared length")
                        hasher.update(chunk)
                        if upload is not None:
                            upload = self.transport.patch_upload(upload, chunk)
                identity = ContentIdentity(f"sha256:{hasher.hexdigest()}", length)
                self._verify_identity(payload, identity)
                if existing is not None:
                    if existing.size != identity.byte_length:
                        raise DigestMismatch("existing registry blob length does not match payload")
                    return identity
                assert upload is not None
                self.transport.finish_upload(upload, identity.digest)
                return identity
            except (ObjectUnavailable, ObjectRateLimited):
                if upload is not None:
                    self._abort_safely(upload)
                if attempt + 1 == attempts:
                    raise
            except Exception:
                if upload is not None:
                    self._abort_safely(upload)
                raise
        raise AssertionError("unreachable upload retry state")

    @staticmethod
    def _verify_identity(payload: PayloadReference, identity: ContentIdentity) -> None:
        if payload.expected_length is not None and identity.byte_length != payload.expected_length:
            raise IncompleteUpload()
        if payload.expected_digest is not None and identity.digest != payload.expected_digest:
            raise DigestMismatch()

    def _commit_object(
        self,
        value: Mapping[str, Any],
        identity: ContentIdentity,
        *,
        immutability: ImmutabilityRequest,
        retention: RetentionRequest | None,
        create_only: bool,
    ) -> ObjectMetadata:
        object_id = cast(str, value["objectId"])
        self._require_create_available(object_id, immutability, create_only=create_only)
        exact_tag = version_tag(object_id, identity.digest)
        if self.transport.manifest_exists(exact_tag):
            if create_only:
                error = (
                    ImmutableObjectConflict if immutability.publish_once else ConditionalConflict
                )
                raise error("exact Object version was already published")
            existing = self._stored_for_reference(
                ObjectReference(self._semantic_resource(), object_id, identity.digest)
            )
            self.transport.put_manifest(latest_tag(object_id), existing.manifest.body)
            return existing.stored.metadata
        anchor = self._ensure_anchor(object_id)
        provenance_value = value["provenance"]
        if not isinstance(provenance_value, Mapping):
            raise ObjectInvalidRequest("Object provenance must be a mapping")
        provenance = dict(provenance_value)
        if "meridianAdapter" in provenance:
            raise ObjectInvalidRequest("meridianAdapter is a reserved provenance field")
        provenance["meridianAdapter"] = {
            "adapterId": ADAPTER_ID,
            "capabilityFingerprint": self._manifest.fingerprint,
            "engineProfile": "oci-distribution",
            "engineVersion": "1.1.1",
        }
        metadata = ObjectMetadata(
            object_ref=ObjectReference(self._semantic_resource(), object_id, identity.digest),
            digest=identity.digest,
            byte_length=identity.byte_length,
            media_type=cast(str, value["mediaType"]),
            created_at=self._now(),
            creation_context=cast(Mapping[str, Any], value["creationContext"]),
            user_metadata=cast(Mapping[str, str], value["userMetadata"]),
            mutability=immutability.mutability,
            provenance=provenance,
        )
        stored = StoredObject(metadata, immutability, retention)
        config_bytes = stored.to_bytes()
        if len(config_bytes) > self.binding.max_metadata_bytes:
            raise ObjectInvalidRequest("serialized Object metadata exceeds the configured limit")
        config = self.transport.upload_blob_bytes(
            config_bytes,
            media_type=MERIDIAN_OBJECT_CONFIG_MEDIA_TYPE,
        )
        payload = OciDescriptor(metadata.media_type, identity.digest, identity.byte_length)
        manifest = build_object_manifest(
            anchor=anchor,
            config=config,
            payload=payload,
            object_hash=object_id_hash(object_id),
            created_at=cast(str, metadata.created_at),
            mutability=metadata.mutability,
        )
        self.transport.put_manifest(exact_tag, manifest)
        self.transport.put_manifest(latest_tag(object_id), manifest)
        return metadata

    def _ensure_anchor(self, object_id: str) -> OciDescriptor:
        tag = anchor_tag(object_id)
        expected_hash = object_id_hash(object_id)
        if self.transport.manifest_exists(tag):
            document = self.transport.get_manifest(tag)
            annotations = document.value.get("annotations")
            if (
                document.value.get("artifactType") != MERIDIAN_OBJECT_ANCHOR_TYPE
                or not isinstance(annotations, Mapping)
                or annotations.get("org.meridian.object.anchor") != expected_hash
            ):
                raise ConditionalConflict("logical Object anchor is incompatible")
            return OciDescriptor(OCI_MANIFEST_MEDIA_TYPE, document.digest, len(document.body))
        self.transport.upload_blob_bytes(EMPTY_JSON, media_type="application/vnd.oci.empty.v1+json")
        manifest = build_anchor_manifest(object_hash=expected_hash)
        return self.transport.put_manifest(tag, manifest)

    def _get(self, operation: Operation, payloads: PayloadRegistry) -> Mapping[str, object]:
        reference = self._reference(operation.input, require_digest=False)
        resolved = self._stored_for_reference(reference)
        payload = payloads.register(
            RegistryBlobSource(lambda: self.transport.open_blob(resolved.payload.digest)),
            expected_length=resolved.payload.size,
            expected_digest=resolved.payload.digest,
        )
        return {"metadata": resolved.stored.metadata.to_dict(), "payload": payload.to_dict()}

    def _stat(self, operation: Operation, _: PayloadRegistry) -> Mapping[str, object]:
        reference = self._reference(operation.input, require_digest=False)
        resolved = self._stored_for_reference(reference)
        return {"metadata": resolved.stored.metadata.to_dict()}

    def _read_range(
        self,
        operation: Operation,
        payloads: PayloadRegistry,
    ) -> Mapping[str, object]:
        reference = self._reference(operation.input, require_digest=False)
        raw_range = operation.input["range"]
        if not isinstance(raw_range, Mapping):
            raise ObjectInvalidRequest("read_range requires a serialized ByteRange")
        byte_range = ByteRange.from_mapping(raw_range)
        resolved_object = self._stored_for_reference(reference)
        selected = byte_range.resolve(resolved_object.payload.size)
        if selected.length > self.binding.max_range_bytes:
            raise ObjectCapabilityMismatch("requested range exceeds the OCI binding limit")
        payload = payloads.register(
            RegistryBlobSource(
                lambda: self.transport.open_blob(
                    resolved_object.payload.digest,
                    start=selected.start,
                    end=selected.end,
                )
            ),
            expected_length=selected.length,
        )
        return {
            "metadata": resolved_object.stored.metadata.to_dict(),
            "payload": payload.to_dict(),
            "range": selected.to_dict(),
        }

    def _list(self, operation: Operation, _: PayloadRegistry) -> Mapping[str, object]:
        prefix = cast(str, operation.input["prefix"])
        limit = cast(int, operation.input["limit"])
        cursor_value = operation.input["cursor"]
        last = None if cursor_value is None else self._cursor.decode(cast(str, cursor_value))
        page = self.transport.list_tags(limit=self.binding.max_scan_tags, last=last)
        items: list[dict[str, object]] = []
        scanned: str | None = None
        stopped_early = False
        for tag in page.tags:
            scanned = tag
            if not tag.startswith(version_tag_prefix()):
                continue
            document = self.transport.get_manifest(tag)
            stored = self._stored_from_manifest(document)
            if stored.stored.metadata.object_ref.object_id.startswith(prefix):
                items.append(cast(dict[str, object], stored.stored.metadata.to_dict()))
            if len(items) == limit:
                stopped_early = True
                break
        next_cursor = None
        if scanned is not None and (stopped_early or page.has_more):
            next_cursor = self._cursor.encode(scanned)
        return {"items": items, "cursor": next_cursor}

    def _delete(self, operation: Operation, _: PayloadRegistry) -> Mapping[str, object]:
        reference = self._reference(operation.input, require_digest=True)
        with self._write_lock:
            resolved = self._stored_for_reference(reference)
            require_delete_permitted(resolved.stored.retention, now=self._now())
            if not self.transport.delete_manifest(resolved.manifest.digest):
                raise ObjectNotFound()
        return {"deleted": True}

    def _stored_for_reference(self, reference: ObjectReference) -> _ResolvedObject:
        tag = (
            latest_tag(reference.object_id)
            if reference.digest is None
            else version_tag(reference.object_id, reference.digest)
        )
        resolved = self._stored_from_manifest(self.transport.get_manifest(tag))
        metadata = resolved.stored.metadata
        if metadata.object_ref.resource_ref != self._semantic_resource():
            raise ObjectNotFound()
        if metadata.object_ref.object_id != reference.object_id:
            raise ObjectNotFound()
        if reference.digest is not None and metadata.digest != reference.digest:
            raise ObjectNotFound()
        anchor = self.transport.get_manifest(anchor_tag(reference.object_id))
        _, _, subject = manifest_descriptors(resolved.manifest)
        if subject.digest != anchor.digest:
            raise DigestMismatch("Object manifest subject does not match its logical anchor")
        return resolved

    def _stored_from_manifest(self, document: ManifestDocument) -> _ResolvedObject:
        try:
            config, payload, _ = manifest_descriptors(document)
        except (TypeError, ValueError) as exc:
            raise DigestMismatch("registry returned an invalid Meridian Object manifest") from exc
        config_bytes = self.transport.get_blob_bytes(
            config,
            maximum_bytes=self.binding.max_metadata_bytes,
        )
        try:
            stored = StoredObject.from_bytes(
                config_bytes,
                maximum_bytes=self.binding.max_metadata_bytes,
            )
        except (TypeError, ValueError) as exc:
            raise DigestMismatch("registry returned invalid Meridian Object metadata") from exc
        metadata = stored.metadata
        if (
            metadata.digest != payload.digest
            or metadata.byte_length != payload.size
            or metadata.media_type != payload.media_type
        ):
            raise DigestMismatch("OCI manifest payload descriptor does not match Object metadata")
        return _ResolvedObject(stored, document, payload)

    def _reference(
        self,
        value: Mapping[str, Any],
        *,
        require_digest: bool,
    ) -> ObjectReference:
        raw = value["reference"]
        if not isinstance(raw, Mapping):
            raise ObjectInvalidRequest("Object reference must be a mapping")
        try:
            reference = parse_object_reference(raw, require_digest=require_digest)
        except (TypeError, ValueError) as exc:
            raise ObjectInvalidRequest(f"invalid Object reference: {exc}") from exc
        if reference.resource_ref != self._semantic_resource():
            raise ObjectInvalidRequest("Object reference does not match the private binding")
        return reference

    def _semantic_resource(self) -> ResourceReference:
        resource = self.binding.resource_ref
        return ResourceReference(CatalogName.OBJECT, resource.namespace, resource.name)

    def _object_reference_for_operation(self, operation: Operation) -> ObjectReference:
        return ObjectReference(
            self._semantic_resource(),
            cast(str, operation.input["objectId"]),
            cast(str | None, operation.input["expectedDigest"]),
        )

    def _require_create_available(
        self,
        object_id: str,
        immutability: ImmutabilityRequest,
        *,
        create_only: bool,
    ) -> None:
        if not create_only:
            return
        reference = anchor_tag(object_id) if immutability.publish_once else latest_tag(object_id)
        if self.transport.manifest_exists(reference):
            error = ImmutableObjectConflict if immutability.publish_once else ConditionalConflict
            raise error("logical Object id was already published")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("adapter clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def _abort_safely(self, upload: UploadLocation) -> None:
        try:
            self.transport.abort_upload(upload)
        except Exception:
            return


__all__ = ["OciDistributionAdapter"]
