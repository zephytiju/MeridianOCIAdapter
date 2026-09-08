# SPDX-License-Identifier: Apache-2.0
"""Conformance against a genuine OCI Distribution endpoint."""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import pytest

from meridian_storage import Operation
from meridian_storage.adapters.oci import (
    AnonymousCredentials,
    BasicCredentials,
    BearerCredentials,
    OciDistributionAdapter,
    OciDistributionBinding,
    OciDistributionProbe,
)
from meridian_storage.object_common import (
    ObjectConformanceTarget,
    ObjectNotFound,
    PayloadRegistry,
    run_object_conformance,
)


@dataclass(slots=True)
class _Target(ObjectConformanceTarget):
    adapter: OciDistributionAdapter

    @property
    def target_id(self) -> str:
        return self.adapter.target_id

    def reset(self) -> None:
        if self.adapter.transport.list_tags(limit=1).tags:
            raise AssertionError("per-run OCI conformance repository was not empty")

    def execute(
        self,
        operation: Operation,
        payloads: PayloadRegistry,
    ) -> Mapping[str, object]:
        return self.adapter.execute(operation, payloads)


def _adapter(prefix: str) -> OciDistributionAdapter:
    endpoint = os.getenv(f"{prefix}_ENDPOINT")
    repository = os.getenv(f"{prefix}_REPOSITORY")
    if not endpoint or not repository:
        pytest.skip(f"{prefix}_ENDPOINT and {prefix}_REPOSITORY are required")
    username = os.getenv(f"{prefix}_USERNAME")
    password = os.getenv(f"{prefix}_PASSWORD")
    token = os.getenv(f"{prefix}_TOKEN")
    if token:
        credentials = BearerCredentials(token)
    elif username and password:
        credentials = BasicCredentials(username, password)
    else:
        credentials = AnonymousCredentials()
    repository = f"{repository.rstrip('/')}/meridian-conformance-{secrets.token_hex(8)}"
    return OciDistributionAdapter(
        OciDistributionBinding(
            resource="object:conformance.objects",
            endpoint=endpoint,
            repository=repository,
            credentials=credentials,
            allow_insecure_http=urlsplit(endpoint).scheme == "http",
            verify_tls=os.getenv(f"{prefix}_VERIFY_TLS", "true").lower() != "false",
            deletion_enabled=True,
            registry_release=os.getenv(f"{prefix}_RELEASE"),
            registry_image=os.getenv(f"{prefix}_IMAGE"),
            referrers_required=os.getenv(f"{prefix}_REQUIRE_REFERRERS", "false").lower() == "true",
        )
    )


@pytest.mark.integration
def test_distribution_reference_registry_conformance() -> None:
    with _adapter("MERIDIAN_OCI_TEST") as adapter:
        try:
            probe = OciDistributionProbe(adapter.transport).deep()
            assert probe.passed, probe.to_dict()
            report = run_object_conformance(_Target(adapter))
            report.require_success()
        finally:
            _cleanup_unique_repository(adapter)


@pytest.mark.integration
@pytest.mark.owner_service
def test_owner_managed_registry_gate() -> None:
    with _adapter("MERIDIAN_OCI_OWNER") as adapter:
        try:
            report = OciDistributionProbe(adapter.transport).deep()
            assert report.passed, report.to_dict()
        finally:
            _cleanup_unique_repository(adapter)


def _cleanup_unique_repository(adapter: OciDistributionAdapter) -> None:
    """Remove manifests only from the random repository created by this test run."""

    for _ in range(100):
        page = adapter.transport.list_tags(limit=100)
        if not page.tags:
            return
        for tag in page.tags:
            try:
                document = adapter.transport.get_manifest(tag)
                adapter.transport.delete_manifest(document.digest)
            except ObjectNotFound:
                continue
    raise AssertionError("unique OCI conformance repository cleanup did not converge")


@pytest.mark.integration
def test_required_referrers_rejects_reference_registry() -> None:
    """These exact reference images have no native referrers API; fail closed."""
    from dataclasses import replace

    with (
        _adapter("MERIDIAN_OCI_TEST") as original,
        OciDistributionAdapter(replace(original.binding, referrers_required=True)) as adapter,
    ):
        try:
            report = OciDistributionProbe(adapter.transport).deep()
            assert not report.passed, report.to_dict()
            assert report.push_pull
            assert report.range_read
            assert not report.referrers
            assert report.failure == "ValueError"
        finally:
            _cleanup_unique_repository(adapter)
