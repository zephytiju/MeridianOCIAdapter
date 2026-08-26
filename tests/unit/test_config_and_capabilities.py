# SPDX-License-Identifier: Apache-2.0
"""Binding validation, credential secrecy, and negotiated capabilities."""

from __future__ import annotations

import base64

import pytest

from meridian_storage.adapters.oci import (
    AnonymousCredentials,
    BasicCredentials,
    BearerCredentials,
    CallbackCredentials,
    OciDistributionBinding,
    configured_capability_manifest,
    oci_distribution_descriptor,
)


def test_credentials_are_explicit_and_secret_safe() -> None:
    basic = BasicCredentials("meridian", "secret")
    bearer = BearerCredentials("opaque-token")
    callback = CallbackCredentials(lambda: "Bearer refreshed")

    assert basic.authorization_header() == "Basic " + base64.b64encode(b"meridian:secret").decode()
    assert bearer.authorization_header() == "Bearer opaque-token"
    assert callback.authorization_header() == "Bearer refreshed"
    assert AnonymousCredentials().authorization_header() is None
    assert "secret" not in repr(basic)
    assert "opaque-token" not in repr(bearer)
    assert "refreshed" not in repr(callback)


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: BasicCredentials("", "secret"), "username"),
        (lambda: BasicCredentials("user\nname", "secret"), "username"),
        (lambda: BasicCredentials("user", ""), "password"),
        (lambda: BearerCredentials("bad\rvalue"), "token"),
        (lambda: CallbackCredentials(lambda: "").authorization_header(), "Authorization"),
        (lambda: CallbackCredentials(lambda: 7).authorization_header(), "Authorization"),  # type: ignore[arg-type,return-value]
    ],
)
def test_credentials_reject_unsafe_values(factory: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()  # type: ignore[operator]


def test_binding_normalizes_resource_endpoint_and_public_fingerprint() -> None:
    binding = OciDistributionBinding(
        resource="object:assets.releases",
        endpoint="https://registry.example.com/",
        repository="org/release_assets",
    )

    assert binding.endpoint == "https://registry.example.com"
    assert binding.resource_ref.canonical == "object:assets.releases"
    assert binding.origin == ("https", "registry.example.com", None)
    assert binding.public_fingerprint.startswith("sha256:")
    assert "registry.example.com" not in binding.public_fingerprint
    assert len(binding.cursor_signing_key or b"") == 32
    second = OciDistributionBinding(
        resource="object:assets.releases",
        endpoint="https://registry.example.com/",
        repository="org/release_assets",
    )
    assert binding.cursor_signing_key != second.cursor_signing_key
    assert binding.public_fingerprint == second.public_fingerprint


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"endpoint": "http://registry.test"}, "HTTPS"),
        ({"endpoint": "https://user@registry.test"}, "userinfo"),
        ({"endpoint": "https://registry.test/v2"}, "path"),
        ({"endpoint": "https://registry.test?x=1"}, "query"),
        ({"endpoint": "https://registry.test:invalid"}, "invalid port"),
        ({"endpoint": 7}, "endpoint must be a string"),
        ({"repository": "Upper/Case"}, "name grammar"),
        ({"repository": ".invalid"}, "name grammar"),
        ({"conditional_create_mode": "unsafe"}, "conditional_create_mode"),
        ({"chunk_size": 1024}, "chunk_size"),
        ({"max_list_page_size": 1001}, "max_list_page_size"),
        ({"max_scan_tags": 10}, "max_scan_tags"),
        ({"max_multipart_parts": 0}, "max_multipart_parts"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"deletion_enabled": 1}, "deletion_enabled"),
        ({"verify_tls": ""}, "verify_tls"),
        ({"credentials": object()}, "credentials"),
        ({"cursor_signing_key": b"short"}, "cursor_signing_key"),
    ],
)
def test_binding_rejects_invalid_configuration(
    changes: dict[str, object],
    error: str,
) -> None:
    values: dict[str, object] = {
        "resource": "object:assets.releases",
        "endpoint": "https://registry.test",
        "repository": "meridian/test",
    }
    values.update(changes)
    with pytest.raises((TypeError, ValueError), match=error):
        OciDistributionBinding(**values)  # type: ignore[arg-type]


def test_insecure_http_requires_explicit_opt_in() -> None:
    binding = OciDistributionBinding(
        resource="object:assets.releases",
        endpoint="http://127.0.0.1:5000",
        repository="meridian/test",
        allow_insecure_http=True,
    )

    assert binding.endpoint == "http://127.0.0.1:5000"
    assert binding.origin == ("http", "127.0.0.1", 5000)


def test_capabilities_are_binding_specific() -> None:
    base = OciDistributionBinding(
        resource="object:assets.releases",
        endpoint="https://registry.test",
        repository="meridian/test",
    )
    managed = OciDistributionBinding(
        resource="object:assets.releases",
        endpoint="https://registry.test",
        repository="meridian/test",
        deletion_enabled=True,
        retention_enforced=True,
        referrers_required=True,
    )

    descriptor = oci_distribution_descriptor(managed)
    configured = configured_capability_manifest(managed)
    restricted = configured_capability_manifest(base)

    assert descriptor.adapter_id == "oci-distribution"
    assert descriptor.driver == "httpx"
    assert len(descriptor.capabilities) == 6
    assert "meridian.object.delete" in configured.available_operation_contracts
    assert "meridian.object.delete" not in restricted.available_operation_contracts
    assert configured.extensions["referrersRequired"] is True
    put = next(
        capability
        for capability in descriptor.capabilities
        if capability.operation_contract == "meridian.object.put"
    )
    assert "object.retention-enforcement" in put.guarantees
    assert put.extensions["design.objectLldRevision"] == 12
