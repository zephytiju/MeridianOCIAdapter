# SPDX-License-Identifier: Apache-2.0
"""Shared deterministic fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest

from meridian_storage.adapters.oci import OciDistributionAdapter, OciDistributionBinding
from meridian_storage.adapters.oci.transport import RegistryHttpClient
from meridian_storage.object_common import ObjectCatalogProvider, PayloadRegistry

from .support.registry import FakeRegistry


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


@pytest.fixture
def binding() -> OciDistributionBinding:
    return OciDistributionBinding(
        resource="object:conformance.objects",
        endpoint="https://registry.test",
        repository="meridian/test",
        deletion_enabled=True,
        retention_enforced=True,
        cursor_signing_key=b"deterministic-test-cursor-key",
    )


@pytest.fixture
def transport(
    binding: OciDistributionBinding,
    registry: FakeRegistry,
) -> Iterator[RegistryHttpClient]:
    client = httpx.Client(transport=httpx.MockTransport(registry.handle))
    yield RegistryHttpClient(binding, client=client, sleep=lambda _: None)
    client.close()


@pytest.fixture
def adapter(
    binding: OciDistributionBinding,
    transport: RegistryHttpClient,
) -> OciDistributionAdapter:
    return OciDistributionAdapter(
        binding,
        transport=transport,
        clock=lambda: datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
    )


@pytest.fixture
def provider() -> ObjectCatalogProvider:
    return ObjectCatalogProvider()


@pytest.fixture
def payloads() -> PayloadRegistry:
    return PayloadRegistry()
