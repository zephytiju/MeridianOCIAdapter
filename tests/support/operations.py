# SPDX-License-Identifier: Apache-2.0
"""Compact helpers for consumer-shaped Object operations."""

from __future__ import annotations

import hashlib
from io import BytesIO

from meridian_storage import Operation
from meridian_storage.object_common import (
    FactoryPayloadSource,
    ObjectCatalogProvider,
    PayloadRegistry,
)


def digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def put_operation(
    provider: ObjectCatalogProvider,
    payloads: PayloadRegistry,
    *,
    data: bytes = b"payload",
    resource: str = "object:conformance.objects",
    object_id: str = "fixture/object",
    expected_digest: str | None | object = ...,
    expected_length: int | None | object = ...,
    create_only: bool = False,
    user_metadata: dict[str, str] | None = None,
    creation_context: dict[str, object] | None = None,
    provenance: dict[str, object] | None = None,
    immutability: dict[str, object] | None = None,
    retention: dict[str, object] | None = None,
    replayable: bool = True,
) -> Operation:
    selected_digest = digest(data) if expected_digest is ... else expected_digest
    selected_length = len(data) if expected_length is ... else expected_length
    reference = payloads.register(
        FactoryPayloadSource(lambda: BytesIO(data), replayable=replayable),
        expected_digest=selected_digest,  # type: ignore[arg-type]
        expected_length=selected_length,  # type: ignore[arg-type]
    )
    expression = provider.create_surface().put(
        resource=resource,
        object_id=object_id,
        payload=reference,
        media_type="application/octet-stream",
        expected_digest=selected_digest,  # type: ignore[arg-type]
        expected_length=selected_length,  # type: ignore[arg-type]
        create_only=create_only,
        user_metadata=user_metadata,
        creation_context=creation_context,
        provenance=provenance,
        immutability=immutability,
        retention=retention,
    )
    return provider.normalize(expression)


__all__ = ["digest", "put_operation"]
