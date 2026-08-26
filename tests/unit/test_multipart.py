# SPDX-License-Identifier: Apache-2.0
"""OCI resumable multipart state-machine behavior."""

from __future__ import annotations

from dataclasses import replace
from io import BytesIO

import pytest

from meridian_storage.adapters.oci import OciDistributionAdapter
from meridian_storage.object_common import (
    ConditionalConflict,
    DigestMismatch,
    FactoryPayloadSource,
    ImmutableObjectConflict,
    IncompleteUpload,
    MultipartInvalid,
    ObjectReference,
    ObjectUnavailable,
    PayloadRegistry,
)

from ..support.operations import digest, put_operation
from ..support.registry import FakeRegistry


def _part(payloads: PayloadRegistry, value: bytes, *, expected: str | None | object = ...):
    selected = digest(value) if expected is ... else expected
    return payloads.register(
        FactoryPayloadSource(lambda: BytesIO(value), replayable=True),
        expected_digest=selected,  # type: ignore[arg-type]
        expected_length=len(value),
    )


def test_multipart_upload_completes_into_normal_object(
    adapter: OciDistributionAdapter,
    provider: object,
    payloads: PayloadRegistry,
) -> None:
    first = b"a" * (64 * 1024)
    second = b"tail"
    operation = put_operation(
        provider,  # type: ignore[arg-type]
        payloads,
        data=first + second,
        object_id="multipart/success",
    )
    session = adapter.begin_multipart(operation)

    one = adapter.upload_part(session, 1, _part(payloads, first), payloads)
    two = adapter.upload_part(session, 2, _part(payloads, second), payloads)
    metadata = adapter.complete_multipart(session, [one, two])

    assert metadata.digest == digest(first + second)
    assert metadata.byte_length == len(first + second)
    assert session.part_size == adapter.binding.chunk_size
    assert session.expires_at is not None
    assert one.verification_token.startswith("p_")
    adapter.abort_multipart(session)
    with pytest.raises(MultipartInvalid, match="unknown"):
        adapter.upload_part(session, 3, _part(payloads, b"x"), payloads)


def test_multipart_rejects_out_of_order_empty_and_oversized_parts(
    adapter: OciDistributionAdapter,
    provider: object,
    payloads: PayloadRegistry,
    registry: FakeRegistry,
) -> None:
    operation = put_operation(
        provider,
        payloads,
        data=b"x",
        object_id="multipart/order",  # type: ignore[arg-type]
    )
    session = adapter.begin_multipart(operation)
    with pytest.raises(MultipartInvalid, match="in order"):
        adapter.upload_part(session, 2, _part(payloads, b"x"), payloads)
    adapter.abort_multipart(session)

    empty_session = adapter.begin_multipart(
        put_operation(provider, payloads, data=b"", object_id="multipart/empty")  # type: ignore[arg-type]
    )
    with pytest.raises(MultipartInvalid, match="empty"):
        adapter.upload_part(empty_session, 1, _part(payloads, b""), payloads)

    too_large = b"x" * (adapter.binding.chunk_size + 1)
    large_session = adapter.begin_multipart(
        put_operation(
            provider,  # type: ignore[arg-type]
            payloads,
            data=too_large,
            object_id="multipart/large",
        )
    )
    with pytest.raises(MultipartInvalid, match="part size"):
        adapter.upload_part(large_session, 1, _part(payloads, too_large), payloads)
    assert registry.uploads == {}


def test_multipart_digest_mismatch_aborts_upload(
    adapter: OciDistributionAdapter,
    provider: object,
    payloads: PayloadRegistry,
    registry: FakeRegistry,
) -> None:
    operation = put_operation(
        provider,
        payloads,
        data=b"actual",
        object_id="multipart/digest",  # type: ignore[arg-type]
    )
    session = adapter.begin_multipart(operation)
    with pytest.raises(DigestMismatch):
        adapter.upload_part(
            session,
            1,
            _part(payloads, b"actual", expected="sha256:" + "0" * 64),
            payloads,
        )
    assert registry.uploads == {}
    with pytest.raises(MultipartInvalid, match="unknown"):
        adapter.complete_multipart(session, [])


def test_multipart_completion_validates_parts_declarations_and_minimum(
    adapter: OciDistributionAdapter,
    provider: object,
    payloads: PayloadRegistry,
) -> None:
    operation = put_operation(
        provider,  # type: ignore[arg-type]
        payloads,
        data=b"ab",
        object_id="multipart/parts",
    )
    session = adapter.begin_multipart(operation)
    one = adapter.upload_part(session, 1, _part(payloads, b"a"), payloads)
    two = adapter.upload_part(session, 2, _part(payloads, b"b"), payloads)
    with pytest.raises(MultipartInvalid, match="do not match"):
        adapter.complete_multipart(session, [one])
    with pytest.raises(MultipartInvalid, match="minimum"):
        adapter.complete_multipart(session, [one, two])
    adapter.abort_multipart(session)

    mismatch = adapter.begin_multipart(
        put_operation(
            provider,  # type: ignore[arg-type]
            payloads,
            data=b"actual",
            object_id="multipart/declared",
            expected_digest="sha256:" + "0" * 64,
        )
    )
    part = adapter.upload_part(mismatch, 1, _part(payloads, b"actual"), payloads)
    with pytest.raises(IncompleteUpload, match="digest"):
        adapter.complete_multipart(mismatch, [part])


def test_multipart_part_count_object_limit_and_conditional_create(
    adapter: OciDistributionAdapter,
    provider: object,
    payloads: PayloadRegistry,
) -> None:
    adapter.execute(
        put_operation(provider, payloads, object_id="multipart/existing", create_only=True),  # type: ignore[arg-type]
        payloads,
    )
    with pytest.raises(ConditionalConflict):
        adapter.begin_multipart(
            put_operation(
                provider,  # type: ignore[arg-type]
                payloads,
                object_id="multipart/existing",
                create_only=True,
            )
        )

    limited = OciDistributionAdapter(
        replace(adapter.binding, max_multipart_parts=1),
        transport=adapter.transport,
        clock=adapter._clock,
    )
    session = limited.begin_multipart(
        put_operation(provider, payloads, data=b"ab", object_id="multipart/count")  # type: ignore[arg-type]
    )
    limited.upload_part(session, 1, _part(payloads, b"a"), payloads)
    with pytest.raises(MultipartInvalid, match="part-count"):
        limited.upload_part(session, 2, _part(payloads, b"b"), payloads)
    limited.abort_multipart(session)

    object_limited = OciDistributionAdapter(
        replace(adapter.binding, max_object_bytes=1),
        transport=adapter.transport,
        clock=adapter._clock,
    )
    session = object_limited.begin_multipart(
        put_operation(  # type: ignore[arg-type]
            provider,
            payloads,
            data=b"ab",
            object_id="multipart/object-limit",
            expected_length=None,
        )
    )
    with pytest.raises(MultipartInvalid, match="Object"):
        object_limited.upload_part(session, 1, _part(payloads, b"ab"), payloads)


def test_begin_multipart_requires_put_operation(
    adapter: OciDistributionAdapter,
    provider: object,
    payloads: PayloadRegistry,
) -> None:
    surface = provider.create_surface()  # type: ignore[attr-defined]
    operation = provider.normalize(  # type: ignore[attr-defined]
        surface.list(resource="object:conformance.objects")
    )
    with pytest.raises(MultipartInvalid, match="only for Object put"):
        adapter.begin_multipart(operation)


def test_multipart_completion_length_mismatch_and_session_identity(
    adapter: OciDistributionAdapter,
    provider: object,
    payloads: PayloadRegistry,
) -> None:
    operation = put_operation(
        provider,  # type: ignore[arg-type]
        payloads,
        data=b"ab",
        object_id="multipart/length",
        expected_length=3,
    )
    session = adapter.begin_multipart(operation)
    part = adapter.upload_part(session, 1, _part(payloads, b"ab"), payloads)
    with pytest.raises(IncompleteUpload, match="length"):
        adapter.complete_multipart(session, [part])

    session = adapter.begin_multipart(
        put_operation(provider, payloads, data=b"x", object_id="multipart/identity")  # type: ignore[arg-type]
    )
    mismatched = replace(
        session,
        object_ref=ObjectReference(
            session.object_ref.resource_ref, "different", session.object_ref.digest
        ),
    )
    with pytest.raises(MultipartInvalid, match="does not match"):
        adapter.upload_part(mismatched, 1, _part(payloads, b"x"), payloads)
    adapter.abort_multipart(session)


@pytest.mark.parametrize("publish_once", [False, True])
def test_multipart_detects_create_race_at_completion(
    adapter: OciDistributionAdapter,
    provider: object,
    payloads: PayloadRegistry,
    publish_once: bool,
) -> None:
    object_id = f"multipart/race/{publish_once}"
    immutability = {"mutability": "immutable", "publishOnce": True} if publish_once else None
    operation = put_operation(
        provider,  # type: ignore[arg-type]
        payloads,
        data=b"session",
        object_id=object_id,
        create_only=not publish_once,
        immutability=immutability,
    )
    session = adapter.begin_multipart(operation)
    part = adapter.upload_part(session, 1, _part(payloads, b"session"), payloads)
    adapter.execute(
        put_operation(provider, payloads, data=b"winner", object_id=object_id),  # type: ignore[arg-type]
        payloads,
    )
    expected = ImmutableObjectConflict if publish_once else ConditionalConflict
    with pytest.raises(expected):
        adapter.complete_multipart(session, [part])


def test_multipart_records_post_finalize_failure(
    adapter: OciDistributionAdapter,
    provider: object,
    payloads: PayloadRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = put_operation(
        provider,  # type: ignore[arg-type]
        payloads,
        data=b"payload",
        object_id="multipart/post-finalize",
        retention={
            "retainUntil": "2025-01-01T00:00:00.000000Z",
            "policy": "expired",
            "requireEnforcement": False,
        },
    )
    session = adapter.begin_multipart(operation)
    part = adapter.upload_part(session, 1, _part(payloads, b"payload"), payloads)

    def fail_commit(*_: object, **__: object) -> None:
        raise ObjectUnavailable()

    monkeypatch.setattr(adapter, "_commit_object", fail_commit)
    with pytest.raises(ObjectUnavailable):
        adapter.complete_multipart(session, [part])
    with pytest.raises(MultipartInvalid, match="unknown"):
        adapter.complete_multipart(session, [part])
