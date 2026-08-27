"""R5 (deterministic Decimal math, half-up cent rounding at resource level)
and R6 (delta semantics per action; total + per-resource breakdown).

Expected values are computed from the bundled snapshot, never hardcoded.
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal

import pytest

from spend_sentinel.core.cost import HOURS_PER_MONTH, estimate
from spend_sentinel.core.models import Plan
from spend_sentinel.core.plan import load_plan
from spend_sentinel.pricing.snapshot import SnapshotPricingSource

from .conftest import (
    fixture_path,
    load_snapshot,
    make_change,
    make_plan,
    run_analyze,
    write_plan,
)

SNAPSHOT = load_snapshot()


def rate(region: str, service: str, key: str) -> Decimal:
    return Decimal(SNAPSHOT["regions"][region][service][key])


def cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@pytest.fixture(scope="module")
def pricing() -> SnapshotPricingSource:
    return SnapshotPricingSource()


def single_change_report(pricing, *, actions, before=None, after=None, type_="aws_instance"):
    plan = Plan.model_validate(
        make_plan(
            [
                make_change(
                    address=f"{type_}.x",
                    type_=type_,
                    actions=actions,
                    before=before,
                    after=after,
                )
            ]
        )
    )
    return estimate(plan, pricing, "us-east-1")


class TestDeltaSemanticsR6:
    def test_r6_create_is_plus_cost_of_after(self, pricing):
        expected = cents(rate("us-east-1", "aws_instance", "m5.xlarge") * HOURS_PER_MONTH)
        report = single_change_report(
            pricing, actions=["create"], after={"instance_type": "m5.xlarge"}
        )
        assert report.breakdown[0].monthly_delta_usd == expected
        assert report.monthly_delta_usd == expected
        assert expected > 0

    def test_r6_delete_is_minus_cost_of_before(self, pricing):
        expected = -cents(rate("us-east-1", "aws_instance", "m5.xlarge") * HOURS_PER_MONTH)
        report = single_change_report(
            pricing, actions=["delete"], before={"instance_type": "m5.xlarge"}
        )
        assert report.breakdown[0].monthly_delta_usd == expected
        assert expected < 0

    def test_r6_update_resize_is_after_minus_before(self, pricing):
        """AC3: t3.large -> t3.xlarge; breakdown entry equals the rate difference
        and the total matches it."""
        plan = load_plan(fixture_path("update_resize.json"))
        report = estimate(plan, pricing, "us-east-1")
        expected = cents(
            (
                rate("us-east-1", "aws_instance", "t3.xlarge")
                - rate("us-east-1", "aws_instance", "t3.large")
            )
            * HOURS_PER_MONTH
        )
        assert len(report.breakdown) == 1
        assert report.breakdown[0].monthly_delta_usd == expected
        assert report.monthly_delta_usd == expected

    def test_r6_downsize_update_is_negative(self, pricing):
        expected = cents(
            (
                rate("us-east-1", "aws_instance", "t3.micro")
                - rate("us-east-1", "aws_instance", "t3.large")
            )
            * HOURS_PER_MONTH
        )
        report = single_change_report(
            pricing,
            actions=["update"],
            before={"instance_type": "t3.large"},
            after={"instance_type": "t3.micro"},
        )
        assert report.breakdown[0].monthly_delta_usd == expected
        assert expected < 0

    def test_r6_replace_is_after_minus_before(self, pricing):
        expected = cents(
            (
                rate("us-east-1", "aws_instance", "c5.large")
                - rate("us-east-1", "aws_instance", "t3.small")
            )
            * HOURS_PER_MONTH
        )
        report = single_change_report(
            pricing,
            actions=["delete", "create"],
            before={"instance_type": "t3.small"},
            after={"instance_type": "c5.large"},
        )
        assert report.breakdown[0].monthly_delta_usd == expected

    def test_r6_same_size_replace_is_zero(self, pricing):
        report = single_change_report(
            pricing,
            actions=["delete", "create"],
            before={"instance_type": "t3.micro"},
            after={"instance_type": "t3.micro"},
        )
        assert str(report.breakdown[0].monthly_delta_usd) == "0.00"

    def test_r6_total_is_sum_of_breakdown(self, pricing):
        plan = load_plan(fixture_path("mixed_actions.json"))
        report = estimate(plan, pricing, "us-east-1")
        assert report.monthly_delta_usd == sum(
            (line.monthly_delta_usd for line in report.breakdown), Decimal("0.00")
        )


class TestRoundingR5:
    def test_r5_half_cent_rounds_up_not_bankers(self, pricing):
        """ALB us-east-1: rate x 730 lands exactly on a half cent, so half-up
        and banker's rounding disagree — the spec demands half-up."""
        raw = rate("us-east-1", "aws_lb", "application") * HOURS_PER_MONTH
        # guard: this rate must actually exercise the half-cent boundary;
        # if the snapshot changes, pick another rate rather than deleting this.
        assert raw % Decimal("0.01") == Decimal("0.005"), (
            "snapshot no longer provides a half-cent case; update the test"
        )
        expected_half_up = raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        report = single_change_report(
            pricing, actions=["create"], after={"load_balancer_type": "application"},
            type_="aws_lb",
        )
        assert report.breakdown[0].monthly_delta_usd == expected_half_up
        # and it genuinely differs from ROUND_HALF_EVEN on this value
        from decimal import ROUND_HALF_EVEN

        assert expected_half_up != raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)

    def test_r5_negative_half_cent_rounds_away_from_zero(self, pricing):
        """Deleting the same ALB: -x.xx5 must round to -x.(xx+1) (half-up is
        symmetric/away-from-zero in Decimal)."""
        raw = rate("us-east-1", "aws_lb", "application") * HOURS_PER_MONTH
        expected = -(raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        report = single_change_report(
            pricing, actions=["delete"], before={"load_balancer_type": "application"},
            type_="aws_lb",
        )
        assert report.breakdown[0].monthly_delta_usd == expected

    def test_r5_rounding_happens_once_per_resource(self, pricing):
        """Update delta is computed on unrounded costs, then rounded once:
        cents(after - before), not cents(after) - cents(before)."""
        before_raw = rate("us-east-1", "aws_instance", "t2.micro") * HOURS_PER_MONTH
        after_raw = rate("us-east-1", "aws_instance", "t3.micro") * HOURS_PER_MONTH
        expected = cents(after_raw - before_raw)
        report = single_change_report(
            pricing,
            actions=["update"],
            before={"instance_type": "t2.micro"},
            after={"instance_type": "t3.micro"},
        )
        assert report.breakdown[0].monthly_delta_usd == expected

    def test_r5_zero_total_renders_as_positive_zero(self, pricing):
        report = single_change_report(
            pricing,
            actions=["update"],
            before={"instance_type": "t3.micro"},
            after={"instance_type": "t3.micro"},
        )
        assert str(report.monthly_delta_usd) == "0.00"  # never "-0.00"


class TestDeterminismR5:
    def test_r5_estimate_is_deterministic(self, pricing):
        plan = load_plan(fixture_path("mixed_actions.json"))
        first = estimate(plan, pricing, "us-east-1")
        second = estimate(plan, SnapshotPricingSource(), "us-east-1")
        assert first == second

    def test_r5_cli_cost_section_byte_identical_across_runs(self, runner, tmp_path):
        path = write_plan(
            tmp_path,
            make_plan(
                [
                    make_change(
                        address="aws_db_instance.db",
                        type_="aws_db_instance",
                        actions=["create"],
                        after={
                            "engine": "postgres",
                            "instance_class": "db.t3.medium",
                            "allocated_storage": 100,
                            "multi_az": True,
                        },
                    ),
                    make_change(
                        address="aws_lambda_function.fn",
                        type_="aws_lambda_function",
                        actions=["create"],
                        after={"function_name": "fn"},
                    ),
                ],
                provider_region="us-east-1",
            ),
        )
        first = run_analyze(runner, path)
        second = run_analyze(runner, path)
        assert first.exit_code == second.exit_code == 0
        assert first.stdout == second.stdout

    def test_r5_cost_values_are_two_decimal_strings_in_json(self, runner, tmp_path):
        """Spec modularity note: monetary values serialize as strings with two
        decimals for determinism."""
        path = write_plan(
            tmp_path,
            make_plan(
                [
                    make_change(
                        address="aws_nat_gateway.n",
                        type_="aws_nat_gateway",
                        actions=["create"],
                        after={},
                    )
                ],
                provider_region="us-east-1",
            ),
        )
        result = run_analyze(runner, path)
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        total = payload["cost"]["monthly_delta_usd"]
        assert isinstance(total, str)
        assert len(total.rsplit(".", 1)[1]) == 2
        for line in payload["cost"]["breakdown"]:
            value = line["monthly_delta_usd"]
            assert isinstance(value, str)
            assert len(value.rsplit(".", 1)[1]) == 2
