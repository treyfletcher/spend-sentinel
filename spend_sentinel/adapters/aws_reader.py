"""The ``AwsReader`` protocol and its attribute models (spec layout home).

The protocol is *defined* in :mod:`spend_sentinel.core.drift` and the attr
models in :mod:`spend_sentinel.core.models` so that ``core`` never imports
``adapters`` (spec Modularity rule); this module re-exports them under the
spec's ``adapters/aws_reader.py`` path, which adapter implementations and
production wiring import from.
"""

from __future__ import annotations

from spend_sentinel.core.drift import AwsReader
from spend_sentinel.core.models import (
    BucketAttrs,
    InstanceAttrs,
    SecurityGroupAttrs,
    SecurityGroupRule,
)

__all__ = [
    "AwsReader",
    "BucketAttrs",
    "InstanceAttrs",
    "SecurityGroupAttrs",
    "SecurityGroupRule",
]
