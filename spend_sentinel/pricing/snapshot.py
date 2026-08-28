"""``SnapshotPricingSource`` — pricing from the bundled snapshot data file.

The snapshot (``spend_sentinel/data/pricing_snapshot.json``) is a hand-curated,
versioned data file shipped with the package (R4); its ``meta`` block records
version, snapshot date, and sources. Rates are stored as strings and converted
to :class:`~decimal.Decimal` exactly once at load time (R5).
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from importlib import resources
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

_SNAPSHOT_PACKAGE = "spend_sentinel"
_SNAPSHOT_RESOURCE = "data/pricing_snapshot.json"


class SnapshotMeta(BaseModel):
    """The snapshot's provenance block."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    version: str
    snapshot_date: str
    sources: list[str]


class _SnapshotFile(BaseModel):
    """On-disk shape of the bundled snapshot."""

    model_config = ConfigDict(extra="ignore")

    meta: SnapshotMeta
    regions: dict[str, dict[str, dict[str, str]]]


class SnapshotError(Exception):
    """The bundled pricing snapshot is missing or malformed (packaging bug)."""


class SnapshotPricingSource:
    """A :class:`~spend_sentinel.pricing.source.PricingSource` backed by the snapshot."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        """Load the bundled snapshot, or use ``data`` (same shape) if given."""
        if data is None:
            data = _read_bundled_snapshot()
        try:
            parsed = _SnapshotFile.model_validate(data)
        except ValidationError as exc:
            count = exc.error_count()
            raise SnapshotError(f"pricing snapshot is malformed: {count} schema error(s)") from None
        self._meta = parsed.meta
        try:
            self._rates: dict[str, dict[str, dict[str, Decimal]]] = {
                region: {
                    service: {key: Decimal(rate) for key, rate in table.items()}
                    for service, table in services.items()
                }
                for region, services in parsed.regions.items()
            }
        except InvalidOperation:
            raise SnapshotError("pricing snapshot contains a non-decimal rate") from None

    @property
    def meta(self) -> SnapshotMeta:
        """Snapshot provenance (version, date, sources)."""
        return self._meta

    @property
    def supported_regions(self) -> tuple[str, ...]:
        """Regions present in the snapshot, sorted (used by the R8 diagnostic)."""
        return tuple(sorted(self._rates))

    def get_rate(self, region: str, service_key: str, price_key: str) -> Decimal | None:
        """Return the rate for the triple, or ``None`` if any level is unknown."""
        return self._rates.get(region, {}).get(service_key, {}).get(price_key)


def _read_bundled_snapshot() -> dict[str, Any]:
    try:
        raw = (resources.files(_SNAPSHOT_PACKAGE) / _SNAPSHOT_RESOURCE).read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise SnapshotError(f"cannot load bundled pricing snapshot: {type(exc).__name__}") from None
    if not isinstance(data, dict):
        raise SnapshotError("bundled pricing snapshot is not a JSON object")
    return data
