# SPDX-License-Identifier: Apache-2.0
"""OCI Distribution HTTP behavior, retries, authentication, and error translation."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import replace

import httpx
import pytest

from meridian_storage.adapters.oci import BasicCredentials, OciDistributionBinding
from meridian_storage.adapters.oci._json import sha256_digest
from meridian_storage.adapters.oci.transport import (
    EMPTY_JSON,
    MERIDIAN_OBJECT_ARTIFACT_TYPE,
    MERIDIAN_OBJECT_CONFIG_MEDIA_TYPE,
    OciDescriptor,
    RegistryHttpClient,
    UploadLocation,
    build_anchor_manifest,
    build_object_manifest,
)
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

from ..support.registry import FakeRegistry


class _ChunkStream(httpx.SyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks


def _client(
    binding: OciDistributionBinding,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    attempts: int = 1,
    sleep: Callable[[float], None] = lambda _: None,
) -> tuple[RegistryHttpClient, httpx.Client]:
    raw = httpx.Client(transport=httpx.MockTransport(handler))
    return (
        RegistryHttpClient(
            binding,
            client=raw,
            sleep=sleep,
            max_read_attempts=attempts,
        ),
        raw,
    )


def test_blob_upload_status_download_range_and_abort(
    transport: RegistryHttpClient,
    registry: FakeRegistry,
) -> None:
    assert transport.ping() == {"apiVersion": "registry/2.0", "status": "ok"}
    assert transport.head_blob("sha256:" + "0" * 64) is None

    upload = transport.start_upload()
    upload = transport.patch_upload(upload, b"pay")
    assert transport.upload_status(upload).offset == 3
    upload = transport.patch_upload(upload, b"load")
    selected_digest = sha256_digest(b"payload")
    descriptor = transport.finish_upload(upload, selected_digest)

    assert descriptor.digest == selected_digest
    assert transport.head_blob(selected_digest).size == 7  # type: ignore[union-attr]
    with transport.open_blob(selected_digest) as stream:
        assert stream.read() == b"payload"
    with transport.open_blob(selected_digest, start=1, end=3) as stream:
        assert stream.read() == b"ayl"

    abandoned = transport.start_upload()
    transport.abort_upload(abandoned)
    transport.abort_upload(abandoned)
    assert registry.uploads == {}


def test_monolithic_blobs_manifests_tags_referrers_and_delete(
    transport: RegistryHttpClient,
) -> None:
    empty = transport.upload_blob_bytes(EMPTY_JSON, media_type="application/vnd.oci.empty.v1+json")
    assert (
        transport.upload_blob_bytes(EMPTY_JSON, media_type="application/vnd.oci.empty.v1+json")
        == empty
    )
    config = transport.upload_blob_bytes(
        b'{"fixture":true}', media_type=MERIDIAN_OBJECT_CONFIG_MEDIA_TYPE
    )
    payload = transport.upload_blob_bytes(b"payload", media_type="application/octet-stream")
    anchor = transport.put_manifest("anchor", build_anchor_manifest(object_hash="a" * 64))
    manifest_body = build_object_manifest(
        anchor=anchor,
        config=config,
        payload=payload,
        object_hash="a" * 64,
        created_at="2026-08-26T00:00:00.000000Z",
        mutability="immutable",
    )
    version = transport.put_manifest("version", manifest_body)

    assert transport.manifest_exists("version")
    assert transport.get_manifest("version").body == manifest_body
    assert transport.get_manifest(version.digest).digest == version.digest
    assert transport.get_blob_bytes(config, maximum_bytes=1024) == b'{"fixture":true}'
    assert transport.list_tags(limit=1).tags == ("anchor",)
    second_page = transport.list_tags(limit=10, last="anchor")
    assert second_page.tags == ("version",)
    assert any(
        item.digest == version.digest
        for item in transport.get_referrers(
            anchor.digest,
            artifact_type=MERIDIAN_OBJECT_ARTIFACT_TYPE,
        )
    )
    assert transport.delete_manifest(version.digest)
    assert not transport.delete_manifest(version.digest)
    assert not transport.manifest_exists("version")


@pytest.mark.parametrize(
    ("status", "code", "error"),
    [
        (401, "UNAUTHORIZED", ObjectAuthenticationFailed),
        (403, "DENIED", ObjectAuthorizationFailed),
        (404, "NAME_UNKNOWN", ObjectNotFound),
        (409, "CONFLICT", ConditionalConflict),
        (416, "RANGE_INVALID", RangeNotSatisfiable),
        (429, "TOOMANYREQUESTS", ObjectRateLimited),
        (413, "SIZE_INVALID", ObjectQuotaExceeded),
        (400, "DIGEST_INVALID", DigestMismatch),
        (400, "BLOB_UPLOAD_INVALID", IncompleteUpload),
        (400, "MANIFEST_INVALID", ObjectInvalidRequest),
        (500, "UNKNOWN", ObjectUnavailable),
    ],
)
def test_provider_errors_map_to_stable_common_errors(
    binding: OciDistributionBinding,
    status: int,
    code: str,
    error: type[Exception],
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"errors": [{"code": code}]})

    transport, raw = _client(binding, handler)
    try:
        with pytest.raises(error) as raised:
            transport.ping()
        public = raised.value.to_dict()  # type: ignore[attr-defined]
        assert public["adapterProvenance"]["providerCode"] == code
        assert "registry.test" not in json.dumps(public)
    finally:
        raw.close()


def test_retry_is_bounded_for_status_and_network_failures(
    binding: OciDistributionBinding,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("offline", request=request)
        if calls == 2:
            return httpx.Response(503)
        return httpx.Response(
            200,
            headers={"Docker-Distribution-Api-Version": "registry/2.0"},
        )

    transport, raw = _client(binding, handler, attempts=3, sleep=sleeps.append)
    try:
        assert transport.ping()["status"] == "ok"
        assert calls == 3
        assert sleeps == [0.05, 0.1]
    finally:
        raw.close()


def test_authorization_is_not_forwarded_to_cross_origin_upload_location() -> None:
    binding = OciDistributionBinding(
        resource="object:assets.releases",
        endpoint="https://registry.test",
        repository="meridian/test",
        credentials=BasicCredentials("user", "secret"),
    )
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("Authorization")))
        if request.method == "POST":
            return httpx.Response(202, headers={"Location": "https://blob.test/upload/1"})
        return httpx.Response(
            202,
            headers={"Location": "https://blob.test/upload/1", "Range": "0-2"},
        )

    transport, raw = _client(binding, handler)
    try:
        transport.patch_upload(transport.start_upload(), b"abc")
        assert seen[0][0] == "registry.test"
        assert seen[0][1] is not None
        assert seen[1] == ("blob.test", None)
    finally:
        raw.close()


def test_transport_rejects_https_downgrade_redirects(
    binding: OciDistributionBinding,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "registry.test":
            return httpx.Response(302, headers={"Location": "http://blob.test/v2/"})
        return httpx.Response(200, json={})

    transport, raw = _client(binding, handler)
    try:
        with pytest.raises(ObjectUnavailable, match="insecure redirect"):
            transport.ping()
    finally:
        raw.close()


def test_transport_rejects_inconsistent_provider_responses(
    binding: OciDistributionBinding,
) -> None:
    responses = iter(
        [
            httpx.Response(200),
            httpx.Response(
                200,
                headers={
                    "Content-Length": "7",
                    "Docker-Content-Digest": "sha256:" + "b" * 64,
                },
            ),
            httpx.Response(202),
        ]
    )
    transport, raw = _client(binding, lambda _: next(responses))
    selected_digest = "sha256:" + "a" * 64
    try:
        with pytest.raises(ObjectUnavailable, match="Content-Length"):
            transport.head_blob(selected_digest)
        with pytest.raises(DigestMismatch, match="different digest"):
            transport.head_blob(selected_digest)
        with pytest.raises(ObjectUnavailable, match="Location"):
            transport.start_upload()
    finally:
        raw.close()


def test_large_head_content_length_is_metadata_not_a_response_body(
    binding: OciDistributionBinding,
) -> None:
    selected_digest = "sha256:" + "a" * 64
    transport, raw = _client(
        binding,
        lambda _: httpx.Response(
            200,
            headers={
                "Content-Length": str(5 * 1024**4),
                "Docker-Content-Digest": selected_digest,
            },
        ),
    )
    try:
        descriptor = transport.head_blob(selected_digest)
        assert descriptor is not None
        assert descriptor.size == 5 * 1024**4
    finally:
        raw.close()


def test_buffered_responses_are_bounded_without_losing_status_mapping(
    binding: OciDistributionBinding,
) -> None:
    limited = replace(binding, max_manifest_bytes=1024)
    transport, raw = _client(
        limited,
        lambda _: httpx.Response(
            200,
            headers={"Content-Type": "application/vnd.oci.image.manifest.v1+json"},
            stream=_ChunkStream(b"x" * 600, b"y" * 600),
        ),
    )
    try:
        with pytest.raises(ObjectUnavailable, match="configured bound"):
            transport.get_manifest("oversized")
    finally:
        raw.close()

    transport, raw = _client(
        binding,
        lambda _: httpx.Response(401, content=b"x" * (65 * 1024)),
    )
    try:
        with pytest.raises(ObjectAuthenticationFailed):
            transport.ping()
    finally:
        raw.close()


def test_stream_rejects_wrong_digest_range_and_http_status(
    binding: OciDistributionBinding,
) -> None:
    selected_digest = "sha256:" + "a" * 64
    responses = iter(
        [
            httpx.Response(200, headers={"Docker-Content-Digest": "sha256:" + "b" * 64}),
            httpx.Response(206, headers={"Content-Range": "bytes 2-3/4"}),
            httpx.Response(404, json={"errors": [{"code": "BLOB_UNKNOWN"}]}),
        ]
    )
    transport, raw = _client(binding, lambda _: next(responses))
    try:
        with pytest.raises(DigestMismatch), transport.open_blob(selected_digest) as stream:
            stream.read()
        with (
            pytest.raises(RangeNotSatisfiable),
            transport.open_blob(selected_digest, start=0, end=1) as stream,
        ):
            stream.read()
        with pytest.raises(ObjectNotFound), transport.open_blob(selected_digest) as stream:
            stream.read()
        with (
            pytest.raises(ValueError, match="requires an end"),
            transport.open_blob(selected_digest, start=0),
        ):
            pass
    finally:
        raw.close()


def test_constructor_and_owned_client_lifecycle(binding: OciDistributionBinding) -> None:
    with pytest.raises(ValueError, match="positive"):
        RegistryHttpClient(binding, max_read_attempts=0)

    transport = RegistryHttpClient(binding)
    assert transport.__enter__() is transport
    transport.__exit__()


def test_upload_response_validation_and_safe_abort(binding: OciDistributionBinding) -> None:
    selected_digest = "sha256:" + "a" * 64
    transport, raw = _client(binding, lambda _: httpx.Response(500))
    try:
        with pytest.raises(ValueError, match="cannot be empty"):
            transport.patch_upload(UploadLocation("https://registry.test/upload"), b"")
    finally:
        raw.close()

    responses = iter(
        [
            httpx.Response(
                202,
                headers={"Location": "/upload", "Range": "0-99"},
            ),
            httpx.Response(201, headers={"Docker-Content-Digest": "sha256:" + "b" * 64}),
        ]
    )
    transport, raw = _client(binding, lambda _: next(responses))
    try:
        with pytest.raises(IncompleteUpload, match="range"):
            transport.patch_upload(UploadLocation("https://registry.test/upload"), b"abc")
        with pytest.raises(DigestMismatch, match="finalized"):
            transport.finish_upload(
                UploadLocation("https://registry.test/upload", offset=3), selected_digest
            )
    finally:
        raw.close()


def test_monolithic_upload_failure_attempts_abort(binding: OciDistributionBinding) -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(404)
        if request.method == "POST":
            return httpx.Response(202, headers={"Location": "/upload"})
        if request.method == "PUT":
            return httpx.Response(
                201,
                headers={"Docker-Content-Digest": "sha256:" + "0" * 64},
            )
        raise httpx.ConnectError("abort unavailable", request=request)

    transport, raw = _client(binding, handler)
    try:
        with pytest.raises(DigestMismatch, match="different blob"):
            transport.upload_blob_bytes(b"payload", media_type="application/octet-stream")
        assert methods == ["HEAD", "POST", "PUT", "DELETE"]
    finally:
        raw.close()


def test_existing_and_downloaded_blob_sizes_and_digests_are_verified(
    binding: OciDistributionBinding,
) -> None:
    value = b"payload"
    selected_digest = sha256_digest(value)
    registry = FakeRegistry()
    registry.blobs[selected_digest] = value + b"extra"
    transport, raw = _client(binding, registry.handle)
    try:
        with pytest.raises(DigestMismatch, match="existing registry blob size"):
            transport.upload_blob_bytes(value, media_type="application/octet-stream")
        descriptor = OciDescriptor("application/json", selected_digest, len(value))
        with pytest.raises(ObjectInvalidRequest, match="metadata bound"):
            transport.get_blob_bytes(descriptor, maximum_bytes=1)
    finally:
        raw.close()

    responses = iter(
        [httpx.Response(200, content=b"short"), httpx.Response(200, content=b"PAYLOAD")]
    )
    transport, raw = _client(binding, lambda _: next(responses))
    try:
        descriptor = OciDescriptor("application/json", selected_digest, len(value))
        with pytest.raises(DigestMismatch, match="length"):
            transport.get_blob_bytes(descriptor, maximum_bytes=100)
        with pytest.raises(DigestMismatch, match="content"):
            transport.get_blob_bytes(descriptor, maximum_bytes=100)
    finally:
        raw.close()


def test_manifest_response_integrity_is_verified(binding: OciDistributionBinding) -> None:
    body = build_anchor_manifest(object_hash="a" * 64)
    responses = iter(
        [
            httpx.Response(200, content=b"not-json"),
            httpx.Response(
                200,
                headers={"Docker-Content-Digest": "sha256:" + "0" * 64},
                content=body,
            ),
            httpx.Response(200, headers={"Content-Type": "application/json"}, content=body),
            httpx.Response(
                201,
                headers={"Docker-Content-Digest": "sha256:" + "0" * 64},
            ),
        ]
    )
    transport, raw = _client(binding, lambda _: next(responses))
    try:
        with pytest.raises(DigestMismatch, match="invalid OCI manifest"):
            transport.get_manifest("tag")
        with pytest.raises(DigestMismatch, match="digest"):
            transport.get_manifest("tag")
        with pytest.raises(DigestMismatch, match="Content-Type"):
            transport.get_manifest("tag")
        with pytest.raises(DigestMismatch, match="stored a different manifest"):
            transport.put_manifest("tag", body)
    finally:
        raw.close()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(200, content=b"{"),
        httpx.Response(200, json={"tags": "invalid"}),
        httpx.Response(200, json={"tags": ["valid", 1]}),
    ],
)
def test_tag_listing_handles_absence_and_malformed_data(
    binding: OciDistributionBinding,
    response: httpx.Response,
) -> None:
    transport, raw = _client(binding, lambda _: response)
    try:
        if response.status_code == 404:
            assert transport.list_tags(limit=10).tags == ()
        else:
            with pytest.raises(ObjectUnavailable, match="tag response"):
                transport.list_tags(limit=10)
    finally:
        raw.close()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(200, content=b"{"),
        httpx.Response(200, json={"manifests": "invalid"}),
        httpx.Response(200, json={"manifests": ["invalid"]}),
        httpx.Response(200, json={"manifests": [{"mediaType": "bad"}]}),
    ],
)
def test_referrers_handles_absence_and_malformed_data(
    binding: OciDistributionBinding,
    response: httpx.Response,
) -> None:
    transport, raw = _client(binding, lambda _: response)
    try:
        if response.status_code == 404:
            assert transport.get_referrers("sha256:" + "0" * 64, artifact_type="test") == ()
        else:
            with pytest.raises(ObjectUnavailable, match="referrers"):
                transport.get_referrers("sha256:" + "0" * 64, artifact_type="test")
    finally:
        raw.close()


@pytest.mark.parametrize(
    "location",
    ["ftp://blob.test/upload", "https://user@blob.test/upload", "http://blob.test/upload"],
)
def test_upload_location_is_validated(
    binding: OciDistributionBinding,
    location: str,
) -> None:
    transport, raw = _client(
        binding,
        lambda _: httpx.Response(202, headers={"Location": location}),
    )
    try:
        with pytest.raises(ObjectUnavailable, match="upload Location"):
            transport.start_upload()
    finally:
        raw.close()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, text="not-json"),
        httpx.Response(400, json=[]),
        httpx.Response(400, json={"errors": []}),
        httpx.Response(400, json={"errors": [{"code": 7}]}),
    ],
)
def test_malformed_error_envelopes_use_safe_fallback(
    binding: OciDistributionBinding,
    response: httpx.Response,
) -> None:
    transport, raw = _client(binding, lambda _: response)
    try:
        with pytest.raises(ObjectInvalidRequest) as raised:
            transport.ping()
        assert raised.value.adapter_provenance["providerCode"] == "HTTP_ERROR"
    finally:
        raw.close()


def test_network_exhaustion_and_stream_connect_failure(binding: OciDistributionBinding) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    transport, raw = _client(binding, handler, attempts=2)
    try:
        with pytest.raises(ObjectUnavailable, match="request failed"):
            transport.ping()
        with (
            pytest.raises(ObjectUnavailable, match="request failed"),
            transport.open_blob("sha256:" + "0" * 64),
        ):
            pass
    finally:
        raw.close()


def test_upload_minimum_and_status_without_range(binding: OciDistributionBinding) -> None:
    responses = iter(
        [
            httpx.Response(
                202,
                headers={
                    "Location": "/upload",
                    "OCI-Chunk-Min-Length": "65536",
                },
            ),
            httpx.Response(204, headers={"Location": "/upload"}),
        ]
    )
    transport, raw = _client(binding, lambda _: next(responses))
    try:
        upload = transport.start_upload()
        assert upload.minimum_chunk_bytes == 65536
        assert transport.upload_status(upload).offset == 0
    finally:
        raw.close()
