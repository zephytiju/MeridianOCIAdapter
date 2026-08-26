# SPDX-License-Identifier: Apache-2.0
"""Opaque, deterministic OCI tags and signed maintenance cursors."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass

from meridian_storage.object_common import ObjectInvalidRequest

_DIGEST_RE = re.compile(r"sha256:([0-9a-f]{64})\Z")
_TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}\Z")


def object_id_hash(object_id: str) -> str:
    return hashlib.sha256(object_id.encode("utf-8")).hexdigest()


def anchor_tag(object_id: str) -> str:
    return _tag(f"m-a1-{object_id_hash(object_id)}")


def latest_tag(object_id: str) -> str:
    return _tag(f"m-l1-{object_id_hash(object_id)}")


def version_tag(object_id: str, digest: str) -> str:
    match = _DIGEST_RE.fullmatch(digest)
    if match is None:
        raise ValueError("OCI version tags require a SHA-256 digest")
    return _tag(f"m-v1-{object_id_hash(object_id)[:32]}-{match.group(1)}")


def version_tag_prefix(object_id: str | None = None) -> str:
    return "m-v1-" if object_id is None else f"m-v1-{object_id_hash(object_id)[:32]}-"


def _tag(value: str) -> str:
    if _TAG_RE.fullmatch(value) is None:
        raise ValueError("generated OCI tag is invalid")
    return value


@dataclass(frozen=True, slots=True)
class CursorCodec:
    key: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.key, bytes) or len(self.key) < 16:
            raise ValueError("cursor signing key must contain at least 16 bytes")

    def encode(self, last_tag: str) -> str:
        payload = json.dumps(
            {"formatVersion": "meridian.oci.cursor.v1", "last": last_tag},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        signature = hmac.new(self.key, payload, hashlib.sha256).digest()
        return f"v1.{_b64(payload)}.{_b64(signature)}"

    def decode(self, cursor: str) -> str:
        try:
            version, payload_value, signature_value = cursor.split(".", 2)
            payload = _unb64(payload_value)
            signature = _unb64(signature_value)
        except (ValueError, TypeError) as exc:
            raise ObjectInvalidRequest("invalid OCI maintenance cursor") from exc
        expected = hmac.new(self.key, payload, hashlib.sha256).digest()
        if version != "v1" or not hmac.compare_digest(signature, expected):
            raise ObjectInvalidRequest("invalid OCI maintenance cursor")
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ObjectInvalidRequest("invalid OCI maintenance cursor") from exc
        if not isinstance(value, dict) or set(value) != {"formatVersion", "last"}:
            raise ObjectInvalidRequest("invalid OCI maintenance cursor")
        last = value.get("last")
        if value.get("formatVersion") != "meridian.oci.cursor.v1" or not isinstance(last, str):
            raise ObjectInvalidRequest("invalid OCI maintenance cursor")
        if _TAG_RE.fullmatch(last) is None:
            raise ObjectInvalidRequest("invalid OCI maintenance cursor")
        return last


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


__all__ = [
    "CursorCodec",
    "anchor_tag",
    "latest_tag",
    "object_id_hash",
    "version_tag",
    "version_tag_prefix",
]
