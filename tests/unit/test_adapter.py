# SPDX-License-Identifier: Apache-2.0
"""Adapter operation semantics beyond the shared conformance suite."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from io import BytesIO

import pytest

from meridian_storage import Operation, ResourceRef
from meridian_storage.adapters.oci import OciDistributionAdapter, OciDistributionBinding
from meridian_storage.adapters.oci._naming import anchor_tag, latest_tag, version_tag
from meridian_storage.adapters.oci.transport import (
    MERIDIAN_OBJECT_CONFIG_MEDIA_TYPE,
    RegistryHttpClient,
    build_anchor_manifest,
)
from meridian_storage.object_common import (
    ByteRange,
    ConditionalConflict,
    DigestMismatch,
    ImmutableObjectConflict,
    IncompleteUpload,
    ObjectCapabilityMismatch,
    ObjectInvalidRequest,
    ObjectNotFound,
    ObjectUnavailable,
    PayloadReference,
    PayloadRegistry,
    RetentionDenied,
    transfer_payload,
)
from meridian_storage.spi import CapabilityRequirement

from ..support.operations import digest, put_operation
from ..support.registry import FakeRegistry


def _metadata(result: dict[str, object] | object) -> dict[str, object]:
    assert isinstance(result, dict)
    value = result["metadata"]
    assert isinstance(value, dict)
    return value


def test_mutable_updates_preserve_exact_versions_and_latest(
    adapter: OciDistributionAdapter,
    provider: object,
    payloads: PayloadRegistry,
) -> None:
    first = adapter.execute(
        put_operation(provider, payloads, data=b"first", object_id="release/1"),  # type: ignore[arg-type]
        payloads,
    )
    repeated = adapter.execute(
        put_operation(provider, payloads, data=b"first", object_id="release/1"),  # type: ignore[arg-type]
        payloads,
    )
    second = adapter.execute(
        put_operation(provider, payloads, data=b"second", object_id="release/1"),  # type: ignore[arg-type]
        payloads,
    )
    surface = provider.create_surface()  # type: ignore[attr-defined]
    first_ref = _metadata(first)["objectRef"]
    latest_ref = {"resourceRef": first_ref["resourceRef"], "objectId": "release/1", "digest": None}  # type: ignore[index]

    first_stat = adapter.execute(
        provider.normalize(
            surface.stat(resource="object:conformance.objects", reference=first_ref)
        ),  # type: ignore[attr-defined]
        payloads,
    )
    latest = adapter.execute(
        provider.normalize(
            surface.stat(resource="object:conformance.objects", reference=latest_ref)
        ),  # type: ignore[attr-defined]
        payloads,
    )

    assert _metadata(repeated)["createdAt"] == _metadata(first)["createdAt"]
    assert _metadata(first_stat)["digest"] == digest(b"first")
    assert _metadata(latest)["digest"] == digest(b"second")
    assert _metadata(second)["digest"] == digest(b"second")


def test_create_only_and_publish_once_have_distinct_conflicts(
    adapter: OciDistributionAdapter,
    provider: object,
    payloads: PayloadRegistry,
) -> None:
    adapter.execute(
        put_operation(provider, payloads, object_id="immutable", create_only=True),  # type: ignore[arg-type]
        payloads,
    )
    with pytest.raises(ConditionalConflict):
        adapter.execute(
            put_operation(provider, payloads, object_id="immutable", create_only=True),  # type: ignore[arg-type]
            payloads,
        )
    with pytest.raises(ImmutableObjectConflict):
        adapter.execute(
            put_operation(
                provider,  # type: ignore[arg-type]
                payloads,
                object_id="immutable",
                immutability={"mutability": "immutable", "publishOnce": True},
            ),
            payloads,
        )

    published = adapter.execute(
        put_operation(
            provider,  # type: ignore[arg-type]
            payloads,
            data=b"first artifact",
            object_id="publish-once/history",
            immutability={"mutability": "immutable", "publishOnce": True},
        ),
        payloads,
    )
    delete = provider.normalize(  # type: ignore[attr-defined]
        provider.create_surface().delete(  # type: ignore[attr-defined]
            resource="object:conformance.objects",
            reference=_metadata(published)["objectRef"],
        )
    )
    assert adapter.execute(delete, payloads) == {"deleted": True}
    with pytest.raises(ImmutableObjectConflict):
        adapter.execute(
            put_operation(
                provider,  # type: ignore[arg-type]
                payloads,
                data=b"replacement artifact",
                object_id="publish-once/history",
                immutability={"mutability": "immutable", "publishOnce": True},
            ),
            payloads,
        )


def test_range_limits_cursor_pagination_and_prefix_filter(
    adapter: OciDistributionAdapter,
    provider: object,
    payloads: PayloadRegistry,
) -> None:
    for object_id in ("a/1", "a/2", "b/1"):
        adapter.execute(
            put_operation(provider, payloads, object_id=object_id, data=object_id.encode()),  # type: ignore[arg-type]
            payloads,
        )
    surface = provider.create_surface()  # type: ignore[attr-defined]
    first = adapter.execute(
        provider.normalize(
            surface.list(resource="object:conformance.objects", prefix="a/", limit=1)
        ),  # type: ignore[attr-defined]
        payloads,
    )
    assert len(first["items"]) == 1  # type: ignore[arg-type]
    cursor = first["cursor"]
    assert isinstance(cursor, str)
    second = adapter.execute(
        provider.normalize(  # type: ignore[attr-defined]
            surface.list(
                resource="object:conformance.objects",
                prefix="a/",
                limit=1,
                cursor=cursor,
            )
        ),
        payloads,
    )
    assert len(second["items"]) == 1  # type: ignore[arg-type]
    with pytest.raises(ObjectInvalidRequest, match="cursor"):
        adapter.execute(
            provider.normalize(  # type: ignore[attr-defined]
                surface.list(
                    resource="object:conformance.objects",
                    prefix="",
                    cursor=cursor + "tampered",
                )
            ),
            payloads,
        )

    reference = _metadata(
        adapter.execute(
            put_operation(provider, payloads, object_id="range", data=b"0123456789"),  # type: ignore[arg-type]
            payloads,
        )
    )["objectRef"]
    limited_binding = replace(adapter.binding, max_range_bytes=2)
    limited = OciDistributionAdapter(
        limited_binding,
        transport=adapter.transport,
        clock=adapter._clock,
    )
    with pytest.raises(ObjectCapabilityMismatch, match="range"):
        limited.execute(
            provider.normalize(  # type: ignore[attr-defined]
                surface.read_range(
                    resource="object:conformance.objects",
                    reference=reference,
                    byte_range=ByteRange(0, 2),
                )
            ),
            payloads,
        )


def test_get_payload_is_replayable_and_digest_bound(
    adapter: OciDistributionAdapter,
    provider: object,
    payloads: PayloadRegistry,
) -> None:
    result = adapter.execute(
        put_operation(provider, payloads, data=b"download", object_id="get"),  # type: ignore[arg-type]
        payloads,
    )
    surface = provider.create_surface()  # type: ignore[attr-defined]
    fetched = adapter.execute(
        provider.normalize(  # type: ignore[attr-defined]
            surface.get(
                resource="object:conformance.objects",
                reference=_metadata(result)["objectRef"],
            )
        ),
        payloads,
    )
    reference = PayloadReference.from_mapping(fetched["payload"])  # type: ignore[arg-type]
    for _ in range(2):
        sink = BytesIO()
        identity = transfer_payload(reference, payloads, sink)
        assert sink.getvalue() == b"download"
        assert identity.digest == digest(b"download")


def test_retention_blocks_delete_until_expiry(
    adapter: OciDistributionAdapter,
    provider: object,
    payloads: PayloadRegistry,
) -> None:
    result = adapter.execute(
        put_operation(
            provider,  # type: ignore[arg-type]
            payloads,
            object_id="retained",
            retention={
                "retainUntil": "2027-01-01T00:00:00.000000Z",
                "policy": "release",
                "requireEnforcement": True,
            },
        ),
        payloads,
    )
    expression = provider.create_surface().delete(  # type: ignore[attr-defined]
        resource="object:conformance.objects",
        reference=_metadata(result)["objectRef"],
        reason="too-soon",
    )
    with pytest.raises(RetentionDenied):
        adapter.execute(provider.normalize(expression), payloads)  # type: ignore[attr-defined]


def test_payload_and_metadata_limits_abort_cleanly(
    binding: OciDistributionBinding,
    transport: RegistryHttpClient,
    provider: object,
) -> None:
    payloads = PayloadRegistry()
    tiny = OciDistributionAdapter(replace(binding, max_object_bytes=1), transport=transport)
    with pytest.raises(ObjectInvalidRequest, match="size limit"):
        tiny.execute(
            put_operation(provider, payloads, data=b"too large", expected_length=None),  # type: ignore[arg-type]
            payloads,
        )

    metadata_limited = OciDistributionAdapter(
        replace(binding, max_metadata_bytes=1024),
        transport=transport,
    )
    with pytest.raises(ObjectInvalidRequest, match="metadata"):
        metadata_limited.execute(
            put_operation(
                provider,  # type: ignore[arg-type]
                payloads,
                object_id="large-metadata",
                user_metadata={"large": "x" * 2000},
            ),
            payloads,
        )


def test_reserved_provenance_and_corrupt_anchor_are_rejected(
    adapter: OciDistributionAdapter,
    provider: object,
    payloads: PayloadRegistry,
    registry: FakeRegistry,
) -> None:
    with pytest.raises(ObjectInvalidRequest, match="reserved"):
        adapter.execute(
            put_operation(
                provider,  # type: ignore[arg-type]
                payloads,
                object_id="reserved",
                provenance={"meridianAdapter": {"forged": True}},
            ),
            payloads,
        )

    wrong = adapter.transport.put_manifest(
        "wrong-anchor",
        build_anchor_manifest(object_hash="b" * 64),
    )
    registry.tags[anchor_tag("collision")] = wrong.digest
    with pytest.raises(ConditionalConflict, match="anchor"):
        adapter.execute(
            put_operation(provider, payloads, object_id="collision"),  # type: ignore[arg-type]
            payloads,
        )


def test_operation_validation_and_capability_negotiation(
    adapter: OciDistributionAdapter,
    provider: object,
    payloads: PayloadRegistry,
) -> None:
    valid = put_operation(provider, payloads)  # type: ignore[arg-type]
    cases: list[tuple[Operation, type[Exception]]] = [
        (
            replace(
                valid,
                catalog="structured",
                resources=(ResourceRef("structured", "other", "resource"),),
            ),
            ObjectInvalidRequest,
        ),
        (replace(valid, operation_version="2.0.0"), ObjectCapabilityMismatch),
        (
            replace(valid, resources=(ResourceRef("object", "other", "resource"),)),
            ObjectInvalidRequest,
        ),
        (
            replace(
                valid,
                requirements=(
                    CapabilityRequirement(
                        "meridian.object.put",
                        "1.0.0",
                        guarantees=("object.nonexistent",),
                    ),
                ),
            ),
            ObjectCapabilityMismatch,
        ),
    ]
    for operation, error in cases:
        with pytest.raises(error):
            adapter.execute(operation, payloads)


def test_delete_disabled_and_wrong_references_are_stable_errors(
    binding: OciDistributionBinding,
    transport: RegistryHttpClient,
    provider: object,
    payloads: PayloadRegistry,
) -> None:
    restricted = OciDistributionAdapter(
        replace(binding, deletion_enabled=False), transport=transport
    )
    result = restricted.execute(put_operation(provider, payloads), payloads)  # type: ignore[arg-type]
    surface = provider.create_surface()  # type: ignore[attr-defined]
    delete = provider.normalize(  # type: ignore[attr-defined]
        surface.delete(
            resource="object:conformance.objects",
            reference=_metadata(result)["objectRef"],
        )
    )
    with pytest.raises(ObjectCapabilityMismatch):
        restricted.execute(delete, payloads)

    stat = provider.normalize(  # type: ignore[attr-defined]
        surface.stat(
            resource="object:conformance.objects",
            reference=_metadata(result)["objectRef"],
        )
    )
    bad_input = dict(stat.input)
    bad_input["reference"] = "not-a-mapping"
    with pytest.raises(ObjectInvalidRequest, match="mapping"):
        restricted.execute(replace(stat, input=bad_input), payloads)

    wrong_input = dict(stat.input)
    wrong_input["reference"] = {
        "resourceRef": {"catalog": "object", "namespace": "other", "name": "resource"},
        "objectId": "fixture/object",
        "digest": digest(b"payload"),
    }
    with pytest.raises(ObjectInvalidRequest, match="binding"):
        restricted.execute(replace(stat, input=wrong_input), payloads)

    missing = dict(stat.input)
    missing["reference"] = {
        "resourceRef": {
            "catalog": "object",
            "namespace": "conformance",
            "name": "objects",
        },
        "objectId": "missing",
        "digest": digest(b"missing"),
    }
    with pytest.raises(ObjectNotFound):
        restricted.execute(replace(stat, input=missing), payloads)


def test_adapter_lifecycle(
    adapter: OciDistributionAdapter,
    binding: OciDistributionBinding,
) -> None:
    assert adapter.adapter_id == "oci-distribution"
    assert adapter.__enter__() is adapter
    adapter.__exit__()
    owned = OciDistributionAdapter(binding)
    owned.close()


def test_serialized_input_validation_paths(
    adapter: OciDistributionAdapter,
    provider: object,
    payloads: PayloadRegistry,
) -> None:
    put = put_operation(provider, payloads)  # type: ignore[arg-type]
    bad_payload = dict(put.input)
    bad_payload["payload"] = "private-sdk-object"
    with pytest.raises(ObjectInvalidRequest, match="serialized PayloadReference"):
        adapter.execute(replace(put, input=bad_payload), payloads)

    bad_provenance = dict(put.input)
    bad_provenance["provenance"] = "invalid"
    with pytest.raises(ObjectInvalidRequest, match="provenance"):
        adapter.execute(replace(put, input=bad_provenance), payloads)

    result = adapter.execute(
        put_operation(provider, payloads, object_id="range-shape"),  # type: ignore[arg-type]
        payloads,
    )
    surface = provider.create_surface()  # type: ignore[attr-defined]
    operation = provider.normalize(  # type: ignore[attr-defined]
        surface.read_range(
            resource="object:conformance.objects",
            reference=_metadata(result)["objectRef"],
            byte_range=ByteRange(0, 1),
        )
    )
    invalid_range = dict(operation.input)
    invalid_range["range"] = "bytes=0-1"
    with pytest.raises(ObjectInvalidRequest, match="serialized ByteRange"):
        adapter.execute(replace(operation, input=invalid_range), payloads)

    invalid_reference = dict(operation.input)
    invalid_reference["reference"] = {"invalid": True}
    with pytest.raises(ObjectInvalidRequest, match="invalid Object reference"):
        adapter.execute(replace(operation, input=invalid_reference), payloads)


def test_declared_lengths_and_transient_replay_behavior(
    adapter: OciDistributionAdapter,
    provider: object,
    payloads: PayloadRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(IncompleteUpload, match="declared length"):
        adapter.execute(
            put_operation(
                provider,  # type: ignore[arg-type]
                payloads,
                data=b"two",
                object_id="length/exceeded",
                expected_length=1,
            ),
            payloads,
        )
    with pytest.raises(IncompleteUpload):
        adapter.execute(
            put_operation(
                provider,  # type: ignore[arg-type]
                payloads,
                data=b"one",
                object_id="length/short",
                expected_length=4,
            ),
            payloads,
        )

    original = adapter.transport.patch_upload
    calls = 0

    def fail_once(upload: object, chunk: bytes) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ObjectUnavailable()
        return original(upload, chunk)  # type: ignore[arg-type]

    monkeypatch.setattr(adapter.transport, "patch_upload", fail_once)
    result = adapter.execute(
        put_operation(
            provider,  # type: ignore[arg-type]
            payloads,
            data=b"retry",
            object_id="transient/replay",
        ),
        payloads,
    )
    assert _metadata(result)["digest"] == digest(b"retry")
    assert calls == 2

    def always_unavailable(*_: object) -> None:
        raise ObjectUnavailable()

    monkeypatch.setattr(adapter.transport, "patch_upload", always_unavailable)
    with pytest.raises(ObjectUnavailable):
        adapter.execute(
            put_operation(
                provider,  # type: ignore[arg-type]
                payloads,
                data=b"once",
                object_id="transient/nonreplay",
                replayable=False,
            ),
            payloads,
        )


def test_partial_exact_version_and_delete_race_errors(
    adapter: OciDistributionAdapter,
    provider: object,
    payloads: PayloadRegistry,
    registry: FakeRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_id = "partial/exact"
    adapter.execute(
        put_operation(provider, payloads, object_id=object_id),  # type: ignore[arg-type]
        payloads,
    )
    registry.tags.pop(latest_tag(object_id))
    with pytest.raises(ConditionalConflict, match="exact Object version"):
        adapter.execute(
            put_operation(provider, payloads, object_id=object_id, create_only=True),  # type: ignore[arg-type]
            payloads,
        )

    result = adapter.execute(
        put_operation(provider, payloads, object_id="delete/race"),  # type: ignore[arg-type]
        payloads,
    )
    delete = provider.normalize(  # type: ignore[attr-defined]
        provider.create_surface().delete(  # type: ignore[attr-defined]
            resource="object:conformance.objects",
            reference=_metadata(result)["objectRef"],
        )
    )
    monkeypatch.setattr(adapter.transport, "delete_manifest", lambda _: False)
    with pytest.raises(ObjectNotFound):
        adapter.execute(delete, payloads)


def test_corrupt_subject_payload_and_metadata_are_detected(
    adapter: OciDistributionAdapter,
    provider: object,
    payloads: PayloadRegistry,
) -> None:
    surface = provider.create_surface()  # type: ignore[attr-defined]
    for object_id, mutation, message in (
        (
            "corrupt/subject",
            lambda body: body["subject"].__setitem__("digest", "sha256:" + "0" * 64),
            "subject",
        ),
        (
            "corrupt/payload",
            lambda body: body["layers"][0].__setitem__("size", body["layers"][0]["size"] + 1),
            "payload descriptor",
        ),
    ):
        result = adapter.execute(
            put_operation(provider, payloads, object_id=object_id),  # type: ignore[arg-type]
            payloads,
        )
        reference = _metadata(result)["objectRef"]
        tag = version_tag(object_id, digest(b"payload"))
        body = json.loads(adapter.transport.get_manifest(tag).body)
        mutation(body)
        changed = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        adapter.transport.put_manifest(tag, changed)
        with pytest.raises(DigestMismatch, match=message):
            adapter.execute(
                provider.normalize(  # type: ignore[attr-defined]
                    surface.stat(resource="object:conformance.objects", reference=reference)
                ),
                payloads,
            )

    object_id = "corrupt/config"
    result = adapter.execute(
        put_operation(provider, payloads, object_id=object_id),  # type: ignore[arg-type]
        payloads,
    )
    reference = _metadata(result)["objectRef"]
    tag = version_tag(object_id, digest(b"payload"))
    body = json.loads(adapter.transport.get_manifest(tag).body)
    invalid = adapter.transport.upload_blob_bytes(
        b"not-json",
        media_type=MERIDIAN_OBJECT_CONFIG_MEDIA_TYPE,
    )
    body["config"] = invalid.to_dict()
    adapter.transport.put_manifest(
        tag,
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode(),
    )
    with pytest.raises(DigestMismatch, match="metadata"):
        adapter.execute(
            provider.normalize(  # type: ignore[attr-defined]
                surface.stat(resource="object:conformance.objects", reference=reference)
            ),
            payloads,
        )


def test_naive_clock_and_failed_abort_are_safe(
    adapter: OciDistributionAdapter,
    provider: object,
    payloads: PayloadRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    naive = OciDistributionAdapter(
        adapter.binding,
        transport=adapter.transport,
        clock=lambda: datetime(2026, 8, 26),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        naive.execute(
            put_operation(provider, payloads, object_id="naive-clock"),  # type: ignore[arg-type]
            payloads,
        )

    upload = adapter.transport.start_upload()

    def unavailable(_: object) -> None:
        raise ObjectUnavailable()

    monkeypatch.setattr(adapter.transport, "abort_upload", unavailable)
    adapter._abort_safely(upload)
