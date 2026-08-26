# SPDX-License-Identifier: Apache-2.0
"""Portable retention intent checks without WORM certification claims."""

from __future__ import annotations

from datetime import UTC, datetime

from meridian_storage.object_common import RetentionDenied, RetentionRequest


def require_delete_permitted(
    retention: RetentionRequest | None,
    *,
    now: datetime | None = None,
) -> None:
    if retention is None:
        return
    selected = datetime.now(UTC) if now is None else now
    if selected.tzinfo is None or selected.utcoffset() is None:
        raise ValueError("retention evaluation time must be timezone-aware")
    if retention.retain_until is None:
        raise RetentionDenied("logical retention policy requires an external release decision")
    retention.require_delete_allowed(now=selected)


__all__ = ["require_delete_permitted"]
