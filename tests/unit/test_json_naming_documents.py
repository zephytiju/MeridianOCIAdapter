# SPDX-License-Identifier: Apache-2.0
"""Deterministic JSON, private naming, packaged evidence, and cursor safety."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from meridian_storage.adapters.oci import compatibility_document
from meridian_storage.adapters.oci._json import canonical_json, parse_json_object, sha256_digest
from meridian_storage.adapters.oci._naming import (
    CursorCodec,
    _tag,
    anchor_tag,
    latest_tag,
    object_id_hash,
    version_tag,
    version_tag_prefix,
)
from meridian_storage.object_common import ObjectInvalidRequest


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _signed_cursor(key: bytes, payload: bytes) -> str:
    signature = hmac.new(key, payload, hashlib.sha256).digest()
    return f"v1.{_b64(payload)}.{_b64(signature)}"


def test_json_helpers_are_canonical_and_strict() -> None:
    encoded = canonical_json({"z": "☃", "a": 1})

    assert encoded == b'{"a":1,"z":"\xe2\x98\x83"}'
    assert parse_json_object(encoded, label="fixture") == {"a": 1, "z": "☃"}
    assert sha256_digest(b"payload") == "sha256:" + hashlib.sha256(b"payload").hexdigest()
    with pytest.raises(ValueError, match="valid UTF-8 JSON"):
        parse_json_object(b"\xff", label="fixture")
    with pytest.raises(ValueError, match="JSON object"):
        parse_json_object(b"[]", label="fixture")


def test_private_tags_are_deterministic_and_do_not_expose_object_ids() -> None:
    object_id = "customers/acme/private release"
    selected_digest = "sha256:" + "a" * 64

    assert object_id_hash(object_id) == hashlib.sha256(object_id.encode()).hexdigest()
    assert anchor_tag(object_id).startswith("m-a1-")
    assert latest_tag(object_id).startswith("m-l1-")
    assert version_tag(object_id, selected_digest).startswith(version_tag_prefix(object_id))
    assert object_id not in anchor_tag(object_id)
    assert version_tag_prefix() == "m-v1-"
    with pytest.raises(ValueError, match="SHA-256"):
        version_tag(object_id, "sha512:bad")
    with pytest.raises(ValueError, match="generated OCI tag"):
        _tag("bad tag")


def test_signed_cursor_round_trip_and_rejects_tampering() -> None:
    codec = CursorCodec(b"0123456789abcdef")
    encoded = codec.encode("m-v1-safe")

    assert codec.decode(encoded) == "m-v1-safe"
    with pytest.raises(ObjectInvalidRequest):
        codec.decode(encoded[:-1] + ("A" if encoded[-1] != "A" else "B"))
    with pytest.raises(ObjectInvalidRequest):
        codec.decode("not-a-cursor")
    with pytest.raises(ValueError, match="at least 16"):
        CursorCodec(b"short")


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"[]",
        json.dumps({"formatVersion": "wrong", "last": "m-v1-safe"}).encode(),
        json.dumps({"formatVersion": "meridian.oci.cursor.v1", "last": "bad tag!"}).encode(),
        json.dumps(
            {"formatVersion": "meridian.oci.cursor.v1", "last": "m-v1-safe", "extra": 1}
        ).encode(),
    ],
)
def test_signed_cursor_rejects_invalid_signed_payloads(payload: bytes) -> None:
    key = b"0123456789abcdef"
    with pytest.raises(ObjectInvalidRequest):
        CursorCodec(key).decode(_signed_cursor(key, payload))


def test_packaged_compatibility_evidence_is_available() -> None:
    document = compatibility_document()

    assert document["adapterId"] == "oci-distribution"
    assert document["objectCommon"]["version"] == "1.0.1"  # type: ignore[index]
    assert document["design"]["hldRevision"] == 56  # type: ignore[index]
