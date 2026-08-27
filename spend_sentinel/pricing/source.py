"""The :class:`PricingSource` protocol — the estimator's only view of pricing.

``core.cost`` depends on this narrow, typed interface so the bundled snapshot
can later be swapped for a live adapter without touching the estimator
(see the spec's Out of scope / Modularity notes).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol


class PricingSource(Protocol):
    """Looks up an on-demand rate for a (region, service, price key) triple.

    ``service_key`` identifies the priced dimension (e.g. ``aws_instance``,
    ``aws_db_instance.instance``, ``aws_db_instance.storage``,
    ``aws_nat_gateway``, ``aws_lb``); ``price_key`` selects the concrete rate
    (e.g. ``t3.micro``, ``postgres:db.t3.medium``, ``gp3``, ``hourly``,
    ``application``). Returns ``None`` when the rate is not known.
    """

    def get_rate(self, region: str, service_key: str, price_key: str) -> Decimal | None:
        """Return the rate in USD, or ``None`` if the key is not in the source."""
        ...
