# SPDX-License-Identifier: Apache-2.0
"""OCI Distribution 1.1 HTTP transport with bounded retries and safe failures."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, BinaryIO, cast
from urllib.parse import quote, urljoin, urlsplit

import httpx

from meridian_storage.object_common import (
    ConditionalConflict,
    DigestMismatch,
    IncompleteUpload,
    ObjectAuthenticationFailed,
    ObjectAuthorizationFailed,
    ObjectInvalidRequest,
    ObjectNotFound,
    ObjectQuotaExceeded,
    ObjectRateLimited,
    ObjectUnavailable,
    RangeNotSatisfiable,
)

from .._json import sha256_digest
from ..descriptor import OciDistributionBinding
from .models import (
    OCI_MANIFEST_MEDIA_TYPE,
    ManifestDocument,
    OciDescriptor,
    TagPage,
    UploadLocation,
)
from .stream import IteratorReader

_RETRIABLE_STATUS = {408, 425, 500, 502, 503, 504}
_DEFAULT_RESPONSE_BYTES = 64 * 1024


class RegistryHttpClient:
    """One private physical OCI repository client."""

    def __init__(
        self,
        binding: OciDistributionBinding,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_read_attempts: int = 3,
    ) -> None:
        if max_read_attempts < 1:
            raise ValueError("max_read_attempts must be positive")
        self.binding = binding
        self._sleep = sleep
        self._max_read_attempts = max_read_attempts
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=binding.timeout_seconds,
            verify=binding.verify_tls,
            follow_redirects=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> RegistryHttpClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def ping(self) -> Mapping[str, str]:
        response = self._request("GET", self._url("/v2/"), expected={200}, retry=True)
        return {
            "apiVersion": response.headers.get("Docker-Distribution-Api-Version", "unknown"),
            "status": "ok",
        }

    def head_blob(self, digest: str) -> OciDescriptor | None:
        response = self._request(
            "HEAD",
            self._blob_url(digest),
            expected={200, 404},
            retry=True,
        )
        if response.status_code == 404:
            return None
        size = _content_length(response)
        returned = response.headers.get("Docker-Content-Digest", digest)
        if returned != digest:
            raise DigestMismatch("registry blob HEAD returned a different digest")
        return OciDescriptor("application/octet-stream", digest, size)

    def start_upload(self) -> UploadLocation:
        response = self._request(
            "POST",
            self._url(f"/v2/{self._repository()}/blobs/uploads/"),
            headers={"Content-Length": "0"},
            expected={202},
        )
        location = self._location(response)
        minimum = response.headers.get("OCI-Chunk-Min-Length")
        minimum_value = int(minimum) if minimum is not None and minimum.isdigit() else None
        return UploadLocation(
            location,
            response.headers.get("Docker-Upload-UUID"),
            minimum_value,
            0,
        )

    def patch_upload(self, upload: UploadLocation, chunk: bytes) -> UploadLocation:
        if not chunk:
            raise ValueError("an OCI upload chunk cannot be empty")
        start = upload.offset
        end = start + len(chunk) - 1
        response = self._request(
            "PATCH",
            upload.url,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(chunk)),
                "Content-Range": f"{start}-{end}",
            },
            content=chunk,
            expected={202},
        )
        returned_range = response.headers.get("Range")
        if returned_range is not None and returned_range != f"0-{end}":
            raise IncompleteUpload("registry acknowledged an unexpected upload range")
        return UploadLocation(
            self._location(response),
            response.headers.get("Docker-Upload-UUID", upload.uuid),
            upload.minimum_chunk_bytes,
            end + 1,
        )

    def finish_upload(self, upload: UploadLocation, digest: str) -> OciDescriptor:
        url = str(httpx.URL(upload.url).copy_merge_params({"digest": digest}))
        response = self._request(
            "PUT",
            url,
            headers={"Content-Type": "application/octet-stream", "Content-Length": "0"},
            content=b"",
            expected={201},
        )
        returned = response.headers.get("Docker-Content-Digest", digest)
        if returned != digest:
            raise DigestMismatch("registry finalized the blob under a different digest")
        return OciDescriptor("application/octet-stream", digest, upload.offset)

    def abort_upload(self, upload: UploadLocation) -> None:
        self._request(
            "DELETE",
            upload.url,
            expected={204, 404},
            retry=True,
        )

    def upload_status(self, upload: UploadLocation) -> UploadLocation:
        response = self._request("GET", upload.url, expected={204}, retry=True)
        returned_range = response.headers.get("Range", "")
        offset = upload.offset
        if returned_range.startswith("0-") and returned_range[2:].isdigit():
            offset = int(returned_range[2:]) + 1
        return UploadLocation(
            self._location(response),
            response.headers.get("Docker-Upload-UUID", upload.uuid),
            upload.minimum_chunk_bytes,
            offset,
        )

    def upload_blob_bytes(self, value: bytes, *, media_type: str) -> OciDescriptor:
        digest = sha256_digest(value)
        existing = self.head_blob(digest)
        if existing is not None:
            if existing.size != len(value):
                raise DigestMismatch("existing registry blob size does not match its digest")
            return OciDescriptor(media_type, digest, len(value))
        upload = self.start_upload()
        try:
            url = str(httpx.URL(upload.url).copy_merge_params({"digest": digest}))
            response = self._request(
                "PUT",
                url,
                headers={"Content-Type": "application/octet-stream"},
                content=value,
                expected={201},
            )
            returned = response.headers.get("Docker-Content-Digest", digest)
            if returned != digest:
                raise DigestMismatch("registry stored a different blob digest")
        except Exception:
            self._abort_safely(upload)
            raise
        return OciDescriptor(media_type, digest, len(value))

    def get_blob_bytes(self, descriptor: OciDescriptor, *, maximum_bytes: int) -> bytes:
        if descriptor.size > maximum_bytes:
            raise ObjectInvalidRequest("registry blob exceeds the configured metadata bound")
        response = self._request(
            "GET",
            self._blob_url(descriptor.digest),
            expected={200},
            retry=True,
            maximum_response_bytes=maximum_bytes,
        )
        body = response.content
        if len(body) > maximum_bytes or len(body) != descriptor.size:
            raise DigestMismatch("registry blob length does not match its descriptor")
        if sha256_digest(body) != descriptor.digest:
            raise DigestMismatch("registry blob content does not match its descriptor")
        return body

    @contextmanager
    def open_blob(
        self,
        digest: str,
        *,
        start: int | None = None,
        end: int | None = None,
    ) -> Iterator[BinaryIO]:
        headers = {"Accept-Encoding": "identity"}
        ranged = start is not None
        if ranged:
            if end is None:
                raise ValueError("a resolved OCI range requires an end")
            headers["Range"] = f"bytes={start}-{end}"
        request = self._client.build_request(
            "GET",
            self._blob_url(digest),
            headers=self._headers(self._blob_url(digest), headers),
        )
        try:
            response = self._client.send(request, stream=True, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise ObjectUnavailable("OCI registry request failed") from exc
        self._require_safe_response_url(response)
        try:
            expected = {206} if ranged else {200}
            if response.status_code not in expected:
                response.read()
                self._raise_response(response)
            returned = response.headers.get("Docker-Content-Digest")
            if returned is not None and returned != digest:
                raise DigestMismatch("registry blob response identified a different digest")
            if ranged:
                expected_range = f"bytes {start}-{end}/"
                content_range = response.headers.get("Content-Range", "")
                if not content_range.startswith(expected_range):
                    raise RangeNotSatisfiable("registry returned an unexpected byte range")
            yield cast(BinaryIO, IteratorReader(iter(response.iter_bytes())))
        finally:
            response.close()

    def get_manifest(self, reference: str) -> ManifestDocument:
        response = self._request(
            "GET",
            self._manifest_url(reference),
            headers={"Accept": OCI_MANIFEST_MEDIA_TYPE},
            expected={200},
            retry=True,
            maximum_response_bytes=self.binding.max_manifest_bytes,
        )
        body = response.content
        expected_digest = reference if reference.startswith("sha256:") else None
        try:
            document = ManifestDocument.parse(
                body,
                expected_digest=expected_digest,
                maximum_bytes=self.binding.max_manifest_bytes,
            )
        except (TypeError, ValueError) as exc:
            raise DigestMismatch("registry returned an invalid OCI manifest") from exc
        returned = response.headers.get("Docker-Content-Digest")
        if returned is not None and returned != document.digest:
            raise DigestMismatch("registry manifest response digest is invalid")
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type and content_type != document.media_type:
            raise DigestMismatch("registry manifest Content-Type does not match its body")
        return document

    def manifest_exists(self, reference: str) -> bool:
        response = self._request(
            "HEAD",
            self._manifest_url(reference),
            headers={"Accept": OCI_MANIFEST_MEDIA_TYPE},
            expected={200, 404},
            retry=True,
        )
        return response.status_code == 200

    def put_manifest(self, reference: str, body: bytes) -> OciDescriptor:
        document = ManifestDocument.parse(
            body,
            maximum_bytes=self.binding.max_manifest_bytes,
        )
        response = self._request(
            "PUT",
            self._manifest_url(reference),
            headers={"Content-Type": OCI_MANIFEST_MEDIA_TYPE},
            content=body,
            expected={201},
        )
        returned = response.headers.get("Docker-Content-Digest", document.digest)
        if returned != document.digest:
            raise DigestMismatch("registry stored a different manifest digest")
        return OciDescriptor(OCI_MANIFEST_MEDIA_TYPE, document.digest, len(body))

    def delete_manifest(self, digest: str) -> bool:
        response = self._request(
            "DELETE",
            self._manifest_url(digest),
            expected={202, 404},
            retry=True,
        )
        return response.status_code != 404

    def list_tags(self, *, limit: int, last: str | None = None) -> TagPage:
        params: dict[str, str | int] = {"n": limit}
        if last is not None:
            params["last"] = last
        url = str(
            httpx.URL(self._url(f"/v2/{self._repository()}/tags/list")).copy_merge_params(params)
        )
        response = self._request(
            "GET",
            url,
            expected={200, 404},
            retry=True,
            maximum_response_bytes=max(_DEFAULT_RESPONSE_BYTES, limit * 192 + 1024),
        )
        if response.status_code == 404:
            return TagPage((), False)
        try:
            value = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ObjectUnavailable("registry tag response is not valid JSON") from exc
        tags = value.get("tags", []) if isinstance(value, dict) else []
        if tags is None:
            tags = []
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise ObjectUnavailable("registry tag response is invalid")
        has_more = 'rel="next"' in response.headers.get("Link", "") or len(tags) >= limit
        return TagPage(tuple(cast(list[str], tags)), has_more)

    def get_referrers(
        self, subject_digest: str, *, artifact_type: str
    ) -> tuple[OciDescriptor, ...]:
        url = str(
            httpx.URL(
                self._url(f"/v2/{self._repository()}/referrers/{quote(subject_digest, safe=':')}")
            ).copy_add_param("artifactType", artifact_type)
        )
        response = self._request(
            "GET",
            url,
            headers={"Accept": "application/vnd.oci.image.index.v1+json"},
            expected={200, 404},
            retry=True,
            maximum_response_bytes=self.binding.max_manifest_bytes,
        )
        if response.status_code == 404:
            return ()
        try:
            value = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ObjectUnavailable("registry referrers response is not valid JSON") from exc
        manifests = value.get("manifests", []) if isinstance(value, dict) else []
        if not isinstance(manifests, list):
            raise ObjectUnavailable("registry referrers response is invalid")
        result: list[OciDescriptor] = []
        for item in manifests:
            if not isinstance(item, Mapping):
                raise ObjectUnavailable("registry referrers descriptor is invalid")
            try:
                result.append(OciDescriptor.from_mapping(item))
            except (TypeError, ValueError) as exc:
                raise ObjectUnavailable("registry referrers descriptor is invalid") from exc
        return tuple(result)

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
        expected: set[int],
        retry: bool = False,
        maximum_response_bytes: int = _DEFAULT_RESPONSE_BYTES,
    ) -> httpx.Response:
        attempts = self._max_read_attempts if retry else 1
        last_error: httpx.HTTPError | None = None
        for attempt in range(attempts):
            try:
                request = self._client.build_request(
                    method,
                    url,
                    headers=self._headers(url, headers),
                    content=content,
                )
                response = self._client.send(request, stream=True, follow_redirects=True)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt + 1 == attempts:
                    break
                self._sleep(0.05 * (2**attempt))
                continue
            self._require_safe_response_url(response)
            if response.status_code in _RETRIABLE_STATUS and attempt + 1 < attempts:
                response.close()
                self._sleep(0.05 * (2**attempt))
                continue
            status_code = response.status_code
            response_headers = response.headers
            response_extensions = response.extensions
            try:
                response = _bounded_response(response, maximum_response_bytes)
            except ObjectUnavailable:
                if status_code not in expected:
                    self._raise_response(
                        httpx.Response(
                            status_code,
                            headers=response_headers,
                            content=b"",
                            request=request,
                            extensions=response_extensions,
                        )
                    )
                raise
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt + 1 == attempts:
                    break
                self._sleep(0.05 * (2**attempt))
                continue
            if response.status_code in expected:
                return response
            self._raise_response(response)
        raise ObjectUnavailable("OCI registry request failed") from last_error

    def _headers(
        self,
        url: str,
        additional: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        result = {
            "User-Agent": "meridian-storage-oci/1.0.0",
            "Accept": "application/json",
        }
        if _origin(url) == _origin(self.binding.endpoint):
            authorization = self.binding.credentials.authorization_header()
            if authorization is not None:
                result["Authorization"] = authorization
        result.update(additional or {})
        return result

    def _raise_response(self, response: httpx.Response) -> None:
        code = _provider_code(response)
        details: dict[str, Any] = {
            "adapter_provenance": {
                "adapterId": "oci-distribution",
                "providerCode": code,
                "httpStatus": str(response.status_code),
            }
        }
        status = response.status_code
        if status == 401 or code == "UNAUTHORIZED":
            raise ObjectAuthenticationFailed(**details)
        if status == 403 or code == "DENIED":
            raise ObjectAuthorizationFailed(**details)
        if status == 404 or code in {"BLOB_UNKNOWN", "MANIFEST_UNKNOWN", "NAME_UNKNOWN"}:
            raise ObjectNotFound(**details)
        if status == 409:
            raise ConditionalConflict(**details)
        if status == 416:
            raise RangeNotSatisfiable(**details)
        if status == 429 or code == "TOOMANYREQUESTS":
            raise ObjectRateLimited(**details)
        if status in {413, 507}:
            raise ObjectQuotaExceeded(**details)
        if code == "DIGEST_INVALID":
            raise DigestMismatch(**details)
        if code in {"BLOB_UPLOAD_INVALID", "BLOB_UPLOAD_UNKNOWN"}:
            raise IncompleteUpload(**details)
        if 400 <= status < 500:
            raise ObjectInvalidRequest("OCI registry rejected the request", **details)
        raise ObjectUnavailable("OCI registry is unavailable", **details)

    def _location(self, response: httpx.Response) -> str:
        location = response.headers.get("Location")
        if not location:
            raise ObjectUnavailable("registry upload response omitted Location")
        resolved = urljoin(f"{self.binding.endpoint}/", location)
        parsed = urlsplit(resolved)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ObjectUnavailable("registry returned an invalid upload Location")
        if parsed.scheme == "http" and (
            not self.binding.allow_insecure_http
            or _origin(resolved) != _origin(self.binding.endpoint)
        ):
            raise ObjectUnavailable("registry returned an insecure upload Location")
        return cast(str, resolved)

    def _require_safe_response_url(self, response: httpx.Response) -> None:
        parsed = urlsplit(str(response.url))
        allowed_http = self.binding.allow_insecure_http and _origin(str(response.url)) == _origin(
            self.binding.endpoint
        )
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or (parsed.scheme == "http" and not allowed_http)
        ):
            response.close()
            raise ObjectUnavailable("registry returned an insecure redirect")

    def _abort_safely(self, upload: UploadLocation) -> None:
        try:
            self.abort_upload(upload)
        except Exception:
            return

    def _url(self, path: str) -> str:
        return f"{self.binding.endpoint}{path}"

    def _repository(self) -> str:
        return quote(self.binding.repository, safe="/")

    def _blob_url(self, digest: str) -> str:
        return self._url(f"/v2/{self._repository()}/blobs/{quote(digest, safe=':')}")

    def _manifest_url(self, reference: str) -> str:
        return self._url(f"/v2/{self._repository()}/manifests/{quote(reference, safe=':._-')}")


def _provider_code(response: httpx.Response) -> str:
    try:
        value = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "HTTP_ERROR"
    if not isinstance(value, dict):
        return "HTTP_ERROR"
    errors = value.get("errors")
    if not isinstance(errors, list) or not errors or not isinstance(errors[0], dict):
        return "HTTP_ERROR"
    code = errors[0].get("code")
    return code if isinstance(code, str) else "HTTP_ERROR"


def _content_length(response: httpx.Response) -> int:
    value = response.headers.get("Content-Length")
    if value is None or not value.isdigit():
        raise ObjectUnavailable("registry response omitted a valid Content-Length")
    return int(value)


def _bounded_response(response: httpx.Response, maximum_bytes: int) -> httpx.Response:
    body = bytearray()
    try:
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > maximum_bytes:
                raise ObjectUnavailable("OCI registry response exceeded its configured bound")
    finally:
        response.close()
    return httpx.Response(
        response.status_code,
        headers=response.headers,
        content=bytes(body),
        request=response.request,
        extensions=response.extensions,
    )


def _origin(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname or "", port


__all__ = ["RegistryHttpClient"]
