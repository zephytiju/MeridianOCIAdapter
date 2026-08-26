# SPDX-License-Identifier: Apache-2.0
"""Adapter-internal resumable OCI chunked upload implementation."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from threading import RLock
from typing import TYPE_CHECKING, Protocol, cast

from meridian_storage import Operation
from meridian_storage.object_common import (
    ContentIdentity,
    ImmutabilityRequest,
    IncompleteUpload,
    MultipartCompletion,
    MultipartInvalid,
    MultipartLimits,
    MultipartPart,
    MultipartSession,
    ObjectMetadata,
    PayloadReference,
    PayloadRegistry,
    PutState,
    PutStateMachine,
    RetentionRequest,
    iter_payload_chunks,
    require_object_capabilities,
)

from ..transport import UploadLocation

if TYPE_CHECKING:
    from ..adapter import OciDistributionAdapter


class _Hash(Protocol):
    def update(self, value: bytes) -> None: ...

    def hexdigest(self) -> str: ...


@dataclass(slots=True)
class _MultipartState:
    operation: Operation
    upload: UploadLocation
    machine: PutStateMachine
    hasher: _Hash
    length: int = 0
    next_part: int = 1
    parts: list[MultipartPart] = field(default_factory=list)


class OciMultipartManager:
    def __init__(self, adapter: OciDistributionAdapter) -> None:
        self.adapter = adapter
        self.limits = MultipartLimits(
            min_part_bytes=64 * 1024,
            max_part_bytes=adapter.binding.chunk_size,
            max_parts=adapter.binding.max_multipart_parts,
        )
        self._sessions: dict[str, _MultipartState] = {}
        self._lock = RLock()

    def begin_multipart(self, operation: Operation) -> MultipartSession:
        if self.adapter._validate_operation(operation) != "put":
            raise MultipartInvalid("multipart is available only for Object put")
        require_object_capabilities(self.adapter.capability_manifest, operation.requirements)
        object_id = cast(str, operation.input["objectId"])
        immutability = _immutability(operation)
        create_only = cast(bool, operation.input["createOnly"]) or immutability.publish_once
        with self.adapter._write_lock:
            self.adapter._require_create_available(
                object_id,
                immutability,
                create_only=create_only,
            )
            upload = self.adapter.transport.start_upload()
        machine = PutStateMachine()
        machine.transition(PutState.UPLOADING)
        session_id = f"u{secrets.token_hex(30)}"
        expires = self.adapter._now() + timedelta(hours=1)
        session = MultipartSession(
            session_id=session_id,
            object_ref=self.adapter._object_reference_for_operation(operation),
            part_size=self.adapter.binding.chunk_size,
            expires_at=expires.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        )
        with self._lock:
            self._sessions[session_id] = _MultipartState(
                operation=operation,
                upload=upload,
                machine=machine,
                hasher=hashlib.sha256(),
            )
        return session

    def upload_part(
        self,
        session: MultipartSession,
        part_number: int,
        payload: PayloadReference,
        payloads: PayloadRegistry,
    ) -> MultipartPart:
        state = self._state(session)
        if part_number != state.next_part:
            raise MultipartInvalid("OCI multipart parts must be uploaded in order")
        if part_number > self.adapter.binding.max_multipart_parts:
            raise MultipartInvalid("OCI multipart part-count limit was exceeded")
        part_hasher = hashlib.sha256()
        part_length = 0
        try:
            with payloads.open(payload) as stream:
                for chunk in iter_payload_chunks(stream, chunk_size=session.part_size):
                    part_length += len(chunk)
                    if part_length > session.part_size:
                        raise MultipartInvalid("multipart part exceeds the session part size")
                    if state.length + part_length > self.adapter.binding.max_object_bytes:
                        raise MultipartInvalid("multipart Object exceeds the configured size limit")
                    part_hasher.update(chunk)
                    state.hasher.update(chunk)
                    state.upload = self.adapter.transport.patch_upload(state.upload, chunk)
            if part_length == 0:
                raise MultipartInvalid("multipart parts cannot be empty")
            identity = ContentIdentity(f"sha256:{part_hasher.hexdigest()}", part_length)
            self.adapter._verify_identity(payload, identity)
        except Exception:
            self.abort_multipart(session)
            raise
        state.length += part_length
        verification = _verification_token(
            cast(bytes, self.adapter.binding.cursor_signing_key),
            session.session_id,
            part_number,
            identity,
        )
        part = MultipartPart(part_number, identity, verification)
        state.parts.append(part)
        state.next_part += 1
        return part

    def complete_multipart(
        self,
        session: MultipartSession,
        parts: Sequence[MultipartPart],
    ) -> ObjectMetadata:
        state = self._state(session)
        supplied = tuple(parts)
        if supplied != tuple(state.parts):
            raise MultipartInvalid("multipart completion parts do not match uploaded parts")
        if any(part.identity.byte_length < self.limits.min_part_bytes for part in supplied[:-1]):
            raise MultipartInvalid("every non-final multipart part must meet the minimum size")
        identity = ContentIdentity(f"sha256:{state.hasher.hexdigest()}", state.length)
        MultipartCompletion(supplied, identity)
        expected_digest = cast(str | None, state.operation.input["expectedDigest"])
        expected_length = cast(int | None, state.operation.input["expectedLength"])
        if expected_digest is not None and expected_digest != identity.digest:
            self.abort_multipart(session)
            raise IncompleteUpload("multipart digest does not match the put declaration")
        if expected_length is not None and expected_length != identity.byte_length:
            self.abort_multipart(session)
            raise IncompleteUpload("multipart length does not match the put declaration")
        immutability = _immutability(state.operation)
        retention = _retention(state.operation)
        create_only = cast(bool, state.operation.input["createOnly"]) or immutability.publish_once
        object_id = cast(str, state.operation.input["objectId"])
        try:
            with self.adapter._write_lock:
                self.adapter._require_create_available(
                    object_id,
                    immutability,
                    create_only=create_only,
                )
                self.adapter.transport.finish_upload(state.upload, identity.digest)
                state.machine.transition(PutState.VERIFYING)
                metadata = self.adapter._commit_object(
                    state.operation.input,
                    identity,
                    immutability=immutability,
                    retention=retention,
                    create_only=create_only,
                )
                state.machine.transition(PutState.COMMITTED)
                return metadata
        except Exception:
            if state.machine.state == PutState.UPLOADING:
                self.adapter._abort_safely(state.upload)
                state.machine.fail(physical_commit_possible=False)
            elif state.machine.state == PutState.VERIFYING:
                state.machine.fail(physical_commit_possible=True)
            raise
        finally:
            with self._lock:
                self._sessions.pop(session.session_id, None)

    def abort_multipart(self, session: MultipartSession) -> None:
        with self._lock:
            state = self._sessions.pop(session.session_id, None)
        if state is None:
            return
        self.adapter._abort_safely(state.upload)
        if state.machine.state == PutState.UPLOADING:
            state.machine.transition(PutState.ABORTED)

    def _state(self, session: MultipartSession) -> _MultipartState:
        with self._lock:
            state = self._sessions.get(session.session_id)
        if state is None or session.object_ref != self.adapter._object_reference_for_operation(
            state.operation
        ):
            raise MultipartInvalid("multipart session is unknown or does not match the Object")
        return state


def _immutability(operation: Operation) -> ImmutabilityRequest:
    value = operation.input["immutability"]
    return (
        ImmutabilityRequest("immutable", False)
        if value is None
        else ImmutabilityRequest.from_mapping(cast(dict[str, object], value))
    )


def _retention(operation: Operation) -> RetentionRequest | None:
    value = operation.input["retention"]
    return None if value is None else RetentionRequest.from_mapping(cast(dict[str, object], value))


def _verification_token(
    key: bytes,
    session_id: str,
    part_number: int,
    identity: ContentIdentity,
) -> str:
    value = f"{session_id}|{part_number}|{identity.digest}|{identity.byte_length}".encode()
    return f"p_{hmac.new(key, value, hashlib.sha256).hexdigest()}"


__all__ = ["OciMultipartManager"]
