"""R4: per-type monthly pricing from the bundled snapshot.

Every expected value is computed FROM spend_sentinel/data/pricing_snapshot.json
(loaded independently of the code under test) — never hardcoded. Also verifies
the snapshot itself against the spec's T3 matrix and R4's coverage promises,
and the coder's provider-default assumptions (A-i7) and multi_az doubling
(A-i12).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import pytest

from spend_sentinel.core.cost import HOURS_PER_MONTH, estimate
from spend_sentinel.core.plan import load_plan
from spend_sentinel.pricing.snapshot import SnapshotError, SnapshotPricingSource

from .conftest import fixture_path, load_snapshot, make_change, make_plan, write_plan

SNAPSHOT = load_snapshot()
REGIONS = ("us-east-1", "us-west-2", "eu-west-1")
EBS_TYPES = ("gp2", "gp3", "io1", "io2", "st1", "standard")


def rate(region: str, service: str, key: str) -> Decimal:
    return Decimal(SNAPSHOT["regions"][region][service][key])


def cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@pytest.fixture(scope="module")
def pricing() -> SnapshotPricingSource:
    return SnapshotPricingSource()


def one_create_delta(pricing, region, type_, after):
    """Estimate a single-create plan and return its only breakdown delta."""
    from spend_sentinel.core.models import Plan

    plan = Plan.model_validate(
        make_plan([make_change(address=f"{type_}.x", type_=type_, actions=["create"], after=after)])
    )
    report = estimate(plan, pricing, region)
    assert report.unpriced == (), f"unexpectedly unpriced: {report.unpriced}"
    assert len(report.breakdown) == 1
    return report.breakdown[0].monthly_delta_usd


class TestInstancePricing:
    @pytest.mark.parametrize("region", REGIONS)
    @pytest.mark.parametrize("itype", ["t3.micro", "t3.xlarge", "m5.large", "r5.large"])
    def test_r4_instance_hourly_times_730(self, pricing, region, itype):
        expected = cents(rate(region, "aws_instance", itype) * HOURS_PER_MONTH)
        got = one_create_delta(pricing, region, "aws_instance", {"instance_type": itype})
        assert got == expected


class TestEbsPricing:
    @pytest.mark.parametrize("vtype", EBS_TYPES)
    def test_r4_ebs_per_gb_month(self, pricing, vtype):
        expected = cents(rate("us-east-1", "aws_ebs_volume", vtype) * Decimal("123"))
        got = one_create_delta(
            pricing, "us-east-1", "aws_ebs_volume", {"type": vtype, "size": 123}
        )
        assert got == expected

    def test_r4_ebs_type_defaults_to_gp2(self, pricing):
        """A-i7: absent `type` uses the provider default gp2."""
        expected = cents(rate("us-east-1", "aws_ebs_volume", "gp2") * Decimal("50"))
        got = one_create_delta(pricing, "us-east-1", "aws_ebs_volume", {"size": 50})
        assert got == expected


class TestDbInstancePricing:
    @pytest.mark.parametrize(
        ("engine", "klass"),
        [("postgres", "db.t3.medium"), ("mysql", "db.r5.large")],
    )
    def test_r4_db_instance_plus_storage(self, pricing, engine, klass):
        hourly = rate("us-east-1", "aws_db_instance.instance", f"{engine}:{klass}")
        storage = rate("us-east-1", "aws_db_instance.storage", "gp3")
        expected = cents(hourly * HOURS_PER_MONTH + storage * Decimal("100"))
        got = one_create_delta(
            pricing,
            "us-east-1",
            "aws_db_instance",
            {
                "engine": engine,
                "instance_class": klass,
                "storage_type": "gp3",
                "allocated_storage": 100,
                "multi_az": False,
            },
        )
        assert got == expected

    def test_r4_db_multi_az_doubles_instance_component_only(self, pricing):
        """R4/A-i12: multi_az doubles the instance hourly component, not storage."""
        hourly = rate("us-east-1", "aws_db_instance.instance", "postgres:db.m5.large")
        storage = rate("us-east-1", "aws_db_instance.storage", "io1")
        expected = cents(hourly * HOURS_PER_MONTH * 2 + storage * Decimal("200"))
        got = one_create_delta(
            pricing,
            "us-east-1",
            "aws_db_instance",
            {
                "engine": "postgres",
                "instance_class": "db.m5.large",
                "storage_type": "io1",
                "allocated_storage": 200,
                "multi_az": True,
            },
        )
        assert got == expected

    def test_r4_db_storage_type_defaults_to_gp2(self, pricing):
        """A-i7: absent `storage_type` uses gp2."""
        hourly = rate("us-east-1", "aws_db_instance.instance", "mysql:db.t3.micro")
        storage = rate("us-east-1", "aws_db_instance.storage", "gp2")
        expected = cents(hourly * HOURS_PER_MONTH + storage * Decimal("20"))
        got = one_create_delta(
            pricing,
            "us-east-1",
            "aws_db_instance",
            {"engine": "mysql", "instance_class": "db.t3.micro", "allocated_storage": 20},
        )
        assert got == expected

    def test_r4_db_multi_az_false_not_doubled(self, pricing):
        hourly = rate("us-east-1", "aws_db_instance.instance", "postgres:db.t3.small")
        storage = rate("us-east-1", "aws_db_instance.storage", "gp2")
        expected = cents(hourly * HOURS_PER_MONTH + storage * Decimal("30"))
        got = one_create_delta(
            pricing,
            "us-east-1",
            "aws_db_instance",
            {
                "engine": "postgres",
                "instance_class": "db.t3.small",
                "allocated_storage": 30,
                "multi_az": False,
            },
        )
        assert got == expected


class TestNatAndLbPricing:
    @pytest.mark.parametrize("region", REGIONS)
    def test_r4_nat_gateway_hourly_times_730(self, pricing, region):
        expected = cents(rate(region, "aws_nat_gateway", "hourly") * HOURS_PER_MONTH)
        got = one_create_delta(pricing, region, "aws_nat_gateway", {})
        assert got == expected

    @pytest.mark.parametrize("lb_type", ["application", "network"])
    def test_r4_lb_hourly_by_type(self, pricing, lb_type):
        expected = cents(rate("us-east-1", "aws_lb", lb_type) * HOURS_PER_MONTH)
        got = one_create_delta(
            pricing, "us-east-1", "aws_lb", {"load_balancer_type": lb_type}
        )
        assert got == expected

    def test_r4_lb_type_defaults_to_application(self, pricing):
        """A-i7: absent `load_balancer_type` uses application."""
        expected = cents(rate("us-east-1", "aws_lb", "application") * HOURS_PER_MONTH)
        got = one_create_delta(pricing, "us-east-1", "aws_lb", {})
        assert got == expected


class TestSnapshotIntegrity:
    """The snapshot file itself must honor R4's and T3's coverage promises."""

    def test_r4_snapshot_has_the_three_required_regions(self):
        assert set(SNAPSHOT["regions"]) >= set(REGIONS)

    def test_r4_snapshot_meta_provenance(self):
        meta = SNAPSHOT["meta"]
        assert meta["version"]
        assert meta["snapshot_date"]
        assert isinstance(meta["sources"], list) and len(meta["sources"]) > 0

    @pytest.mark.parametrize("region", REGIONS)
    def test_t3_matrix_coverage_per_region(self, region):
        services = SNAPSHOT["regions"][region]
        assert len(services["aws_instance"]) >= 10  # >= 10 common EC2 types
        assert set(services["aws_ebs_volume"]) >= set(EBS_TYPES)  # all six R4 EBS types
        rds = services["aws_db_instance.instance"]
        for engine in ("postgres", "mysql"):
            classes = [k for k in rds if k.startswith(f"{engine}:")]
            assert len(classes) >= 5, f"{region}: <5 RDS classes for {engine}"
        assert "hourly" in services["aws_nat_gateway"]
        assert {"application", "network"} <= set(services["aws_lb"])

    def test_r5_all_rates_are_positive_decimals(self):
        for region, services in SNAPSHOT["regions"].items():
            for service, table in services.items():
                for key, raw in table.items():
                    value = Decimal(raw)  # raises if not exact-decimal
                    assert value > 0, f"{region}/{service}/{key} rate not positive"

    def test_r4_snapshot_source_meta_exposed(self, pricing):
        assert pricing.meta.version == SNAPSHOT["meta"]["version"]
        assert pricing.meta.snapshot_date == SNAPSHOT["meta"]["snapshot_date"]

    def test_r8_supported_regions_sorted(self, pricing):
        assert pricing.supported_regions == tuple(sorted(SNAPSHOT["regions"]))

    def test_get_rate_unknown_levels_return_none(self, pricing):
        assert pricing.get_rate("mars-north-1", "aws_instance", "t3.micro") is None
        assert pricing.get_rate("us-east-1", "aws_fake_service", "t3.micro") is None
        assert pricing.get_rate("us-east-1", "aws_instance", "t9.mega") is None


class TestSnapshotLoaderFailsClosed:
    """The loader itself fails closed on a malformed snapshot (packaging-bug
    surface); the ``data`` constructor parameter exists for exactly this."""

    def test_missing_meta_raises_snapshot_error(self):
        with pytest.raises(SnapshotError):
            SnapshotPricingSource(data={"regions": {}})

    def test_wrong_shaped_regions_raises_snapshot_error(self):
        data = {
            "meta": {"version": "t", "snapshot_date": "d", "sources": ["s"]},
            "regions": {"us-east-1": {"aws_instance": "not-a-table"}},
        }
        with pytest.raises(SnapshotError):
            SnapshotPricingSource(data=data)

    def test_non_decimal_rate_raises_snapshot_error(self):
        data = {
            "meta": {"version": "t", "snapshot_date": "d", "sources": ["s"]},
            "regions": {"us-east-1": {"aws_instance": {"t3.micro": "not-a-rate"}}},
        }
        with pytest.raises(SnapshotError):
            SnapshotPricingSource(data=data)

    def test_snapshot_error_does_not_echo_values(self):
        """SnapshotError diagnostics reach stderr via the CLI; they must not
        echo snapshot content (same posture as R2 diagnostics)."""
        marker = "CANARY-VALUE-93af"
        with pytest.raises(SnapshotError) as excinfo:
            SnapshotPricingSource(data={"meta": {"version": marker}, "regions": {}})
        assert marker not in str(excinfo.value)


class TestPricingThroughCli:
    def test_r4_cli_create_small_prices_to_the_cent(self, runner, tmp_path):
        """AC1-flavored (cost part only): instance + gp3 volume sum to the cent."""
        instance = cents(rate("us-east-1", "aws_instance", "t3.micro") * HOURS_PER_MONTH)
        volume = cents(rate("us-east-1", "aws_ebs_volume", "gp3") * Decimal("20"))
        path = write_plan(
            tmp_path,
            make_plan(
                [
                    make_change(
                        address="aws_instance.web",
                        type_="aws_instance",
                        actions=["create"],
                        after={"instance_type": "t3.micro"},
                    ),
                    make_change(
                        address="aws_ebs_volume.data",
                        type_="aws_ebs_volume",
                        actions=["create"],
                        after={"type": "gp3", "size": 20},
                    ),
                ],
                provider_region="us-east-1",
            ),
        )
        from .conftest import run_analyze_json

        result, payload = run_analyze_json(runner, path)
        assert result.exit_code == 0
        assert payload["verdict"] == "PASS"
        assert payload["cost"]["monthly_delta_usd"] == str(instance + volume)
        assert payload["cost"]["unpriced"] == []

    def test_r4_priced_types_only_no_op_still_excluded(self, pricing):
        """A no-op change of a priced type contributes nothing anywhere."""
        plan = load_plan(fixture_path("noop_only.json"))
        report = estimate(plan, pricing, "us-east-1")
        assert report.breakdown == ()
        assert report.unpriced == ()
        assert str(report.monthly_delta_usd) == "0.00"
