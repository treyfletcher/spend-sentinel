"""``FixturePricingClient`` — an offline :class:`PricingApiClient` (R32).

Production test infrastructure, like ``FixtureAwsReader``: serves canned raw
``GetProducts`` pages keyed by (service_code, filter set), records every call
for filter-matrix assertions (AC16), and can inject transport errors or
per-call side effects (e.g. advancing a fake clock for budget tests, AC18).

Fixture shape per key: a list of page dicts, each a realistic GetProducts
response (``PriceList`` of JSON strings, optional ``NextToken`` linking to the
next page in the list). A key mapped to a :class:`PricingApiError` raises it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from spend_sentinel.pricing.live import PricingApiError

FixtureKey = tuple[str, frozenset[tuple[str, str]]]


def fixture_key(service_code: str, filters: tuple[tuple[str, str], ...]) -> FixtureKey:
    """The lookup key for a canned response: service code + filter set."""
    return (service_code, frozenset(filters))


@dataclass(frozen=True)
class RecordedCall:
    """One recorded ``get_products`` invocation, for assertions."""

    service_code: str
    filters: tuple[tuple[str, str], ...]
    next_token: str | None


class FixturePricingClient:
    """Offline PricingApiClient; satisfies the protocol structurally."""

    def __init__(
        self,
        pages: dict[FixtureKey, list[dict[str, Any]] | PricingApiError] | None = None,
        on_call: Callable[[RecordedCall], None] | None = None,
    ) -> None:
        self._pages = dict(pages or {})
        self._on_call = on_call
        self.calls: list[RecordedCall] = []

    def add(
        self,
        service_code: str,
        filters: tuple[tuple[str, str], ...],
        pages: list[dict[str, Any]] | PricingApiError,
    ) -> None:
        """Register canned pages (or an error) for one query."""
        self._pages[fixture_key(service_code, filters)] = pages

    def get_products(
        self,
        service_code: str,
        filters: tuple[tuple[str, str], ...],
        next_token: str | None,
    ) -> dict[str, Any]:
        call = RecordedCall(service_code=service_code, filters=filters, next_token=next_token)
        self.calls.append(call)
        if self._on_call is not None:
            self._on_call(call)

        entry = self._pages.get(fixture_key(service_code, filters))
        if isinstance(entry, PricingApiError):
            raise entry
        if entry is None or not entry:
            return {"PriceList": []}  # unknown/empty key: valid empty response
        index = 0 if next_token is None else _token_index(next_token, len(entry))
        page = dict(entry[index])
        if index + 1 < len(entry):
            page.setdefault("NextToken", str(index + 1))
        return page


def _token_index(token: str, page_count: int) -> int:
    try:
        index = int(token)
    except ValueError:
        return 0
    return min(max(index, 0), page_count - 1)
