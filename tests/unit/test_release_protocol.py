# SPDX-License-Identifier: Apache-2.0
"""Release provenance never substitutes for protocol or physical conformance."""

import json
from dataclasses import replace

import httpx
import pytest

from meridian_storage.adapters.oci import (
    OciDistributionBinding,
    OciDistributionProbe,
    configured_capability_manifest,
)
from meridian_storage.adapters.oci._factory import OciAdapterFactory
from meridian_storage.adapters.oci.transport import RegistryHttpClient
from meridian_storage.object_common import ObjectCapabilityMismatch
from meridian_storage.runtime.config import BindingConfig

from ..support.core import binding_config, create_context
from ..support.registry import FakeRegistry

IMAGE = "registry.example/registry:unlisted@sha256:" + "a" * 64


@pytest.mark.parametrize("release", ["3.0.0", "3.1.1", "unlisted-registry-release"])
def test_selected_release_is_separate_from_protocol_and_observation(release):
    binding = OciDistributionBinding(
        resource="object:resources.objects",
        endpoint="https://registry.test",
        repository="meridian/test",
        registry_release=release,
        registry_image=IMAGE,
        deletion_enabled=True,
        referrers_required=True,
    )
    manifest = configured_capability_manifest(binding)
    assert manifest.engine_version == "1.1.1"
    assert manifest.extensions["selectedRegistry"] == {"release": release, "image": IMAGE}
    assert (
        manifest.fingerprint
        != configured_capability_manifest(
            replace(binding, registry_release=None, registry_image=None)
        ).fingerprint
    )
    assert manifest.fingerprint == configured_capability_manifest(replace(binding)).fingerprint
    with httpx.Client(transport=httpx.MockTransport(FakeRegistry().handle)) as client:
        transport = RegistryHttpClient(binding, client=client)
        for report in (
            OciDistributionProbe(transport).authenticated(),
            OciDistributionProbe(transport).deep(),
        ):
            assert report.passed
            value = json.loads(json.dumps(report.to_dict()))
            assert value["apiVersion"] == "registry/2.0"
            assert value["registryProvenance"]["observedRelease"] is None
            assert value["registryProvenance"]["selected"]["release"] == release
            assert report.capability_manifest.fingerprint == manifest.fingerprint


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"engine_version": "3.1.1"}, "Distribution specification"),
        ({"engine_version": "9.9.9"}, "Distribution specification"),
        ({"engine_profile": "other"}, "engine profile"),
        ({"adapter_contract": "2.0.0"}, "SPI contract"),
    ],
)
def test_factory_retains_real_contract_checks(changes, message):
    with pytest.raises(ObjectCapabilityMismatch, match=message):
        OciAdapterFactory().create(create_context(replace(binding_config(), **changes)))


def test_factory_selected_provenance_roundtrip():
    original = binding_config()
    selected = replace(
        original, settings=dict(original.settings, registryRelease="unlisted", registryImage=IMAGE)
    )
    wire = json.loads(json.dumps(selected.to_dict()))
    restored = BindingConfig.from_mapping(wire, "binding")
    assert restored.to_dict() == selected.to_dict()
    runtime = OciAdapterFactory().create(create_context(restored))
    assert runtime._binding.registry_release == "unlisted"
    assert runtime._binding.registry_image == IMAGE


@pytest.mark.parametrize(
    "changes",
    [
        {"registry_release": ""},
        {"registry_release": " "},
        {"registry_release": "x\ny"},
        {"registry_release": 123},
        {"registry_release": "x" * 129},
        {"registry_image": "registry:latest"},
        {"registry_image": "https://registry@sha256:" + "a" * 64},
        {"registry_image": "r" * 513 + "@sha256:" + "a" * 64},
        {"registry_image": 123},
        {"registry_image": "https://user:password@registry@sha256:" + "a" * 64},
    ],
)
def test_provenance_shape_and_image_integrity_remain_checked(changes):
    with pytest.raises(ValueError, match="registry_"):
        OciDistributionBinding(
            resource="object:resources.objects",
            endpoint="https://registry.test",
            repository="test",
            **changes,
        )


@pytest.mark.parametrize("fault", ["api", "media", "referrers", "range", "delete", "auth", "tls"])
def test_protocol_failures_are_not_rescued_by_release_metadata(fault):
    registry = FakeRegistry()
    binding = OciDistributionBinding(
        resource="object:resources.objects",
        endpoint="https://registry.test",
        repository="meridian/test",
        registry_release="3.1.1",
        registry_image=IMAGE,
        deletion_enabled=True,
        referrers_required=True,
    )

    def handler(request):
        path = request.url.path
        if fault == "tls":
            raise httpx.ConnectError("certificate verify failed", request=request)
        if fault == "auth":
            return httpx.Response(401)
        if (
            (fault == "api" and path == "/v2/")
            or (fault == "media" and request.method == "PUT" and "/manifests/" in path)
            or (fault == "referrers" and "/referrers/" in path)
            or (fault == "delete" and request.method == "DELETE")
        ):
            return httpx.Response(405)
        response = registry.handle(request)
        if fault == "range" and "Range" in request.headers:
            return httpx.Response(206, headers=response.headers, content=b"bad")
        return response

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = OciDistributionProbe(
            RegistryHttpClient(binding, client=client, max_read_attempts=1)
        ).deep()
        assert not report.passed
        assert report.to_dict()["registryProvenance"]["observedRelease"] is None


def test_optional_docker_header_is_not_required_or_a_software_version():
    registry = FakeRegistry()

    def handler(request):
        response = registry.handle(request)
        if request.url.path == "/v2/":
            response.headers.pop("Docker-Distribution-Api-Version")
        return response

    binding = OciDistributionBinding(
        resource="object:resources.objects",
        endpoint="https://registry.test",
        repository="meridian/test",
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = OciDistributionProbe(RegistryHttpClient(binding, client=client)).authenticated()
        assert report.passed
        assert report.api_version == "unknown"


def test_legacy_protocol_manifest_and_fingerprint_are_unchanged():
    from pathlib import Path

    fixture = Path(__file__).parents[2] / "contracts/fixtures/oci-1.0.3-legacy-capabilities.json"
    golden = json.loads(fixture.read_text())
    binding = OciDistributionBinding(
        resource="object:resources.objects",
        endpoint="https://registry.test",
        repository="meridian/test",
        deletion_enabled=True,
    )
    manifest = configured_capability_manifest(binding)
    assert manifest.to_dict() == golden["manifest"]
    assert manifest.fingerprint == golden["fingerprint"]
