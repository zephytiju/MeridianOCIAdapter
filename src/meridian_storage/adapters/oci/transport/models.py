# SPDX-License-Identifier: Apache-2.0
"""Strict OCI descriptor, manifest, upload, and metadata-envelope models."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from meridian_storage.object_common import (
    ImmutabilityRequest,
    ObjectMetadata,
    RetentionRequest,
    parse_object_metadata,
)

from .._json import canonical_json, parse_json_object, sha256_digest

OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_EMPTY_MEDIA_TYPE = "application/vnd.oci.empty.v1+json"
MERIDIAN_OBJECT_ARTIFACT_TYPE = "application/vnd.meridian.object.v1"
MERIDIAN_OBJECT_ANCHOR_TYPE = "application/vnd.meridian.object.anchor.v1"
MERIDIAN_OBJECT_CONFIG_MEDIA_TYPE = "application/vnd.meridian.object.config.v1+json"
STORED_METADATA_FORMAT = "meridian.oci.stored-object.v1"
EMPTY_JSON = b"{}"
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class OciDescriptor:
    media_type: str
    digest: str
    size: int

    def __post_init__(self) -> None:
        if not isinstance(self.media_type, str) or "/" not in self.media_type:
            raise ValueError("OCI descriptor media type is invalid")
        if _DIGEST_RE.fullmatch(self.digest) is None:
            raise ValueError("OCI descriptor digest must be SHA-256")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise ValueError("OCI descriptor size must be a non-negative integer")

    def to_dict(self) -> dict[str, object]:
        return {"mediaType": self.media_type, "digest": self.digest, "size": self.size}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> OciDescriptor:
        required = {"mediaType", "digest", "size"}
        if not required <= set(value):
            raise ValueError("OCI descriptor is missing required fields")
        return cls(
            cast(str, value["mediaType"]),
            cast(str, value["digest"]),
            cast(int, value["size"]),
        )


EMPTY_DESCRIPTOR = OciDescriptor(OCI_EMPTY_MEDIA_TYPE, sha256_digest(EMPTY_JSON), len(EMPTY_JSON))


@dataclass(frozen=True, slots=True)
class ManifestDocument:
    body: bytes
    digest: str
    media_type: str
    value: Mapping[str, object]

    @classmethod
    def parse(
        cls,
        body: bytes,
        *,
        expected_digest: str | None = None,
        maximum_bytes: int,
    ) -> ManifestDocument:
        if len(body) > maximum_bytes:
            raise ValueError("OCI manifest exceeds the configured size bound")
        digest = sha256_digest(body)
        if expected_digest is not None and digest != expected_digest:
            raise ValueError("OCI manifest digest does not match")
        value = parse_json_object(body, label="OCI manifest")
        if value.get("schemaVersion") != 2:
            raise ValueError("OCI manifest schemaVersion must be 2")
        media_type = value.get("mediaType")
        if media_type != OCI_MANIFEST_MEDIA_TYPE:
            raise ValueError("OCI manifest mediaType is unsupported")
        return cls(body, digest, cast(str, media_type), value)


def build_anchor_manifest(*, object_hash: str) -> bytes:
    return canonical_json(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST_MEDIA_TYPE,
            "artifactType": MERIDIAN_OBJECT_ANCHOR_TYPE,
            "config": EMPTY_DESCRIPTOR.to_dict(),
            "layers": [EMPTY_DESCRIPTOR.to_dict()],
            "annotations": {
                "org.meridian.adapter.id": "oci-distribution",
                "org.meridian.object.anchor": object_hash,
                "org.meridian.object.format": STORED_METADATA_FORMAT,
            },
        }
    )


def build_object_manifest(
    *,
    anchor: OciDescriptor,
    config: OciDescriptor,
    payload: OciDescriptor,
    object_hash: str,
    created_at: str,
    mutability: str,
) -> bytes:
    return canonical_json(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST_MEDIA_TYPE,
            "artifactType": MERIDIAN_OBJECT_ARTIFACT_TYPE,
            "config": config.to_dict(),
            "layers": [payload.to_dict()],
            "subject": anchor.to_dict(),
            "annotations": {
                "org.opencontainers.image.created": created_at,
                "org.meridian.adapter.id": "oci-distribution",
                "org.meridian.object.digest": payload.digest,
                "org.meridian.object.id-sha256": object_hash,
                "org.meridian.object.mutability": mutability,
            },
        }
    )


@dataclass(frozen=True, slots=True)
class StoredObject:
    metadata: ObjectMetadata
    immutability: ImmutabilityRequest
    retention: RetentionRequest | None

    def to_bytes(self) -> bytes:
        return canonical_json(
            {
                "formatVersion": STORED_METADATA_FORMAT,
                "metadata": self.metadata.to_dict(),
                "immutability": self.immutability.to_dict(),
                "retention": None if self.retention is None else self.retention.to_dict(),
            }
        )

    @classmethod
    def from_bytes(cls, value: bytes, *, maximum_bytes: int) -> StoredObject:
        if len(value) > maximum_bytes:
            raise ValueError("stored Object metadata exceeds the configured size bound")
        raw = parse_json_object(value, label="stored Object metadata")
        if set(raw) != {"formatVersion", "metadata", "immutability", "retention"}:
            raise ValueError("stored Object metadata contains unknown or missing fields")
        if raw["formatVersion"] != STORED_METADATA_FORMAT:
            raise ValueError("stored Object metadata format is unsupported")
        metadata = raw["metadata"]
        immutability = raw["immutability"]
        retention = raw["retention"]
        if not isinstance(metadata, Mapping) or not isinstance(immutability, Mapping):
            raise ValueError("stored Object metadata fields are invalid")
        if retention is not None and not isinstance(retention, Mapping):
            raise ValueError("stored Object retention field is invalid")
        return cls(
            parse_object_metadata(metadata),
            ImmutabilityRequest.from_mapping(immutability),
            None if retention is None else RetentionRequest.from_mapping(retention),
        )


def manifest_descriptors(
    document: ManifestDocument,
) -> tuple[OciDescriptor, OciDescriptor, OciDescriptor]:
    value = document.value
    if value.get("artifactType") != MERIDIAN_OBJECT_ARTIFACT_TYPE:
        raise ValueError("manifest is not a Meridian Object artifact")
    config = value.get("config")
    layers = value.get("layers")
    subject = value.get("subject")
    if not isinstance(config, Mapping) or not isinstance(subject, Mapping):
        raise ValueError("Meridian Object manifest descriptors are invalid")
    if not isinstance(layers, list) or len(layers) != 1 or not isinstance(layers[0], Mapping):
        raise ValueError("Meridian Object manifest requires exactly one payload layer")
    config_descriptor = OciDescriptor.from_mapping(config)
    payload_descriptor = OciDescriptor.from_mapping(cast(Mapping[str, object], layers[0]))
    subject_descriptor = OciDescriptor.from_mapping(subject)
    if config_descriptor.media_type != MERIDIAN_OBJECT_CONFIG_MEDIA_TYPE:
        raise ValueError("Meridian Object config media type is invalid")
    if subject_descriptor.media_type != OCI_MANIFEST_MEDIA_TYPE:
        raise ValueError("Meridian Object subject must be an OCI manifest")
    return config_descriptor, payload_descriptor, subject_descriptor


@dataclass(frozen=True, slots=True)
class UploadLocation:
    url: str
    uuid: str | None = None
    minimum_chunk_bytes: int | None = None
    offset: int = 0


@dataclass(frozen=True, slots=True)
class TagPage:
    tags: tuple[str, ...]
    has_more: bool


def descriptor_for_bytes(media_type: str, value: bytes) -> OciDescriptor:
    return OciDescriptor(media_type, sha256_digest(value), len(value))


__all__ = [
    "EMPTY_DESCRIPTOR",
    "EMPTY_JSON",
    "MERIDIAN_OBJECT_ANCHOR_TYPE",
    "MERIDIAN_OBJECT_ARTIFACT_TYPE",
    "MERIDIAN_OBJECT_CONFIG_MEDIA_TYPE",
    "OCI_MANIFEST_MEDIA_TYPE",
    "STORED_METADATA_FORMAT",
    "ManifestDocument",
    "OciDescriptor",
    "StoredObject",
    "TagPage",
    "UploadLocation",
    "build_anchor_manifest",
    "build_object_manifest",
    "descriptor_for_bytes",
    "manifest_descriptors",
]
