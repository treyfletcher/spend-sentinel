"""JSON verdict renderer (R19). Structure documented in ``docs/verdict-schema.md``.

Deterministic by construction: keys are emitted in a fixed order and all
monetary values are strings with exactly two decimals (Modularity notes —
floats never carry money). Takes the finished
:class:`~spend_sentinel.core.models.Verdict` only.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from spend_sentinel.core.models import CostLine, Verdict


def render_json(verdict: Verdict) -> str:
    """Serialize the verdict to the documented JSON structure (trailing newline)."""
    return json.dumps(_to_dict(verdict), indent=2, ensure_ascii=False) + "\n"


def _money(value: Decimal) -> str:
    """Two-decimal string; the model quantizes to cents upstream (R5)."""
    return f"{value:.2f}"


def _to_dict(verdict: Verdict) -> dict[str, Any]:
    return {
        "verdict": verdict.verdict.value,
        "summary": {
            "created": verdict.summary.created,
            "deleted": verdict.summary.deleted,
            "updated": verdict.summary.updated,
            "replaced": verdict.summary.replaced,
            "changed": verdict.summary.changed,
        },
        "cost": {
            "monthly_delta_usd": _money(verdict.cost.monthly_delta_usd),
            "breakdown": [_cost_line(line) for line in verdict.cost.breakdown],
            "unpriced": [
                {"address": u.address, "type": u.type, "reason": u.reason.value}
                for u in verdict.cost.unpriced
            ],
        },
        "drift": {
            "status": verdict.drift.status.value,
            "drifts": [
                {
                    "address": d.address,
                    "kind": d.kind.value,
                    "attribute": d.attribute,
                    "state_value": d.state_value,
                    "live_value": d.live_value,
                }
                for d in verdict.drift.drifts
            ],
            "skipped": [
                {"address": s.address, "type": s.type, "reason": s.reason}
                for s in verdict.drift.skipped
            ],
            "errors": [
                {"address": e.address, "error": e.error} for e in verdict.drift.errors
            ],
        },
        "policy": {
            "rules": [
                {"name": r.name, "result": r.result.value, "message": r.message}
                for r in verdict.policy
            ]
        },
        "meta": _meta(verdict),
    }


def _cost_line(line: CostLine) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "address": line.address,
        "type": line.type,
        "action": line.action.value,
        "monthly_delta_usd": _money(line.monthly_delta_usd),
    }
    # v1.1 (R29): present only on live-pricing runs; omitted when None so the
    # default path stays byte-identical to v1 (R22).
    if line.price_source is not None:
        entry["price_source"] = line.price_source
    return entry


def _meta(verdict: Verdict) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "tool_version": verdict.meta.tool_version,
        "pricing_snapshot_version": verdict.meta.pricing_snapshot_version,
        "pricing_snapshot_date": verdict.meta.pricing_snapshot_date,
        "region": verdict.meta.region,
    }
    lp = verdict.meta.live_pricing
    if lp is not None:  # v1.1 (R30): only under --live-pricing
        meta["live_pricing"] = {
            "requested": lp.requested,
            "status": lp.status.value,
            "endpoint_region": lp.endpoint_region,
            "lookups": {
                "live": lp.lookups_live,
                "snapshot_fallback": lp.lookups_snapshot_fallback,
                "miss": lp.lookups_miss,
            },
            "publication_dates": (
                None
                if lp.publication_dates is None
                else {
                    "earliest": lp.publication_dates[0],
                    "latest": lp.publication_dates[1],
                }
            ),
            "warnings": [
                {"reason": w.reason, "detail": w.detail} for w in lp.warnings
            ],
        }
    return meta
