# SPDX-License-Identifier: Apache-2.0
"""Health probes, logical migration, and retention intent enforcement."""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

import httpx
import pytest

from meridian_storage.adapters.oci import (
    OciDistributionAdapter,
    OciDistributionBinding,
    OciDistributionProbe,
    OciLogicalMigration,
)
from meridian_storage.adapters.oci.probe import _delete_safely
from meridian_storage.adapters.oci.retention import require_delete_permitted
from meridian_storage.adapters.oci.transport import OciDescriptor, RegistryHttpClient
from meridian_storage.object_common import (
    ObjectInvalidRequest,
    ObjectReference,
    ObjectUnavailable,
    PayloadReference,
    PayloadRegistry,
    RetentionDenied,
    RetentionRequest,
    transfer_payload,
)

from ..support.operations import put_operation
from ..support.registry import FakeRegistry


def _adapter_for(
    registry: FakeRegistry,
    *,
    resource: str,
    endpoint: str,
    repository: str,
    deletion_enabled: bool = True,
    referrers_required: bool = False,
) -> tuple[OciDistributionAdapter, httpx.Client]:
    binding = OciDistributionBinding(
        resource=resource,
        endpoint=endpoint,
        repository=repository,
        deletion_enabled=deletion_enabled,
        referrers_required=referrers_required,
        cursor_signing_key=b"migration-probe-test-key",
    )
    client = httpx.Client(transport=httpx.MockTransport(registry.handle))
    transport = RegistryHttpClient(binding, client=client, sleep=lambda _: None)
    return (
        OciDistributionAdapter(
            binding,
            transport=transport,
            clock=lambda: datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
        ),
        client,
    )


def test_authenticated_and_deep_probe_with_cleanup(
    transport: RegistryHttpClient,
    registry: FakeRegistry,
) -> None:
    probe = OciDistributionProbe(transport)

    authenticated = probe.authenticated()
    deep = probe.deep()

    assert authenticated.passed
    assert authenticated.api_version == "registry/2.0"
    assert deep.passed
    assert deep.push_pull
    assert deep.range_read
    assert deep.referrers
    assert deep.deletion
    assert deep.to_dict()["formatVersion"] == "meridian.oci-probe.v1"
    assert registry.manifests == {}
    assert registry.tags == {}


def test_probe_reports_failures_without_leaking_details(
    binding: OciDistributionBinding,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="private provider detail")

    raw = httpx.Client(transport=httpx.MockTransport(handler))
    transport = RegistryHttpClient(binding, client=raw, max_read_attempts=1)
    try:
        report = OciDistributionProbe(transport).authenticated()
        assert not report.passed
        assert report.failure == "ObjectUnavailable"
        assert "private provider detail" not in str(report.to_dict())
    finally:
        raw.close()


def test_authenticated_probe_checks_repository_access(
    binding: OciDistributionBinding,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/":
            return httpx.Response(200, json={})
        return httpx.Response(
            401,
            json={"errors": [{"code": "UNAUTHORIZED", "message": "private"}]},
        )

    raw = httpx.Client(transport=httpx.MockTransport(handler))
    transport = RegistryHttpClient(binding, client=raw, max_read_attempts=1)
    try:
        report = OciDistributionProbe(transport).authenticated()
        assert not report.passed
        assert report.failure == "ObjectAuthenticationFailed"
    finally:
        raw.close()


def test_deep_probe_can_require_referrers(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = FakeRegistry()
    adapter, raw = _adapter_for(
        registry,
        resource="object:probe.objects",
        endpoint="https://registry.test",
        repository="meridian/test",
        referrers_required=True,
    )
    monkeypatch.setattr(adapter.transport, "get_referrers", lambda *_args, **_kwargs: ())
    try:
        report = OciDistributionProbe(adapter.transport).deep()
        assert not report.passed
        assert report.failure == "ValueError"
        assert registry.manifests == {}
    finally:
        raw.close()


def test_deep_probe_without_delete_is_non_destructive() -> None:
    registry = FakeRegistry(repository="meridian/probe")
    adapter, raw = _adapter_for(
        registry,
        resource="object:probe.objects",
        endpoint="https://registry.test",
        repository="meridian/probe",
        deletion_enabled=False,
    )
    try:
        report = OciDistributionProbe(adapter.transport).deep()
        assert report.passed
        assert not report.deletion
        assert registry.manifests
    finally:
        raw.close()


def test_logical_export_import_moves_portable_object(
    adapter: OciDistributionAdapter,
    provider: object,
    payloads: PayloadRegistry,
) -> None:
    source_result = adapter.execute(
        put_operation(
            provider,  # type: ignore[arg-type]
            payloads,
            data=b"portable",
            object_id="migration/object",
            user_metadata={"channel": "stable"},
            provenance={"producer": "unit-test"},
        ),
        payloads,
    )
    source_metadata = source_result["metadata"]
    assert isinstance(source_metadata, dict)
    source_reference = source_metadata["objectRef"]
    assert isinstance(source_reference, dict)
    exported = OciLogicalMigration(adapter).export_logical(source_reference, payloads)

    target_registry = FakeRegistry(repository="meridian/target")
    target, raw = _adapter_for(
        target_registry,
        resource="object:target.objects",
        endpoint="https://target.test",
        repository="meridian/target",
    )
    try:
        imported = OciLogicalMigration(target).import_logical(exported, payloads)
        metadata = imported["metadata"]
        assert isinstance(metadata, dict)
        assert metadata["digest"] == source_metadata["digest"]
        assert metadata["userMetadata"] == {"channel": "stable"}
        assert metadata["objectRef"]["resourceRef"]["namespace"] == "target"  # type: ignore[index]
        assert exported.to_dict()["formatVersion"] == "meridian.object-logical-export.v1"

        fetched = target.execute(
            provider.normalize(  # type: ignore[attr-defined]
                provider.create_surface().get(  # type: ignore[attr-defined]
                    resource="object:target.objects",
                    reference=metadata["objectRef"],
                )
            ),
            payloads,
        )
        sink = BytesIO()
        transfer_payload(PayloadReference.from_mapping(fetched["payload"]), payloads, sink)  # type: ignore[arg-type]
        assert sink.getvalue() == b"portable"
    finally:
        raw.close()


def test_logical_export_validates_reference_binding(
    adapter: OciDistributionAdapter,
    payloads: PayloadRegistry,
) -> None:
    migration = OciLogicalMigration(adapter)
    with pytest.raises(ObjectInvalidRequest, match="invalid migration"):
        migration.export_logical({"objectId": "missing"}, payloads)
    wrong = ObjectReference(
        adapter._semantic_resource().__class__.parse("object:other.objects"),
        "missing",
        None,
    )
    with pytest.raises(ObjectInvalidRequest, match="binding"):
        migration.export_logical(wrong, payloads)


def test_retention_intent_enforcement() -> None:
    now = datetime(2026, 8, 26, tzinfo=UTC)
    require_delete_permitted(None, now=now)
    require_delete_permitted(
        RetentionRequest(datetime(2026, 1, 1, tzinfo=UTC), "expired"),
        now=now,
    )
    with pytest.raises(RetentionDenied):
        require_delete_permitted(
            RetentionRequest(datetime(2027, 1, 1, tzinfo=UTC), "future"),
            now=now,
        )
    with pytest.raises(RetentionDenied, match="external release"):
        require_delete_permitted(RetentionRequest(None, "legal-hold"), now=now)
    with pytest.raises(ValueError, match="timezone-aware"):
        require_delete_permitted(RetentionRequest(None, "hold"), now=datetime(2026, 8, 26))


def test_probe_cleanup_is_best_effort(
    transport: RegistryHttpClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = OciDescriptor("application/json", "sha256:" + "0" * 64, 1)

    def unavailable(_: str) -> None:
        raise ObjectUnavailable()

    monkeypatch.setattr(transport, "delete_manifest", unavailable)
    _delete_safely(transport, descriptor)
    _delete_safely(transport, None)
