"""v1.1 chunk 2 — LivePricingSource (R24 resolution/fallback, R27 degradation
and warnings, R28 interplay, R29 attribution) and the R29 hook in
core/cost.py::estimate. All offline via FixturePricingClient; expected
snapshot rates come from the bundled snapshot file.

BUG-6 (untyped transport exception escapes get_rate/estimate) is xfailed with
a reference to docs/test-reports/feature-live-pricing-c2.md.
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal

import pytest

from spend_sentinel.core.cost import HOURS_PER_MONTH, estimate
from spend_sentinel.core.models import (
    LivePricingStatus,
    Plan,
    UnpricedReason,
)
from spend_sentinel.pricing.fixture_client import FixturePricingClient
from spend_sentinel.pricing.live import (
    LiveFailureReason,
    LivePricingSource,
    PricingApiError,
    build_query,
)
from spend_sentinel.pricing.snapshot import SnapshotPricingSource

from .conftest import load_snapshot, make_change, make_plan

SNAPSHOT_DATA = load_snapshot()


def snap_rate(region: str, service: str, key: str) -> Decimal:
    return Decimal(SNAPSHOT_DATA["regions"][region][service][key])


def cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def hourly_entry(usd: str, publication: str = "2026-08-20T00:00:00Z") -> str:
    return json.dumps(
        {
            "product": {"attributes": {}},
            "terms": {"OnDemand": {"T": {"priceDimensions": {
                "T.D": {"unit": "Hrs", "pricePerUnit": {"USD": usd}}}}}},
            "publicationDate": publication,
        }
    )


def gbmo_entry(usd: str, publication: str = "2026-08-21T00:00:00Z") -> str:
    return json.dumps(
        {
            "product": {"attributes": {}},
            "terms": {"OnDemand": {"T": {"priceDimensions": {
                "T.D": {"unit": "GB-Mo", "pricePerUnit": {"USD": usd}}}}}},
            "publicationDate": publication,
        }
    )


def add_live(client: FixturePricingClient, region: str, service_key: str,
             price_key: str, entries: list[str]) -> None:
    spec = build_query(region, service_key, price_key)
    client.add(spec.service_code, spec.filters, [{"PriceList": entries}])


def add_failure(client: FixturePricingClient, region: str, service_key: str,
                price_key: str, payload) -> None:
    spec = build_query(region, service_key, price_key)
    client.add(spec.service_code, spec.filters, payload)


def source_of(client: FixturePricingClient | None, **kwargs) -> LivePricingSource:
    return LivePricingSource(client, SnapshotPricingSource(), **kwargs)


def plan_of(*changes) -> Plan:
    return Plan.model_validate(make_plan(list(changes)))


INSTANCE_CREATE = make_change(
    address="aws_instance.web", actions=["create"], after={"instance_type": "t3.micro"}
)


class TestProtocolAndFallback:
    def test_r24_live_rate_used_when_extraction_succeeds(self):
        client = FixturePricingClient()
        add_live(client, "us-east-1", "aws_instance", "t3.micro",
                 [hourly_entry("0.0120")])
        source = source_of(client)
        assert source.get_rate("us-east-1", "aws_instance", "t3.micro") == Decimal(
            "0.0120"
        )

    def test_r24_live_rate_flows_through_estimate_to_the_cent(self):
        client = FixturePricingClient()
        add_live(client, "us-east-1", "aws_instance", "t3.micro",
                 [hourly_entry("0.0120")])
        report = estimate(plan_of(INSTANCE_CREATE), source_of(client), "us-east-1")
        assert report.breakdown[0].monthly_delta_usd == cents(
            Decimal("0.0120") * HOURS_PER_MONTH
        )
        assert report.breakdown[0].price_source == "live"

    @pytest.mark.parametrize(
        ("mode", "reason"),
        [
            ("no_match", LiveFailureReason.NO_MATCH),
            ("ambiguous", LiveFailureReason.AMBIGUOUS),
            ("parse_error", LiveFailureReason.PARSE_ERROR),
            ("oversize", LiveFailureReason.OVERSIZE_RESPONSE),
            ("api_error", LiveFailureReason.API_ERROR),
            ("timeout", LiveFailureReason.TIMEOUT),
            ("pagination_overflow", LiveFailureReason.PAGINATION_OVERFLOW),
        ],
    )
    def test_r24_per_key_fallback_for_every_key_level_reason(self, mode, reason):
        client = FixturePricingClient()
        payloads = {
            "no_match": [{"PriceList": []}],
            "ambiguous": [{"PriceList": [hourly_entry("0.01"), hourly_entry("0.02")]}],
            "parse_error": [{"PriceList": ["{broken"]}],
            "oversize": [{"PriceList": [json.dumps({"pad": "x" * (300 * 1024)})]}],
            "api_error": PricingApiError(LiveFailureReason.API_ERROR),
            "timeout": PricingApiError(LiveFailureReason.TIMEOUT),
            "pagination_overflow": [{"PriceList": []} for _ in range(4)],
        }
        add_failure(client, "us-east-1", "aws_instance", "t3.micro", payloads[mode])
        source = source_of(client)
        rate = source.get_rate("us-east-1", "aws_instance", "t3.micro")
        assert rate == snap_rate("us-east-1", "aws_instance", "t3.micro")
        report = source.report()
        assert report.status is LivePricingStatus.DEGRADED
        assert [(w.reason, w.detail) for w in report.warnings] == [
            (reason.value, "aws_instance/t3.micro")
        ]

    def test_r24_unmapped_value_falls_back_per_key(self):
        source = source_of(FixturePricingClient())
        rate = source.get_rate("us-east-1", "aws_db_instance.storage", "io2")
        # io2 is unmappable live but absent from the snapshot's RDS storage too
        assert rate is None
        warnings = source.report().warnings
        assert warnings[0].reason == LiveFailureReason.UNMAPPED_VALUE.value
        assert warnings[0].detail == "aws_db_instance.storage/io2"

    def test_r24_both_miss_returns_none_for_r7_taxonomy(self):
        """Both live (empty response) and snapshot miss -> None -> the v1
        unknown_price_key taxonomy unchanged through estimate()."""
        client = FixturePricingClient()  # nothing registered: empty PriceList
        plan = plan_of(
            make_change(address="aws_instance.exotic", actions=["create"],
                        after={"instance_type": "t9.mega"})
        )
        report = estimate(plan, source_of(client), "us-east-1")
        assert report.breakdown == ()
        assert [(u.address, u.reason) for u in report.unpriced] == [
            ("aws_instance.exotic", UnpricedReason.UNKNOWN_PRICE_KEY)
        ]

    def test_r24_snapshot_identical_behavior_for_fallback_keys(self):
        """estimate() with an all-failing live source equals the snapshot-only
        estimate in every cost figure."""
        client = FixturePricingClient()
        add_failure(client, "us-east-1", "aws_instance", "t3.micro",
                    PricingApiError(LiveFailureReason.API_ERROR))
        plan = plan_of(INSTANCE_CREATE)
        live_report = estimate(plan, source_of(client), "us-east-1")
        snap_report = estimate(plan, SnapshotPricingSource(), "us-east-1")
        assert live_report.monthly_delta_usd == snap_report.monthly_delta_usd
        assert (
            live_report.breakdown[0].monthly_delta_usd
            == snap_report.breakdown[0].monthly_delta_usd
        )
        assert live_report.breakdown[0].price_source == "snapshot"
        assert snap_report.breakdown[0].price_source is None


class TestRunLevelDisable:
    def test_r27_unsupported_region_disables_rest_of_run_no_transport_calls(self):
        client = FixturePricingClient()
        add_live(client, "us-east-1", "aws_instance", "t3.micro",
                 [hourly_entry("0.0120")])
        source = source_of(client)
        # first call in an unmapped region disables the API for the run
        assert source.get_rate("ap-southeast-9", "aws_instance", "t3.micro") is None
        # subsequent calls — even in a mappable region — must not touch the API
        rate = source.get_rate("us-east-1", "aws_instance", "t3.micro")
        assert rate == snap_rate("us-east-1", "aws_instance", "t3.micro")
        assert client.calls == []
        report = source.report()
        assert report.status is LivePricingStatus.UNAVAILABLE
        assert report.warnings[0].reason == LiveFailureReason.UNSUPPORTED_REGION.value
        assert report.warnings[0].detail == ""

    @pytest.mark.parametrize(
        "reason",
        [LiveFailureReason.BOTO3_MISSING, LiveFailureReason.CLIENT_INIT_ERROR],
    )
    def test_r27_disabled_construction_snapshot_only(self, reason):
        source = source_of(None, disabled_reason=reason)
        rate = source.get_rate("us-east-1", "aws_instance", "t3.micro")
        assert rate == snap_rate("us-east-1", "aws_instance", "t3.micro")
        report = source.report()
        assert report.status is LivePricingStatus.UNAVAILABLE
        assert [(w.reason, w.detail) for w in report.warnings] == [(reason.value, "")]

    def test_client_none_defaults_to_client_init_error(self):
        source = source_of(None)
        source.get_rate("us-east-1", "aws_instance", "t3.micro")
        assert source.report().warnings[0].reason == (
            LiveFailureReason.CLIENT_INIT_ERROR.value
        )

    def test_r27_run_level_warning_recorded_once(self):
        source = source_of(None, disabled_reason=LiveFailureReason.BOTO3_MISSING)
        for _ in range(5):
            source.get_rate("us-east-1", "aws_instance", "t3.micro")
        assert len(source.report().warnings) == 1


class TestBudgetThroughSource:
    def test_r28_budget_exhaustion_key_level_via_injected_clock(self):
        clock_value = [0.0]
        client = FixturePricingClient(
            on_call=lambda call: clock_value.__setitem__(0, clock_value[0] + 16.0)
        )
        for key in ("t3.micro", "t3.small", "t3.medium"):
            add_live(client, "us-east-1", "aws_instance", key, [hourly_entry("0.01")])
        source = source_of(client, clock=lambda: clock_value[0])
        assert source.get_rate("us-east-1", "aws_instance", "t3.micro") == Decimal("0.01")
        assert source.get_rate("us-east-1", "aws_instance", "t3.small") == Decimal("0.01")
        # budget (30s) exceeded after two 16s calls
        rate = source.get_rate("us-east-1", "aws_instance", "t3.medium")
        assert rate == snap_rate("us-east-1", "aws_instance", "t3.medium")
        assert len(client.calls) == 2
        report = source.report()
        assert report.status is LivePricingStatus.DEGRADED  # not run-level
        assert (LiveFailureReason.BUDGET_EXHAUSTED.value,
                "aws_instance/t3.medium") in [
            (w.reason, w.detail) for w in report.warnings
        ]

    def test_r28_custom_budget_seconds_honored(self):
        clock_value = [0.0]
        client = FixturePricingClient(
            on_call=lambda call: clock_value.__setitem__(0, clock_value[0] + 1.0)
        )
        add_live(client, "us-east-1", "aws_instance", "t3.micro",
                 [hourly_entry("0.01")])
        source = source_of(client, budget_seconds=0.5, clock=lambda: clock_value[0])
        clock_value[0] = 0.6
        rate = source.get_rate("us-east-1", "aws_instance", "t3.micro")
        assert rate == snap_rate("us-east-1", "aws_instance", "t3.micro")
        assert client.calls == []


class TestCacheInterplay:
    def test_r28_two_resources_one_key_one_call_both_live(self):
        client = FixturePricingClient()
        add_live(client, "us-east-1", "aws_instance", "t3.micro",
                 [hourly_entry("0.0120")])
        plan = plan_of(
            make_change(address="aws_instance.a", actions=["create"],
                        after={"instance_type": "t3.micro"}),
            make_change(address="aws_instance.b", actions=["create"],
                        after={"instance_type": "t3.micro"}),
        )
        report = estimate(plan, (source := source_of(client)), "us-east-1")
        assert len(client.calls) == 1
        assert [line.price_source for line in report.breakdown] == ["live", "live"]
        live_report = source.report()
        # A-c9: counters count get_rate resolutions, not unique keys
        assert live_report.lookups_live == 2
        assert live_report.lookups_snapshot_fallback == 0

    def test_r28_cached_failure_warns_once_per_key(self):
        """The same failing key looked up twice: one transport call, one
        de-duped warning (A-c8), two fallback counts (A-c9)."""
        client = FixturePricingClient()
        add_failure(client, "us-east-1", "aws_instance", "t3.micro",
                    PricingApiError(LiveFailureReason.API_ERROR))
        source = source_of(client)
        source.get_rate("us-east-1", "aws_instance", "t3.micro")
        source.get_rate("us-east-1", "aws_instance", "t3.micro")
        assert len(client.calls) == 1
        report = source.report()
        assert len(report.warnings) == 1
        assert report.lookups_snapshot_fallback == 2


class TestAttributionR29:
    def rds_multi_az_plan(self) -> Plan:
        return plan_of(
            make_change(
                address="aws_db_instance.db",
                type_="aws_db_instance",
                actions=["create"],
                after={
                    "engine": "postgres",
                    "instance_class": "db.t3.micro",
                    "allocated_storage": 100,
                    "multi_az": True,
                },
            )
        )

    def test_ac16_rds_mixed_live_instance_snapshot_storage(self):
        """AC16 core: live Single-AZ hourly x 2 x 730 + snapshot storage;
        the line is attributed 'mixed'."""
        client = FixturePricingClient()
        add_live(client, "us-east-1", "aws_db_instance.instance",
                 "postgres:db.t3.micro", [hourly_entry("0.0200")])
        # storage key gets an empty response -> snapshot fallback
        report = estimate(self.rds_multi_az_plan(), source_of(client), "us-east-1")
        line = report.breakdown[0]
        expected = cents(
            Decimal("0.0200") * HOURS_PER_MONTH * 2
            + snap_rate("us-east-1", "aws_db_instance.storage", "gp2") * 100
        )
        assert line.monthly_delta_usd == expected
        assert line.price_source == "mixed"

    def test_ac16_single_az_filter_on_the_recorded_call(self):
        client = FixturePricingClient()
        add_live(client, "us-east-1", "aws_db_instance.instance",
                 "postgres:db.t3.micro", [hourly_entry("0.0200")])
        estimate(self.rds_multi_az_plan(), source_of(client), "us-east-1")
        rds_calls = [c for c in client.calls if c.service_code == "AmazonRDS"
                     and ("databaseEngine", "PostgreSQL") in c.filters]
        assert rds_calls, "no RDS instance call recorded"
        assert ("deploymentOption", "Single-AZ") in rds_calls[0].filters

    def test_r29_all_live_rds_attributed_live(self):
        client = FixturePricingClient()
        add_live(client, "us-east-1", "aws_db_instance.instance",
                 "postgres:db.t3.micro", [hourly_entry("0.0200")])
        spec = build_query("us-east-1", "aws_db_instance.storage", "gp2")
        client.add(spec.service_code, spec.filters,
                   [{"PriceList": [gbmo_entry("0.1200")]}])
        report = estimate(self.rds_multi_az_plan(), source_of(client), "us-east-1")
        assert report.breakdown[0].price_source == "live"

    def test_r29_attribution_isolated_per_resource(self):
        """A live-priced resource next to a snapshot-priced one: each line
        carries its own source, no leakage."""
        client = FixturePricingClient()
        add_live(client, "us-east-1", "aws_instance", "t3.micro",
                 [hourly_entry("0.0120")])
        add_failure(client, "us-east-1", "aws_instance", "t3.large",
                    PricingApiError(LiveFailureReason.API_ERROR))
        plan = plan_of(
            INSTANCE_CREATE,
            make_change(address="aws_instance.big", actions=["create"],
                        after={"instance_type": "t3.large"}),
        )
        report = estimate(plan, source_of(client), "us-east-1")
        sources = {line.address: line.price_source for line in report.breakdown}
        assert sources == {"aws_instance.web": "live", "aws_instance.big": "snapshot"}

    def test_r29_unpriced_attempts_do_not_leak_lookups(self):
        """A both-miss resource before a priced one: the priced line's source
        must reflect only its own lookups."""
        client = FixturePricingClient()
        add_live(client, "us-east-1", "aws_instance", "t3.micro",
                 [hourly_entry("0.0120")])
        plan = plan_of(
            make_change(address="aws_instance.miss", actions=["create"],
                        after={"instance_type": "t9.mega"}),
            INSTANCE_CREATE,
        )
        report = estimate(plan, source_of(client), "us-east-1")
        assert [line.address for line in report.breakdown] == ["aws_instance.web"]
        assert report.breakdown[0].price_source == "live"

    def test_r29_stale_pre_estimate_lookups_discarded(self):
        client = FixturePricingClient()
        add_failure(client, "us-east-1", "aws_instance", "t3.micro",
                    PricingApiError(LiveFailureReason.API_ERROR))
        spec = build_query("us-east-1", "aws_ebs_volume", "gp3")
        client.add(spec.service_code, spec.filters,
                   [{"PriceList": [gbmo_entry("0.0850")]}])
        source = source_of(client)
        source.get_rate("us-east-1", "aws_instance", "t3.micro")  # stale snapshot lookup
        plan = plan_of(
            make_change(address="aws_ebs_volume.v", type_="aws_ebs_volume",
                        actions=["create"], after={"type": "gp3", "size": 20})
        )
        report = estimate(plan, source, "us-east-1")
        assert report.breakdown[0].price_source == "live"  # not polluted to mixed

    def test_r29_drain_lookups_returns_and_clears(self):
        client = FixturePricingClient()
        add_live(client, "us-east-1", "aws_instance", "t3.micro",
                 [hourly_entry("0.0120")])
        source = source_of(client)
        source.get_rate("us-east-1", "aws_instance", "t3.micro")
        drained = source.drain_lookups()
        assert drained == [("aws_instance", "t3.micro", "live")]
        assert source.drain_lookups() == []


class TestSnapshotPathUntouched:
    def test_r22_snapshot_estimate_price_source_none_everywhere(self):
        plan = plan_of(
            INSTANCE_CREATE,
            make_change(address="aws_ebs_volume.v", type_="aws_ebs_volume",
                        actions=["create"], after={"type": "gp3", "size": 20}),
        )
        report = estimate(plan, SnapshotPricingSource(), "us-east-1")
        assert all(line.price_source is None for line in report.breakdown)

    def test_r22_snapshot_source_has_no_drain_lookups(self):
        assert not hasattr(SnapshotPricingSource(), "drain_lookups")

    def test_r22_v1_json_renderer_output_carries_no_price_source(self):
        """Chunk 2 must not change any output byte: the current renderer's
        JSON has no price_source key for a snapshot run."""
        from spend_sentinel.core.models import (
            DriftReport,
            DriftStatus,
            PlanSummary,
            VerdictMeta,
        )
        from spend_sentinel.core.verdict import combine
        from spend_sentinel.render.jsonout import render_json

        plan = plan_of(INSTANCE_CREATE)
        cost = estimate(plan, SnapshotPricingSource(), "us-east-1")
        verdict = combine(
            PlanSummary(created=1, deleted=0, updated=0, replaced=0),
            cost,
            DriftReport(status=DriftStatus.SKIPPED),
            [],
            VerdictMeta(tool_version="0", pricing_snapshot_version="0",
                        pricing_snapshot_date="0", region="us-east-1"),
        )
        assert "price_source" not in render_json(verdict)


class TestReportSemantics:
    def test_a_c7_status_ok_only_when_everything_live(self):
        client = FixturePricingClient()
        add_live(client, "us-east-1", "aws_instance", "t3.micro",
                 [hourly_entry("0.0120", "2026-08-25T00:00:00Z")])
        source = source_of(client)
        source.get_rate("us-east-1", "aws_instance", "t3.micro")
        report = source.report()
        assert report.status is LivePricingStatus.OK
        assert report.requested is True
        assert report.lookups_live == 1
        assert report.lookups_snapshot_fallback == 0
        assert report.lookups_miss == 0
        assert report.warnings == ()

    def test_a_c7_any_fallback_makes_degraded(self):
        client = FixturePricingClient()
        add_live(client, "us-east-1", "aws_instance", "t3.micro",
                 [hourly_entry("0.0120")])
        add_failure(client, "us-east-1", "aws_instance", "t3.large",
                    [{"PriceList": []}])
        source = source_of(client)
        source.get_rate("us-east-1", "aws_instance", "t3.micro")
        source.get_rate("us-east-1", "aws_instance", "t3.large")
        assert source.report().status is LivePricingStatus.DEGRADED

    def test_a_c7_both_source_miss_makes_degraded(self):
        source = source_of(FixturePricingClient())
        assert source.get_rate("us-east-1", "aws_instance", "t9.mega") is None
        report = source.report()
        assert report.status is LivePricingStatus.DEGRADED
        assert report.lookups_miss == 1

    def test_a_c6_publication_dates_earliest_latest_pair(self):
        client = FixturePricingClient()
        add_live(client, "us-east-1", "aws_instance", "t3.micro",
                 [hourly_entry("0.0120", "2026-08-25T00:00:00Z")])
        add_live(client, "us-east-1", "aws_instance", "t3.large",
                 [hourly_entry("0.0900", "2026-08-20T00:00:00Z")])
        source = source_of(client)
        source.get_rate("us-east-1", "aws_instance", "t3.micro")
        source.get_rate("us-east-1", "aws_instance", "t3.large")
        assert source.report().publication_dates == (
            "2026-08-20T00:00:00Z",
            "2026-08-25T00:00:00Z",
        )

    def test_a_c6_single_date_pair_is_equal(self):
        client = FixturePricingClient()
        add_live(client, "us-east-1", "aws_instance", "t3.micro",
                 [hourly_entry("0.0120", "2026-08-25T00:00:00Z")])
        source = source_of(client)
        source.get_rate("us-east-1", "aws_instance", "t3.micro")
        assert source.report().publication_dates == (
            "2026-08-25T00:00:00Z",
            "2026-08-25T00:00:00Z",
        )

    def test_a_c6_no_live_rate_means_null_dates(self):
        source = source_of(None, disabled_reason=LiveFailureReason.BOTO3_MISSING)
        source.get_rate("us-east-1", "aws_instance", "t3.micro")
        assert source.report().publication_dates is None

    def test_a_c8_warnings_deduped_per_reason_and_key(self):
        client = FixturePricingClient()
        for key in ("t3.micro", "t3.large"):
            add_failure(client, "us-east-1", "aws_instance", key,
                        [{"PriceList": []}])
        source = source_of(client)
        for _ in range(3):
            source.get_rate("us-east-1", "aws_instance", "t3.micro")
            source.get_rate("us-east-1", "aws_instance", "t3.large")
        warnings = [(w.reason, w.detail) for w in source.report().warnings]
        assert warnings == [
            ("no_match", "aws_instance/t3.micro"),
            ("no_match", "aws_instance/t3.large"),
        ]

    def test_r31_warning_details_are_internal_keys_only(self):
        """A hostile response body must never surface in warning strings."""
        secret = "HOSTILE-RESPONSE-CONTENT-b7f3"
        client = FixturePricingClient()
        spec = build_query("us-east-1", "aws_instance", "t3.micro")
        client.add(spec.service_code, spec.filters,
                   [{"PriceList": [f'{{"broken": "{secret}"']}])  # malformed
        source = source_of(client)
        source.get_rate("us-east-1", "aws_instance", "t3.micro")
        dumped = source.report().model_dump_json()
        assert secret not in dumped
        assert "aws_instance/t3.micro" in dumped

    def test_a_c9_counters_count_get_rate_calls_not_keys(self):
        client = FixturePricingClient()
        add_live(client, "us-east-1", "aws_instance", "t3.micro",
                 [hourly_entry("0.0120")])
        source = source_of(client)
        for _ in range(4):
            source.get_rate("us-east-1", "aws_instance", "t3.micro")
        report = source.report()
        assert report.lookups_live == 4
        assert len(client.calls) == 1


class TestNeverRaisesThroughEstimate:
    @pytest.mark.xfail(
        strict=True,
        reason="BUG-6 (see docs/test-reports/feature-live-pricing-c2.md): an "
        "untyped exception from the transport escapes LivePricingSource.get_rate "
        "(docstring: 'never raises') and kills estimate(), violating R27's "
        "degradation-never-fails-the-run contract; needs a defensive catch-all "
        "mapping to api_error, mirroring drift's A-i18 posture",
    )
    def test_bug6_untyped_transport_exception_degrades_not_raises(self):
        class RogueClient:
            def get_products(self, service_code, filters, next_token):
                raise ValueError("untyped transport explosion")

        report = estimate(plan_of(INSTANCE_CREATE), source_of(RogueClient()),
                          "us-east-1")
        line = report.breakdown[0]
        assert line.price_source == "snapshot"
        assert line.monthly_delta_usd == cents(
            snap_rate("us-east-1", "aws_instance", "t3.micro") * HOURS_PER_MONTH
        )

    def test_typed_transport_errors_never_reach_estimate(self):
        client = FixturePricingClient()
        add_failure(client, "us-east-1", "aws_instance", "t3.micro",
                    PricingApiError(LiveFailureReason.TIMEOUT))
        report = estimate(plan_of(INSTANCE_CREATE), source_of(client), "us-east-1")
        assert report.breakdown[0].price_source == "snapshot"
