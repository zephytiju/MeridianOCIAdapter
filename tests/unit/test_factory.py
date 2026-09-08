# SPDX-License-Identifier: Apache-2.0
"""Released Core SPI discovery, lifetime and private binding regressions."""

from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from meridian_storage import OperationContext, ResourceRef
from meridian_storage.adapters.oci._factory import OciAdapterFactory
from meridian_storage.object_common import (
    ObjectCapabilityMismatch,
    ObjectInvalidRequest,
    ObjectUnavailable,
    default_payload_registry,
)
from meridian_storage.runtime.config import SecretReference, TLSPolicy
from meridian_storage.spi import (
    AdapterFactory,
    AdapterRuntime,
    ExecutionRequest,
    PhysicalResource,
    SecretValue,
)
from meridian_storage.spi.discovery import ADAPTER_ENTRY_POINT_GROUP, discover_components

from ..support.core import RESOURCE, binding_config, core_runtime, create_context
from ..support.operations import put_operation
from ..support.registry import FakeRegistry

PHYSICAL = PhysicalResource(RESOURCE, "sha256:" + "0" * 64, None, "object")


@pytest.fixture
def clients(monkeypatch):
    created = []
    client_class = httpx.Client
    registry = FakeRegistry()

    def client(**kwargs):
        value = client_class(transport=httpx.MockTransport(registry.handle))
        created.append(value)
        return value

    monkeypatch.setattr(httpx, "Client", client)
    return created


def test_normal_discovery_is_lazy_and_implements_factory(clients):
    discovery = discover_components(
        ADAPTER_ENTRY_POINT_GROUP, (), component_id=lambda a: a.adapter_id
    )
    factory = discovery.components["oci-distribution"]
    assert isinstance(factory, AdapterFactory)
    runtime = factory.create(create_context())
    assert isinstance(runtime, AdapterRuntime)
    assert clients == []
    runtime.close()


def test_normal_core_startup_and_shutdown(clients):
    runtime = core_runtime(binding_config())
    runtime.start()
    assert len(clients) == 1
    runtime.close()
    runtime.close()
    assert clients[0].is_closed


def test_runtime_sessions_execution_and_physical_verification(clients, provider):
    runtime = OciAdapterFactory().create(create_context())
    for call in (
        runtime.probe,
        lambda: runtime.verify_physical((PHYSICAL,)),
        lambda: runtime.open_session(transactional=False),
    ):
        with pytest.raises(ObjectInvalidRequest, match="not open"):
            call()
    runtime.open()
    runtime.open()
    assert len(clients) == 1
    assert runtime.probe() == runtime.probe()
    with pytest.raises(ObjectInvalidRequest, match="not been verified"):
        runtime.open_session(transactional=False)
    assert runtime.verify_physical((PHYSICAL,)) == runtime.verify_physical((PHYSICAL,))
    first = runtime.verify_physical((PHYSICAL,)).fingerprint
    assert (
        runtime.verify_physical(
            (replace(PHYSICAL, resource_fingerprint="sha256:" + "1" * 64),)
        ).fingerprint
        != first
    )
    with pytest.raises(ObjectCapabilityMismatch):
        runtime.open_session(transactional=True)
    session = runtime.open_session(transactional=False)
    for call in (session.begin, session.commit, session.rollback):
        with pytest.raises(ObjectCapabilityMismatch):
            call()
    with pytest.raises(TypeError, match="ExecutionRequest"):
        session.execute(None)
    payloads = default_payload_registry()
    operation = put_operation(provider, payloads, resource=RESOURCE.canonical)
    request = ExecutionRequest(
        operation,
        OperationContext("test:factory"),
        "request",
        "execution",
        "objects",
        1,
        "sha256:" + "0" * 64,
        1,
    )
    result = session.execute(request)
    assert result.data["metadata"]["byteLength"] == 7
    assert result.provenance["adapterId"] == "oci-distribution"
    assert "registry.test" not in str(result)
    session.close()
    session.close()
    with pytest.raises(ObjectInvalidRequest, match="session is closed"):
        session.execute(request)
    active = runtime.open_session(transactional=False)
    runtime.close()
    with pytest.raises(ObjectInvalidRequest, match="not open"):
        active.execute(request)
    payloads.release(operation.input["payload"]["token"])
    assert clients[0].is_closed


@pytest.mark.parametrize(
    "resources",
    [
        (),
        (PHYSICAL, PHYSICAL),
        (replace(PHYSICAL, resource_ref=ResourceRef("object", "other", "objects")),),
    ],
)
def test_physical_resource_boundary(clients, resources):
    runtime = OciAdapterFactory().create(create_context())
    try:
        runtime.open()
        with pytest.raises(ObjectInvalidRequest, match="single configured"):
            runtime.verify_physical(resources)
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"resource": RESOURCE.canonical, "unknown": True},
        {"resource": RESOURCE.canonical, "authMode": "unknown"},
        {"resource": RESOURCE.canonical, "maxObjectBytes": False},
    ],
)
def test_rejects_invalid_private_settings(settings):
    with pytest.raises(ObjectInvalidRequest):
        OciAdapterFactory().create(create_context(replace(binding_config(), settings=settings)))


@pytest.mark.parametrize(
    ("auth", "expected"), [("anonymous", None), ("basic", "Basic "), ("bearer", "Bearer ")]
)
def test_opaque_secret_bridge(auth, expected):
    binding = binding_config()
    runtime = OciAdapterFactory().create(
        create_context(replace(binding, settings=dict(binding.settings, authMode=auth)))
    )
    header = runtime._binding.credentials.authorization_header()
    assert header is None if expected is None else header.startswith(expected)
    assert "test-password" not in repr(runtime._binding)


def test_default_basic_and_invalid_secret_are_redacted():
    binding = binding_config()
    settings = dict(binding.settings)
    del settings["authMode"]
    context = create_context(replace(binding, settings=settings))
    assert (
        OciAdapterFactory()
        .create(context)
        ._binding.credentials.authorization_header()
        .startswith("Basic ")
    )
    with pytest.raises(ObjectInvalidRequest, match="configuration is invalid") as error:
        OciAdapterFactory().create(replace(context, credential=SecretValue(b"\xffsecret")))
    assert error.value.__suppress_context__
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"adapter_id": "other"},
        {"endpoint": None},
        {"tls": TLSPolicy("disabled", None, None, None)},
        {"endpoint": "http://registry.test"},
        {
            "tls": TLSPolicy(
                "mutual",
                "registry.test",
                SecretReference("test", "ca"),
                SecretReference("test", "client"),
            )
        },
        {"tls": TLSPolicy("server", "registry.test", SecretReference("test", "ca"), None)},
    ],
)
def test_rejects_invalid_context(changes):
    with pytest.raises(ObjectInvalidRequest):
        OciAdapterFactory().create(
            replace(create_context(replace(binding_config(), **changes)), tls_ca=None)
        )
    with pytest.raises(TypeError, match="AdapterCreateContext"):
        OciAdapterFactory().create(None)


def test_ca_material_is_private_and_cleaned_on_close(clients):
    binding = replace(
        binding_config(),
        tls=TLSPolicy("server", "registry.test", SecretReference("test", "ca"), None),
    )
    runtime = OciAdapterFactory().create(
        replace(create_context(binding), tls_ca=SecretValue(b"test-ca"))
    )
    runtime.open()
    path = Path(runtime._adapter.binding.verify_tls)
    assert path.read_bytes() == b"test-ca"
    assert path.stat().st_mode & 0o777 == 0o600
    runtime.close()
    assert not path.exists()


def test_failed_probe_closes_transport_and_ca(monkeypatch, clients):
    from meridian_storage.adapters.oci.probe import OciDistributionProbe

    def failed(self):
        report = original(self)
        return replace(report, passed=False)

    original = OciDistributionProbe.authenticated
    monkeypatch.setattr(OciDistributionProbe, "authenticated", failed)
    runtime = OciAdapterFactory().create(replace(create_context(), tls_ca=SecretValue(b"test-ca")))
    with pytest.raises(ObjectUnavailable, match="startup probe failed"):
        runtime.open()
    assert clients[0].is_closed
    assert runtime._ca_directory is None
    with pytest.raises(ObjectInvalidRequest, match="not open"):
        runtime.probe()


def test_construction_failure_cleans_ca(monkeypatch):
    def failed(**kwargs):
        raise RuntimeError("client creation failed")

    monkeypatch.setattr(httpx, "Client", failed)
    runtime = OciAdapterFactory().create(replace(create_context(), tls_ca=SecretValue(b"test-ca")))
    with pytest.raises(RuntimeError, match="creation failed"):
        runtime.open()
    assert runtime._ca_directory is None


def test_selected_provenance_startup_and_expected_fingerprint_drift(clients):
    binding = binding_config(
        registry_release="unlisted", registry_image="registry@sha256:" + "a" * 64
    )
    runtime = core_runtime(binding)
    runtime.start()
    runtime.close()
    changed = replace(binding, settings=dict(binding.settings, registryRelease="another"))
    runtime = core_runtime(changed)
    with pytest.raises(Exception, match="[Cc]apability|[Ff]ingerprint"):
        runtime.start()
    runtime.close()
