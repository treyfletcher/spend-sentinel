"""v1.1 chunk 1 — R28 RunCache and Budget primitives and the cached_resolve
entry point: one transport call per unique key (negative caching included),
budget exhaustion via a fake clock, and the never-raises guarantee across
every failure-injection mode.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import ClassVar

import pytest

from spend_sentinel.pricing.fixture_client import FixturePricingClient, RecordedCall
from spend_sentinel.pricing.live import (
    BUDGET_SECONDS,
    Budget,
    LiveFailureReason,
    LookupOutcome,
    PricingApiError,
    RunCache,
    build_query,
    cached_resolve,
    resolve_live_rate,
)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def hourly_entry(usd: str, publication: str = "2026-08-20T00:00:00Z") -> str:
    return json.dumps(
        {
            "product": {"attributes": {}},
            "terms": {"OnDemand": {"T": {"priceDimensions": {
                "T.D": {"unit": "Hrs", "pricePerUnit": {"USD": usd}}}}}},
            "publicationDate": publication,
        }
    )


def client_with(service_key: str, price_key: str, usd: str,
                region: str = "us-east-1") -> FixturePricingClient:
    spec = build_query(region, service_key, price_key)
    client = FixturePricingClient()
    client.add(spec.service_code, spec.filters,
               [{"PriceList": [hourly_entry(usd)]}])
    return client


def fresh_budget(clock: FakeClock | None = None) -> Budget:
    return Budget(clock=clock or FakeClock())


class TestRunCache:
    def test_put_get_roundtrip_and_len(self):
        cache = RunCache()
        outcome = LookupOutcome(rate=Decimal("1"))
        cache.put("us-east-1", "aws_instance", "t3.micro", outcome)
        assert cache.get("us-east-1", "aws_instance", "t3.micro") is outcome
        assert cache.get("us-east-1", "aws_instance", "t3.small") is None
        assert cache.get("eu-west-1", "aws_instance", "t3.micro") is None
        assert len(cache) == 1


class TestCachedResolveCaching:
    def test_r28_one_transport_call_per_unique_key(self):
        """AC18 second half: 10 resources sharing one instance type -> 1 query."""
        client = client_with("aws_instance", "t3.micro", "0.0110")
        cache, budget = RunCache(), fresh_budget()
        outcomes = [
            cached_resolve(client, cache, budget, "us-east-1", "aws_instance",
                           "t3.micro")
            for _ in range(10)
        ]
        assert len(client.calls) == 1
        assert all(o.ok and o.rate == Decimal("0.0110") for o in outcomes)

    def test_r28_negative_caching_failures_memoized(self):
        """A no_match outcome must be cached: the second lookup issues no call."""
        spec = build_query("us-east-1", "aws_instance", "t9.mega")
        client = FixturePricingClient()
        client.add(spec.service_code, spec.filters, [{"PriceList": []}])
        cache, budget = RunCache(), fresh_budget()
        first = cached_resolve(client, cache, budget, "us-east-1", "aws_instance",
                               "t9.mega")
        second = cached_resolve(client, cache, budget, "us-east-1", "aws_instance",
                                "t9.mega")
        assert first.failure is LiveFailureReason.NO_MATCH
        assert second is first
        assert len(client.calls) == 1

    def test_r28_distinct_keys_get_distinct_calls(self):
        client = FixturePricingClient()
        for key in ("t3.micro", "t3.small"):
            spec = build_query("us-east-1", "aws_instance", key)
            client.add(spec.service_code, spec.filters,
                       [{"PriceList": [hourly_entry("0.01")]}])
        cache, budget = RunCache(), fresh_budget()
        cached_resolve(client, cache, budget, "us-east-1", "aws_instance", "t3.micro")
        cached_resolve(client, cache, budget, "us-east-1", "aws_instance", "t3.small")
        assert len(client.calls) == 2
        assert len(cache) == 2

    def test_r28_region_is_part_of_the_cache_key(self):
        client = FixturePricingClient()
        for region in ("us-east-1", "eu-west-1"):
            spec = build_query(region, "aws_instance", "t3.micro")
            client.add(spec.service_code, spec.filters,
                       [{"PriceList": [hourly_entry("0.01")]}])
        cache, budget = RunCache(), fresh_budget()
        cached_resolve(client, cache, budget, "us-east-1", "aws_instance", "t3.micro")
        cached_resolve(client, cache, budget, "eu-west-1", "aws_instance", "t3.micro")
        assert len(client.calls) == 2

    def test_r28_unmappable_key_cached_without_any_call(self):
        client = FixturePricingClient()
        cache, budget = RunCache(), fresh_budget()
        outcome = cached_resolve(client, cache, budget, "us-east-1",
                                 "aws_db_instance.instance", "oracle:db.m5.large")
        assert outcome.failure is LiveFailureReason.UNMAPPED_VALUE
        assert client.calls == []
        assert len(cache) == 1


class TestBudget:
    def test_not_exhausted_before_limit(self):
        clock = FakeClock()
        budget = Budget(clock=clock)
        clock.advance(BUDGET_SECONDS - 0.01)
        assert not budget.exhausted

    def test_exhausted_at_exactly_the_limit(self):
        clock = FakeClock()
        budget = Budget(clock=clock)
        clock.advance(BUDGET_SECONDS)
        assert budget.exhausted

    def test_custom_budget_seconds(self):
        clock = FakeClock()
        budget = Budget(seconds=5.0, clock=clock)
        clock.advance(4.99)
        assert not budget.exhausted
        clock.advance(0.02)
        assert budget.exhausted

    def test_default_budget_is_30_seconds(self):
        assert BUDGET_SECONDS == 30.0


class TestBudgetExhaustion:
    def test_ac18_budget_exceeded_after_second_call(self):
        """AC18: 4 unique keys, 16s per call -> exactly 2 GetProducts queries;
        the remaining keys resolve budget_exhausted without transport calls."""
        clock = FakeClock()
        keys = ["t3.micro", "t3.small", "t3.medium", "t3.large"]
        client = FixturePricingClient(on_call=lambda call: clock.advance(16.0))
        for key in keys:
            spec = build_query("us-east-1", "aws_instance", key)
            client.add(spec.service_code, spec.filters,
                       [{"PriceList": [hourly_entry("0.01")]}])
        cache, budget = RunCache(), Budget(clock=clock)
        outcomes = {
            key: cached_resolve(client, cache, budget, "us-east-1", "aws_instance", key)
            for key in keys
        }
        assert len(client.calls) == 2
        assert outcomes["t3.micro"].ok and outcomes["t3.small"].ok
        assert outcomes["t3.medium"].failure is LiveFailureReason.BUDGET_EXHAUSTED
        assert outcomes["t3.large"].failure is LiveFailureReason.BUDGET_EXHAUSTED

    def test_r28_cached_hits_still_served_after_exhaustion(self):
        clock = FakeClock()
        client = client_with("aws_instance", "t3.micro", "0.0110")
        cache, budget = RunCache(), Budget(clock=clock)
        first = cached_resolve(client, cache, budget, "us-east-1", "aws_instance",
                               "t3.micro")
        clock.advance(BUDGET_SECONDS + 1)
        second = cached_resolve(client, cache, budget, "us-east-1", "aws_instance",
                                "t3.micro")
        assert second is first  # cache hit, not budget_exhausted
        assert len(client.calls) == 1

    def test_r28_exhausted_outcomes_are_negative_cached(self):
        clock = FakeClock()
        client = client_with("aws_instance", "t3.micro", "0.0110")
        cache = RunCache()
        budget = Budget(clock=clock)
        clock.advance(BUDGET_SECONDS + 1)  # budget spent before any lookup
        first = cached_resolve(client, cache, budget, "us-east-1", "aws_instance",
                               "t3.micro")
        second = cached_resolve(client, cache, budget, "us-east-1", "aws_instance",
                                "t3.micro")
        assert first.failure is LiveFailureReason.BUDGET_EXHAUSTED
        assert second is first
        assert client.calls == []


class TestNeverRaises:
    """cached_resolve / resolve_live_rate must always return a LookupOutcome —
    a sweep over every supported failure-injection mode."""

    def build_client(self, mode: str) -> FixturePricingClient:
        spec = build_query("us-east-1", "aws_instance", "t3.micro")
        client = FixturePricingClient()
        if mode == "api_error":
            client.add(spec.service_code, spec.filters,
                       PricingApiError(LiveFailureReason.API_ERROR))
        elif mode == "timeout":
            client.add(spec.service_code, spec.filters,
                       PricingApiError(LiveFailureReason.TIMEOUT))
        elif mode == "empty":
            client.add(spec.service_code, spec.filters, [{"PriceList": []}])
        elif mode == "malformed_entry":
            client.add(spec.service_code, spec.filters,
                       [{"PriceList": ["{broken"]}])
        elif mode == "hostile_price_list":
            client.add(spec.service_code, spec.filters, [{"PriceList": 42}])
        elif mode == "ambiguous":
            client.add(spec.service_code, spec.filters, [{"PriceList": [
                hourly_entry("0.01"), hourly_entry("0.02")]}])
        elif mode == "nan_rate":
            client.add(spec.service_code, spec.filters,
                       [{"PriceList": [hourly_entry("NaN")]}])
        elif mode == "oversize":
            big = json.dumps({"pad": "x" * (300 * 1024)})
            client.add(spec.service_code, spec.filters, [{"PriceList": [big]}])
        elif mode == "pagination_overflow":
            client.add(spec.service_code, spec.filters,
                       [{"PriceList": []} for _ in range(4)])
        # "unknown_key" mode: nothing registered -> fixture client returns empty
        return client

    EXPECTED: ClassVar[dict[str, LiveFailureReason]] = {
        "api_error": LiveFailureReason.API_ERROR,
        "timeout": LiveFailureReason.TIMEOUT,
        "empty": LiveFailureReason.NO_MATCH,
        "malformed_entry": LiveFailureReason.PARSE_ERROR,
        "hostile_price_list": LiveFailureReason.PARSE_ERROR,
        "ambiguous": LiveFailureReason.AMBIGUOUS,
        "nan_rate": LiveFailureReason.PARSE_ERROR,
        "oversize": LiveFailureReason.OVERSIZE_RESPONSE,
        "pagination_overflow": LiveFailureReason.PAGINATION_OVERFLOW,
        "unknown_key": LiveFailureReason.NO_MATCH,
    }

    @pytest.mark.parametrize("mode", sorted(EXPECTED))
    def test_failure_mode_returns_outcome_with_taxonomy_reason(self, mode):
        client = self.build_client(mode)
        outcome = cached_resolve(
            client, RunCache(), fresh_budget(), "us-east-1", "aws_instance", "t3.micro"
        )
        assert isinstance(outcome, LookupOutcome)
        assert not outcome.ok
        assert outcome.rate is None
        assert outcome.failure is self.EXPECTED[mode]
        assert outcome.failure in set(LiveFailureReason)

    @pytest.mark.parametrize(
        ("service_key", "price_key", "region", "reason"),
        [
            ("aws_instance", "t3.micro", "mars-north-1",
             LiveFailureReason.UNSUPPORTED_REGION),
            ("aws_db_instance.instance", "oracle:db.m5.large", "us-east-1",
             LiveFailureReason.UNMAPPED_VALUE),
            ("aws_kinesis_stream", "shard", "us-east-1",
             LiveFailureReason.UNMAPPED_VALUE),
        ],
    )
    def test_unmappable_inputs_return_outcome(self, service_key, price_key, region,
                                              reason):
        outcome = resolve_live_rate(FixturePricingClient(), region, service_key,
                                    price_key)
        assert isinstance(outcome, LookupOutcome)
        assert outcome.failure is reason

    def test_success_outcome_shape(self):
        client = client_with("aws_instance", "t3.micro", "0.0110")
        outcome = resolve_live_rate(client, "us-east-1", "aws_instance", "t3.micro")
        assert outcome.ok
        assert outcome.rate == Decimal("0.0110")
        assert outcome.failure is None
        assert outcome.publication_dates == ("2026-08-20T00:00:00Z",)


class TestFixtureClientContract:
    """The offline transport itself (production test infrastructure)."""

    def test_records_calls_with_filters_verbatim(self):
        spec = build_query("us-east-1", "aws_db_instance.instance",
                           "postgres:db.t3.micro")
        client = FixturePricingClient()
        client.add(spec.service_code, spec.filters, [{"PriceList": []}])
        client.get_products(spec.service_code, spec.filters, None)
        assert client.calls == [
            RecordedCall(service_code="AmazonRDS", filters=spec.filters,
                         next_token=None)
        ]

    def test_unregistered_key_returns_valid_empty_page(self):
        client = FixturePricingClient()
        page = client.get_products("AmazonEC2", (("instanceType", "x"),), None)
        assert page == {"PriceList": []}

    def test_error_injection_raises_on_call(self):
        client = FixturePricingClient()
        client.add("AmazonEC2", (), PricingApiError(LiveFailureReason.API_ERROR))
        with pytest.raises(PricingApiError):
            client.get_products("AmazonEC2", (), None)
        assert len(client.calls) == 1  # the call was still recorded
