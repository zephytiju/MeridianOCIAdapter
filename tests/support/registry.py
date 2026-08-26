# SPDX-License-Identifier: Apache-2.0
"""Protocol-accurate in-memory OCI Distribution endpoint for deterministic tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import unquote

import httpx

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"


def digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


@dataclass(slots=True)
class FakeRegistry:
    """Small stateful OCI Distribution 1.1 server behind ``httpx.MockTransport``."""

    repository: str = "meridian/test"
    required_authorization: str | None = None
    blobs: dict[str, bytes] = field(default_factory=dict)
    manifests: dict[str, bytes] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)
    uploads: dict[str, bytearray] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    _upload_counter: int = 0

    def reset(self) -> None:
        self.blobs.clear()
        self.manifests.clear()
        self.tags.clear()
        self.uploads.clear()
        self.requests.clear()
        self._upload_counter = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if (
            self.required_authorization is not None
            and request.headers.get("Authorization") != self.required_authorization
        ):
            return self._error(401, "UNAUTHORIZED")
        path = unquote(request.url.path)
        if path == "/v2/":
            return httpx.Response(
                200,
                headers={"Docker-Distribution-Api-Version": "registry/2.0"},
            )
        prefix = f"/v2/{self.repository}"
        if not path.startswith(prefix):
            return self._error(404, "NAME_UNKNOWN")
        suffix = path[len(prefix) :]
        if suffix == "/blobs/uploads/" and request.method == "POST":
            return self._start_upload()
        if suffix.startswith("/blobs/uploads/"):
            return self._upload(request, suffix.removeprefix("/blobs/uploads/"))
        if suffix.startswith("/blobs/"):
            return self._blob(request, suffix.removeprefix("/blobs/"))
        if suffix == "/tags/list" and request.method == "GET":
            return self._tags(request)
        if suffix.startswith("/referrers/") and request.method == "GET":
            return self._referrers(request, suffix.removeprefix("/referrers/"))
        if suffix.startswith("/manifests/"):
            return self._manifest(request, suffix.removeprefix("/manifests/"))
        return self._error(404, "NAME_UNKNOWN")

    def _start_upload(self) -> httpx.Response:
        self._upload_counter += 1
        upload_id = f"upload-{self._upload_counter}"
        self.uploads[upload_id] = bytearray()
        return httpx.Response(
            202,
            headers={
                "Location": f"/v2/{self.repository}/blobs/uploads/{upload_id}",
                "Docker-Upload-UUID": upload_id,
            },
        )

    def _upload(self, request: httpx.Request, upload_id: str) -> httpx.Response:
        value = self.uploads.get(upload_id)
        if value is None:
            return self._error(404, "BLOB_UPLOAD_UNKNOWN")
        location = f"/v2/{self.repository}/blobs/uploads/{upload_id}"
        if request.method == "PATCH":
            start = len(value)
            expected_range = f"{start}-{start + len(request.content) - 1}"
            if request.headers.get("Content-Range") != expected_range:
                return self._error(416, "BLOB_UPLOAD_INVALID")
            value.extend(request.content)
            return httpx.Response(
                202,
                headers={
                    "Location": location,
                    "Docker-Upload-UUID": upload_id,
                    "Range": f"0-{len(value) - 1}",
                },
            )
        if request.method == "GET":
            headers = {"Location": location, "Docker-Upload-UUID": upload_id}
            if value:
                headers["Range"] = f"0-{len(value) - 1}"
            return httpx.Response(204, headers=headers)
        if request.method == "DELETE":
            del self.uploads[upload_id]
            return httpx.Response(204)
        if request.method == "PUT":
            value.extend(request.content)
            declared = request.url.params.get("digest")
            actual = digest(bytes(value))
            if declared != actual:
                return self._error(400, "DIGEST_INVALID")
            self.blobs[actual] = bytes(value)
            del self.uploads[upload_id]
            return httpx.Response(
                201,
                headers={
                    "Location": f"/v2/{self.repository}/blobs/{actual}",
                    "Docker-Content-Digest": actual,
                },
            )
        return self._error(405, "UNSUPPORTED")

    def _blob(self, request: httpx.Request, selected_digest: str) -> httpx.Response:
        value = self.blobs.get(selected_digest)
        if value is None:
            return self._error(404, "BLOB_UNKNOWN")
        base_headers = {
            "Content-Length": str(len(value)),
            "Docker-Content-Digest": selected_digest,
        }
        if request.method == "HEAD":
            return httpx.Response(200, headers=base_headers)
        if request.method != "GET":
            return self._error(405, "UNSUPPORTED")
        range_value = request.headers.get("Range")
        if range_value is None:
            return httpx.Response(200, headers=base_headers, content=value)
        try:
            start_value, end_value = range_value.removeprefix("bytes=").split("-", 1)
            start, end = int(start_value), int(end_value)
        except (TypeError, ValueError):
            return self._error(416, "RANGE_INVALID")
        if start < 0 or end < start or end >= len(value):
            return self._error(416, "RANGE_INVALID")
        selected = value[start : end + 1]
        return httpx.Response(
            206,
            headers={
                "Content-Length": str(len(selected)),
                "Content-Range": f"bytes {start}-{end}/{len(value)}",
                "Docker-Content-Digest": selected_digest,
            },
            content=selected,
        )

    def _manifest(self, request: httpx.Request, reference: str) -> httpx.Response:
        if request.method == "PUT":
            try:
                value = json.loads(request.content)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._error(400, "MANIFEST_INVALID")
            if not isinstance(value, Mapping) or value.get("schemaVersion") != 2:
                return self._error(400, "MANIFEST_INVALID")
            selected_digest = digest(request.content)
            self.manifests[selected_digest] = request.content
            self.tags[reference] = selected_digest
            return httpx.Response(
                201,
                headers={
                    "Location": f"/v2/{self.repository}/manifests/{selected_digest}",
                    "Docker-Content-Digest": selected_digest,
                },
            )
        selected_digest = reference if reference.startswith("sha256:") else self.tags.get(reference)
        if selected_digest is None or selected_digest not in self.manifests:
            return self._error(404, "MANIFEST_UNKNOWN")
        if request.method == "DELETE":
            del self.manifests[selected_digest]
            self.tags = {
                tag: tagged_digest
                for tag, tagged_digest in self.tags.items()
                if tagged_digest != selected_digest
            }
            return httpx.Response(202)
        body = self.manifests[selected_digest]
        headers = {
            "Content-Length": str(len(body)),
            "Content-Type": OCI_MANIFEST,
            "Docker-Content-Digest": selected_digest,
        }
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        if request.method == "GET":
            return httpx.Response(200, headers=headers, content=body)
        return self._error(405, "UNSUPPORTED")

    def _tags(self, request: httpx.Request) -> httpx.Response:
        limit = int(request.url.params.get("n", "100"))
        last = request.url.params.get("last")
        values = sorted(self.tags)
        if last is not None:
            values = [tag for tag in values if tag > last]
        page = values[:limit]
        headers: dict[str, str] = {}
        if len(values) > limit and page:
            headers["Link"] = (
                f'</v2/{self.repository}/tags/list?n={limit}&last={page[-1]}>; rel="next"'
            )
        return httpx.Response(
            200,
            headers=headers,
            json={"name": self.repository, "tags": page},
        )

    def _referrers(self, request: httpx.Request, subject_digest: str) -> httpx.Response:
        artifact_type = request.url.params.get("artifactType")
        descriptors: list[dict[str, object]] = []
        for selected_digest, body in sorted(self.manifests.items()):
            value = json.loads(body)
            subject = value.get("subject")
            if not isinstance(subject, Mapping) or subject.get("digest") != subject_digest:
                continue
            if artifact_type is not None and value.get("artifactType") != artifact_type:
                continue
            descriptors.append(
                {
                    "mediaType": OCI_MANIFEST,
                    "digest": selected_digest,
                    "size": len(body),
                }
            )
        return httpx.Response(
            200,
            headers={"Content-Type": OCI_INDEX},
            json={
                "schemaVersion": 2,
                "mediaType": OCI_INDEX,
                "manifests": descriptors,
            },
        )

    @staticmethod
    def _error(status: int, code: str) -> httpx.Response:
        return httpx.Response(status, json={"errors": [{"code": code, "message": code}]})


__all__ = ["FakeRegistry", "digest"]
