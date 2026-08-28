"""Cost estimation for the priced resource types (R4-R7). Pure logic, no I/O.

Monthly cost math (R5): rates are :class:`~decimal.Decimal` end to end; hourly
rates are multiplied by the fixed 730 hours/month convention; each resource's
monthly delta is rounded half-up to cents; the total is the sum of the rounded
per-resource deltas.

Delta semantics (R6): create -> +cost(after); delete -> -cost(before);
update/replace -> cost(after) - cost(before).

Nothing is silently dropped (R7): a change of an unpriced type, with a price
key absent from the source, or with pricing-relevant attributes unknown at plan
time lands in the report's ``unpriced`` list with a reason from
:class:`~spend_sentinel.core.models.UnpricedReason`.

This module depends only on the :class:`~spend_sentinel.pricing.source.PricingSource`
protocol — never on a concrete adapter.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from spend_sentinel.core.models import (
    ActionClass,
    CostLine,
    CostReport,
    Plan,
    UnpricedReason,
    UnpricedResource,
)
from spend_sentinel.core.plan import classify_actions
from spend_sentinel.pricing.source import PricingSource

#: AWS monthly-estimate convention (spec assumption A4); not configurable in v1.
HOURS_PER_MONTH = Decimal("730")

_CENTS = Decimal("0.01")
_ZERO = Decimal("0.00")

#: The exact set of resource types priced in v1 (R4).
PRICED_TYPES: frozenset[str] = frozenset(
    {"aws_instance", "aws_ebs_volume", "aws_db_instance", "aws_nat_gateway", "aws_lb"}
)


class _Unpriced(Exception):
    """Internal control flow: this resource cannot be priced."""

    def __init__(self, reason: UnpricedReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


def estimate(plan: Plan, pricing: PricingSource, region: str) -> CostReport:
    """Estimate the monthly USD cost delta of ``plan`` in ``region`` (R4-R7)."""
    breakdown: list[CostLine] = []
    unpriced: list[UnpricedResource] = []
    total = _ZERO
    # R29 (v1.1) attribution hook: only a live source has drain_lookups; on
    # the default snapshot path this stays None and nothing below changes.
    drain = getattr(pricing, "drain_lookups", None)
    if drain is not None:
        drain()  # discard any stale lookups from before this estimate

    for rc in plan.resource_changes:
        action = classify_actions(rc.change.actions)
        if action is None:  # no-op / data-source read: excluded per R3
            continue
        if rc.type not in PRICED_TYPES:
            unpriced.append(
                UnpricedResource(
                    address=rc.address, type=rc.type, reason=UnpricedReason.UNSUPPORTED_TYPE
                )
            )
            continue
        try:
            delta = _delta(rc.type, action, rc.change.before, rc.change.after, pricing, region)
        except _Unpriced as exc:
            unpriced.append(UnpricedResource(address=rc.address, type=rc.type, reason=exc.reason))
            if drain is not None:
                drain()  # unpriced attempt: never leak lookups across resources
            continue
        delta = delta.quantize(_CENTS, rounding=ROUND_HALF_UP)
        if delta == 0:
            delta = _ZERO  # normalize -0.00
        price_source = _attribution(drain() if drain is not None else [])
        breakdown.append(
            CostLine(
                address=rc.address,
                type=rc.type,
                action=action,
                monthly_delta_usd=delta,
                price_source=price_source,
            )
        )
        total += delta

    if total == 0:
        total = _ZERO
    return CostReport(
        monthly_delta_usd=total.quantize(_CENTS, rounding=ROUND_HALF_UP),
        breakdown=tuple(breakdown),
        unpriced=tuple(unpriced),
    )


def _attribution(
    lookups: list[tuple[str, str, str]],
) -> Literal["live", "snapshot", "mixed"] | None:
    """R29: 'live' if all lookups live, 'snapshot' if all snapshot, else 'mixed'."""
    sources = {source for _service, _key, source in lookups}
    if sources == {"live"}:
        return "live"
    if sources == {"snapshot"}:
        return "snapshot"
    if sources:
        return "mixed"
    return None


def _delta(
    rtype: str,
    action: ActionClass,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    pricing: PricingSource,
    region: str,
) -> Decimal:
    """Per-resource delta per R6. Raises :class:`_Unpriced` when not priceable."""
    if action is ActionClass.CREATE:
        return _monthly_cost(rtype, after, pricing, region)
    if action is ActionClass.DELETE:
        return -_monthly_cost(rtype, before, pricing, region)
    # update / replace
    return _monthly_cost(rtype, after, pricing, region) - _monthly_cost(
        rtype, before, pricing, region
    )


def _monthly_cost(
    rtype: str, attrs: dict[str, Any] | None, pricing: PricingSource, region: str
) -> Decimal:
    """Monthly cost of one side (before or after) of a change, unrounded (R4)."""
    if attrs is None:
        raise _Unpriced(UnpricedReason.ATTRIBUTES_UNKNOWN)
    if rtype == "aws_instance":
        rate = _rate(pricing, region, "aws_instance", _require_str(attrs, "instance_type"))
        return rate * HOURS_PER_MONTH
    if rtype == "aws_ebs_volume":
        volume_type = _optional_str(attrs, "type", default="gp2")
        return _rate(pricing, region, "aws_ebs_volume", volume_type) * _require_number(
            attrs, "size"
        )
    if rtype == "aws_db_instance":
        engine = _require_str(attrs, "engine")
        instance_class = _require_str(attrs, "instance_class")
        hourly = _rate(pricing, region, "aws_db_instance.instance", f"{engine}:{instance_class}")
        instance_cost = hourly * HOURS_PER_MONTH
        if attrs.get("multi_az"):
            instance_cost *= 2
        storage_type = _optional_str(attrs, "storage_type", default="gp2")
        storage_rate = _rate(pricing, region, "aws_db_instance.storage", storage_type)
        return instance_cost + storage_rate * _require_number(attrs, "allocated_storage")
    if rtype == "aws_nat_gateway":
        return _rate(pricing, region, "aws_nat_gateway", "hourly") * HOURS_PER_MONTH
    if rtype == "aws_lb":
        lb_type = _optional_str(attrs, "load_balancer_type", default="application")
        return _rate(pricing, region, "aws_lb", lb_type) * HOURS_PER_MONTH
    raise _Unpriced(UnpricedReason.UNSUPPORTED_TYPE)  # pragma: no cover - guarded by caller


def _rate(pricing: PricingSource, region: str, service_key: str, price_key: str) -> Decimal:
    rate = pricing.get_rate(region, service_key, price_key)
    if rate is None:
        raise _Unpriced(UnpricedReason.UNKNOWN_PRICE_KEY)
    return rate


def _require_str(attrs: dict[str, Any], key: str) -> str:
    """A pricing-relevant attribute that must be present and a string.

    Absent/None means the value is unknown until apply (``after_unknown``) ->
    ``attributes_unknown``; a non-string value is equally unpriceable.
    """
    value = attrs.get(key)
    if not isinstance(value, str) or not value:
        raise _Unpriced(UnpricedReason.ATTRIBUTES_UNKNOWN)
    return value


def _optional_str(attrs: dict[str, Any], key: str, default: str) -> str:
    """Like :func:`_require_str` but with the provider's documented default."""
    value = attrs.get(key)
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        raise _Unpriced(UnpricedReason.ATTRIBUTES_UNKNOWN)
    return value


def _require_number(attrs: dict[str, Any], key: str) -> Decimal:
    """A pricing-relevant numeric attribute (GB sizes); unknown/invalid fails closed.

    Non-finite values need an explicit check (BUG-2): Python's ``json.loads``
    accepts ``NaN``/``Infinity``/``-Infinity`` and overflows ``1e400`` to
    ``inf``, and ``Decimal(str(...))`` constructs NaN/Infinity *successfully* —
    the InvalidOperation only fires later, at ``quantize`` in :func:`estimate`,
    escaping the fail-closed contract as a traceback.

    Negative values are rejected too (BUG-3): a negative GB count is impossible
    infrastructure, and pricing it would let a crafted plan carry a negative
    create delta that offsets real cost under the R14 ``max_monthly_delta``
    gate. The spec is silent on numeric ranges (flagged as S4); treating the
    attribute as ``attributes_unknown`` is the fail-closed reading — the
    resource stays visible in ``unpriced`` and triggers ``treat_unpriced_as``.
    """
    value = attrs.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _Unpriced(UnpricedReason.ATTRIBUTES_UNKNOWN)
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise _Unpriced(UnpricedReason.ATTRIBUTES_UNKNOWN) from None
    if not number.is_finite() or number < 0:
        raise _Unpriced(UnpricedReason.ATTRIBUTES_UNKNOWN)
    return number
