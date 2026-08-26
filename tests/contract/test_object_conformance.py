# SPDX-License-Identifier: Apache-2.0
"""Released Meridian Object Common conformance suite."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from meridian_storage import Operation
from meridian_storage.adapters.oci import OciDistributionAdapter
from meridian_storage.object_common import PayloadRegistry, run_object_conformance

from ..support.registry import FakeRegistry


@dataclass(slots=True)
class _Target:
    adapter: OciDistributionAdapter
    registry: FakeRegistry

    @property
    def target_id(self) -> str:
        return self.adapter.target_id

    def reset(self) -> None:
        self.registry.reset()

    def execute(
        self,
        operation: Operation,
        payloads: PayloadRegistry,
    ) -> Mapping[str, object]:
        return self.adapter.execute(operation, payloads)


def test_released_object_common_conformance(
    adapter: OciDistributionAdapter,
    registry: FakeRegistry,
) -> None:
    report = run_object_conformance(_Target(adapter, registry))

    report.require_success()
    assert report.passed
    assert len(report.checks) == 9
    assert report.fingerprint.startswith("sha256:")
