# SPDX-License-Identifier: Apache-2.0
"""OCI manifest models, stored metadata, and replayable response streams."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from io import BytesIO

import pytest

from meridian_storage.adapters.oci._json import canonical_json, sha256_digest
from meridian_storage.adapters.oci.transport import (
    EMPTY_DESCRIPTOR,
    MERIDIAN_OBJECT_CONFIG_MEDIA_TYPE,
    OCI_MANIFEST_MEDIA_TYPE,
    IteratorReader,
    ManifestDocument,
    OciDescriptor,
    RegistryBlobSource,
    StoredObject,
    build_anchor_manifest,
    build_object_manifest,
    descriptor_for_bytes,
    manifest_descriptors,
)
from meridian_storage.object_common import (
    ImmutabilityRequest,
    ObjectMetadata,
    ObjectReference,
    RetentionRequest,
)
from meridian_storage.semantics import CatalogName, ResourceReference


def _metadata() -> ObjectMetadata:
    payload = b"payload"
    selected_digest = sha256_digest(payload)
    return ObjectMetadata(
        ObjectReference(
            ResourceReference(CatalogName.OBJECT, "assets", "releases"),
            "release/1",
            selected_digest,
        ),
        selected_digest,
        len(payload),
        "application/octet-stream",
        datetime(2026, 8, 26, tzinfo=UTC),
        user_metadata={"channel": "stable"},
    )


def test_manifest_and_stored_metadata_round_trip() -> None:
    metadata = _metadata()
    stored = StoredObject(
        metadata,
        ImmutabilityRequest("immutable", True),
        RetentionRequest(datetime(2027, 1, 1, tzinfo=UTC), "release", True),
    )
    config_value = stored.to_bytes()
    config = descriptor_for_bytes(MERIDIAN_OBJECT_CONFIG_MEDIA_TYPE, config_value)
    payload = OciDescriptor(metadata.media_type, metadata.digest, metadata.byte_length)
    anchor_body = build_anchor_manifest(object_hash="a" * 64)
    anchor_document = ManifestDocument.parse(anchor_body, maximum_bytes=4096)
    anchor = OciDescriptor(
        OCI_MANIFEST_MEDIA_TYPE,
        anchor_document.digest,
        len(anchor_document.body),
    )
    body = build_object_manifest(
        anchor=anchor,
        config=config,
        payload=payload,
        object_hash="a" * 64,
        created_at="2026-08-26T00:00:00.000000Z",
        mutability="immutable",
    )
    document = ManifestDocument.parse(body, expected_digest=sha256_digest(body), maximum_bytes=4096)

    assert document.media_type == OCI_MANIFEST_MEDIA_TYPE
    assert manifest_descriptors(document) == (config, payload, anchor)
    assert StoredObject.from_bytes(config_value, maximum_bytes=4096) == stored
    assert EMPTY_DESCRIPTOR.size == 2


@pytest.mark.parametrize(
    "body",
    [
        canonical_json({"schemaVersion": 1, "mediaType": OCI_MANIFEST_MEDIA_TYPE}),
        canonical_json({"schemaVersion": 2, "mediaType": "application/json"}),
    ],
)
def test_manifest_rejects_unsupported_envelopes(body: bytes) -> None:
    with pytest.raises(ValueError, match="OCI manifest"):
        ManifestDocument.parse(body, maximum_bytes=4096)
    with pytest.raises(ValueError, match="size bound"):
        ManifestDocument.parse(body, maximum_bytes=1)
    with pytest.raises(ValueError, match="digest"):
        ManifestDocument.parse(body, expected_digest="sha256:" + "0" * 64, maximum_bytes=4096)


@pytest.mark.parametrize(
    "descriptor",
    [
        ("invalid", "sha256:" + "a" * 64, 1),
        ("application/octet-stream", "bad", 1),
        ("application/octet-stream", "sha256:" + "a" * 64, -1),
    ],
)
def test_descriptor_validation(descriptor: tuple[str, str, int]) -> None:
    with pytest.raises(ValueError, match="OCI descriptor"):
        OciDescriptor(*descriptor)
    with pytest.raises(ValueError, match="missing"):
        OciDescriptor.from_mapping({"digest": "sha256:" + "a" * 64})


def test_stored_metadata_rejects_unknown_and_invalid_fields() -> None:
    good = StoredObject(_metadata(), ImmutabilityRequest("immutable"), None).to_bytes()
    value = json_load(good)
    value["extra"] = True
    with pytest.raises(ValueError, match="unknown or missing"):
        StoredObject.from_bytes(canonical_json(value), maximum_bytes=4096)
    value = json_load(good)
    value["formatVersion"] = "future"
    with pytest.raises(ValueError, match="unsupported"):
        StoredObject.from_bytes(canonical_json(value), maximum_bytes=4096)
    with pytest.raises(ValueError, match="size bound"):
        StoredObject.from_bytes(good, maximum_bytes=1)
    value = json_load(good)
    value["metadata"] = "invalid"
    with pytest.raises(ValueError, match="fields are invalid"):
        StoredObject.from_bytes(canonical_json(value), maximum_bytes=4096)
    value = json_load(good)
    value["retention"] = "invalid"
    with pytest.raises(ValueError, match="retention field"):
        StoredObject.from_bytes(canonical_json(value), maximum_bytes=4096)


def test_manifest_descriptors_require_meridian_shape() -> None:
    anchor = ManifestDocument.parse(build_anchor_manifest(object_hash="b" * 64), maximum_bytes=4096)
    with pytest.raises(ValueError, match="not a Meridian Object"):
        manifest_descriptors(anchor)

    body = canonical_json(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST_MEDIA_TYPE,
            "artifactType": "application/vnd.meridian.object.v1",
            "config": EMPTY_DESCRIPTOR.to_dict(),
            "layers": [],
            "subject": EMPTY_DESCRIPTOR.to_dict(),
        }
    )
    with pytest.raises(ValueError, match="exactly one"):
        manifest_descriptors(ManifestDocument.parse(body, maximum_bytes=4096))


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("config", "invalid", "descriptors"),
        (
            "config.mediaType",
            "application/octet-stream",
            "config media type",
        ),
        (
            "subject.mediaType",
            "application/octet-stream",
            "subject must be an OCI manifest",
        ),
    ],
)
def test_manifest_descriptor_media_types_and_shapes(
    field: str,
    replacement: object,
    message: str,
) -> None:
    metadata = _metadata()
    config = descriptor_for_bytes(MERIDIAN_OBJECT_CONFIG_MEDIA_TYPE, b"{}")
    payload = OciDescriptor(metadata.media_type, metadata.digest, metadata.byte_length)
    anchor_body = build_anchor_manifest(object_hash="c" * 64)
    anchor = descriptor_for_bytes(OCI_MANIFEST_MEDIA_TYPE, anchor_body)
    value = json_load(
        build_object_manifest(
            anchor=anchor,
            config=config,
            payload=payload,
            object_hash="c" * 64,
            created_at="2026-08-26T00:00:00.000000Z",
            mutability="immutable",
        )
    )
    if "." in field:
        parent, child = field.split(".", 1)
        value[parent][child] = replacement  # type: ignore[index]
    else:
        value[field] = replacement
    document = ManifestDocument.parse(canonical_json(value), maximum_bytes=4096)
    with pytest.raises(ValueError, match=message):
        manifest_descriptors(document)


def test_iterator_reader_supports_sized_unbounded_and_closed_reads() -> None:
    reader = IteratorReader(iter((b"ab", b"cde")))

    assert reader.readable()
    assert reader.read(0) == b""
    assert reader.read(3) == b"abc"
    assert reader.read(10) == b"de"
    assert reader.read() == b""
    reader.close()
    with pytest.raises(ValueError, match="closed"):
        reader.read(1)


def test_registry_blob_source_is_replayable() -> None:
    calls = 0

    @contextmanager
    def opener() -> object:
        nonlocal calls
        calls += 1
        yield BytesIO(b"payload")

    source = RegistryBlobSource(opener)  # type: ignore[arg-type]
    with source.open() as stream:
        assert stream.read() == b"payload"
    with source.open() as stream:
        assert stream.read() == b"payload"
    assert source.replayable
    assert calls == 2


def json_load(value: bytes) -> dict[str, object]:
    import json

    result = json.loads(value)
    assert isinstance(result, dict)
    return result
