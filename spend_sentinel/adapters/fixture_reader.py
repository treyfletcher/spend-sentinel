"""``FixtureAwsReader`` — an :class:`AwsReader` backed by fixture JSON (R9, R21).

Production test infrastructure: lets drift detection run entirely offline.
Fixture shape::

    {
      "instances":       {"i-0abc": {"instance_type": "t3.micro", "tags": {...}}},
      "security_groups": {"sg-0abc": {"ingress": [...], "egress": [...]}},
      "buckets":         {"my-bucket": {"tags": {...}, "versioning_enabled": true}},
      "errors":          {"i-0bad": "AuthFailure: not authorized"}
    }

A lookup id present in ``errors`` raises :class:`FixtureReaderError` with that
message (exercises the R12 error path); an id absent from its map returns
``None`` (exercises the R10 ``missing`` path).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from spend_sentinel.core.models import BucketAttrs, InstanceAttrs, SecurityGroupAttrs


class FixtureReaderError(Exception):
    """Raised for ids configured under the fixture's ``errors`` map."""


class _FixtureFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    instances: dict[str, InstanceAttrs] = Field(default_factory=dict)
    security_groups: dict[str, SecurityGroupAttrs] = Field(default_factory=dict)
    buckets: dict[str, BucketAttrs] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)


class FixtureAwsReader:
    """An offline AwsReader; satisfies the protocol structurally."""

    def __init__(self, data: dict[str, Any]) -> None:
        try:
            self._fixtures = _FixtureFile.model_validate(data)
        except ValidationError as exc:
            count = exc.error_count()
            raise ValueError(f"invalid AwsReader fixture data: {count} schema error(s)") from None

    @classmethod
    def from_path(cls, path: str | Path) -> FixtureAwsReader:
        """Load fixture JSON from a file."""
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("AwsReader fixture file is not a JSON object")
        return cls(raw)

    def _maybe_raise(self, lookup_id: str) -> None:
        message = self._fixtures.errors.get(lookup_id)
        if message is not None:
            raise FixtureReaderError(message)

    def get_instance(self, instance_id: str) -> InstanceAttrs | None:
        self._maybe_raise(instance_id)
        return self._fixtures.instances.get(instance_id)

    def get_security_group(self, sg_id: str) -> SecurityGroupAttrs | None:
        self._maybe_raise(sg_id)
        return self._fixtures.security_groups.get(sg_id)

    def get_bucket(self, name: str) -> BucketAttrs | None:
        self._maybe_raise(name)
        return self._fixtures.buckets.get(name)
