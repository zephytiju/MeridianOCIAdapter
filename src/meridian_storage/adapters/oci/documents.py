# SPDX-License-Identifier: Apache-2.0
"""Load packaged compatibility evidence without consulting sibling source."""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib import resources
from typing import cast


def compatibility_document() -> Mapping[str, object]:
    value = resources.files("meridian_storage.adapters.oci").joinpath("compatibility.json")
    return cast(Mapping[str, object], json.loads(value.read_text(encoding="utf-8")))


__all__ = ["compatibility_document"]
