"""R7: nothing silently dropped — the unpriced taxonomy
(unsupported_type | unknown_price_key | attributes_unknown), plus the coder's
A-i8 assumption (wrong-typed attributes -> attributes_unknown) and
security/edge behavior of hostile attribute values.

Known defects BUG-2 and BUG-3 are xfailed with references to
docs/test-reports/feature-spend-sentinel-v1-increment2.md.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from spend_sentinel.core.cost import estimate
from spend_sentinel.core.models import Plan, UnpricedReason
from spend_sentinel.core.plan import load_plan
from spend_sentinel.pricing.snapshot import SnapshotPricingSource

from .conftest import fixture_path, make_change, make_plan, run_analyze, write_plan


@pytest.fixture(scope="module")
def pricing() -> SnapshotPricingSource:
    return SnapshotPricingSource()


def unpriced_reason(pricing, *, type_, after, actions=None):
    plan = Plan.model_validate(
        make_plan(
            [
                make_change(
                    address=f"{type_}.x",
                    type_=type_,
                    actions=actions or ["create"],
                    after=after,
                )
            ]
        )
    )
    report = estimate(plan, pricing, "us-east-1")
    assert report.breakdown == (), "resource was priced but should be unpriced"
    assert len(report.unpriced) == 1
    return report.unpriced[0].reason


class TestTaxonomy:
    def test_r7_unsupported_type(self, pricing):
        assert (
            unpriced_reason(pricing, type_="aws_lambda_function", after={"function_name": "f"})
            == UnpricedReason.UNSUPPORTED_TYPE
        )

    def test_r7_unknown_price_key_instance_type(self, pricing):
        assert (
            unpriced_reason(pricing, type_="aws_instance", after={"instance_type": "t9.mega"})
            == UnpricedReason.UNKNOWN_PRICE_KEY
        )

    def test_r7_unknown_price_key_rds_engine_alias(self, pricing):
        """A-i9: engine aliases not in the snapshot surface as unknown_price_key."""
        assert (
            unpriced_reason(
                pricing,
                type_="aws_db_instance",
                after={
                    "engine": "aurora-postgresql",
                    "instance_class": "db.t3.medium",
                    "allocated_storage": 100,
                },
            )
            == UnpricedReason.UNKNOWN_PRICE_KEY
        )

    @pytest.mark.parametrize(
        ("type_", "after"),
        [
            ("aws_instance", {}),  # instance_type missing
            ("aws_instance", {"instance_type": None}),  # unknown until apply
            ("aws_ebs_volume", {"type": "gp3"}),  # size missing
            ("aws_db_instance", {"instance_class": "db.t3.micro"}),  # engine missing
            (
                "aws_db_instance",
                {"engine": "postgres", "instance_class": "db.t3.micro"},
            ),  # allocated_storage missing
        ],
    )
    def test_r7_attributes_unknown_when_missing_or_null(self, pricing, type_, after):
        assert unpriced_reason(pricing, type_=type_, after=after) == (
            UnpricedReason.ATTRIBUTES_UNKNOWN
        )

    def test_r7_delete_with_null_before_is_attributes_unknown(self, pricing):
        plan = Plan.model_validate(
            make_plan(
                [
                    make_change(
                        address="aws_instance.gone",
                        type_="aws_instance",
                        actions=["delete"],
                        before=None,
                    )
                ]
            )
        )
        report = estimate(plan, pricing, "us-east-1")
        assert report.unpriced[0].reason == UnpricedReason.ATTRIBUTES_UNKNOWN

    def test_r7_update_with_unknown_after_side_is_attributes_unknown(self, pricing):
        plan = Plan.model_validate(
            make_plan(
                [
                    make_change(
                        address="aws_instance.resize",
                        type_="aws_instance",
                        actions=["update"],
                        before={"instance_type": "t3.micro"},
                        after={},  # unknown until apply
                    )
                ]
            )
        )
        report = estimate(plan, pricing, "us-east-1")
        assert report.breakdown == ()
        assert report.unpriced[0].reason == UnpricedReason.ATTRIBUTES_UNKNOWN


class TestWrongTypedAttributes:
    """A-i8: wrong-typed pricing-relevant attributes -> attributes_unknown,
    never a crash, never a silently wrong price."""

    @pytest.mark.parametrize(
        ("type_", "after"),
        [
            ("aws_instance", {"instance_type": 42}),
            ("aws_instance", {"instance_type": ["t3.micro"]}),
            ("aws_instance", {"instance_type": {"nested": "t3.micro"}}),
            ("aws_instance", {"instance_type": ""}),
            ("aws_ebs_volume", {"size": "100"}),  # numeric string is not a number
            ("aws_ebs_volume", {"size": True}),  # bool is not a size
            ("aws_ebs_volume", {"size": [100]}),
            ("aws_ebs_volume", {"type": 3, "size": 100}),
            ("aws_lb", {"load_balancer_type": 1}),
            (
                "aws_db_instance",
                {"engine": "postgres", "instance_class": "db.t3.micro",
                 "allocated_storage": "twenty"},
            ),
            (
                "aws_db_instance",
                {"engine": ["postgres"], "instance_class": "db.t3.micro",
                 "allocated_storage": 20},
            ),
        ],
    )
    def test_r7_wrong_typed_attribute_is_attributes_unknown(self, pricing, type_, after):
        assert unpriced_reason(pricing, type_=type_, after=after) == (
            UnpricedReason.ATTRIBUTES_UNKNOWN
        )


class TestAc4Style:
    def test_r7_ac4_exactly_two_unpriced_with_reasons(self, pricing):
        plan = load_plan(fixture_path("mixed_unpriced.json"))
        report = estimate(plan, pricing, "us-east-1")
        reasons = {u.address: u.reason for u in report.unpriced}
        assert reasons == {
            "aws_lambda_function.fn": UnpricedReason.UNSUPPORTED_TYPE,
            "aws_instance.exotic": UnpricedReason.UNKNOWN_PRICE_KEY,
        }
        # the priceable sibling is still priced
        assert [line.address for line in report.breakdown] == ["aws_instance.priced"]

    def test_r7_ac4_through_cli(self, runner):
        result = run_analyze(runner, fixture_path("mixed_unpriced.json"))
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert len(payload["cost"]["unpriced"]) == 2
        reasons = {u["address"]: u["reason"] for u in payload["cost"]["unpriced"]}
        assert reasons == {
            "aws_lambda_function.fn": "unsupported_type",
            "aws_instance.exotic": "unknown_price_key",
        }


class TestNothingSilentlyDropped:
    def test_r7_every_change_lands_in_breakdown_or_unpriced(self, pricing):
        plan = load_plan(fixture_path("mixed_actions.json"))
        report = estimate(plan, pricing, "us-east-1")
        from spend_sentinel.core.plan import summarize_plan

        _, classified = summarize_plan(plan)
        accounted = {line.address for line in report.breakdown} | {
            u.address for u in report.unpriced
        }
        assert accounted == {c.address for c in classified}

    def test_r7_unpriced_entries_carry_address_and_type(self, pricing):
        plan = load_plan(fixture_path("mixed_unpriced.json"))
        report = estimate(plan, pricing, "us-east-1")
        for entry in report.unpriced:
            assert entry.address
            assert entry.type
            assert entry.reason in set(UnpricedReason)


class TestHostileNumericValues:
    """Hostile JSON numbers in pricing-relevant attributes must fail closed."""

    @staticmethod
    def run_cli(path: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "spend_sentinel.cli", "analyze",
             "--plan", path, "--region", "us-east-1"],
            capture_output=True,
            text=True,
            timeout=60,
        )

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e400"])
    def test_r7_nonfinite_size_fails_closed(self, tmp_path, literal):
        """BUG-2 (fixed): non-finite JSON numbers in size attributes fail closed
        as attributes_unknown — visibly unpriced, never a traceback."""
        payload = (
            '{"format_version":"1.2","resource_changes":[{"address":"aws_ebs_volume.x",'
            '"mode":"managed","type":"aws_ebs_volume","name":"x","provider_name":"aws",'
            '"change":{"actions":["create"],"before":null,'
            '"after":{"type":"gp3","size":' + literal + "}}}]}"
        )
        p = tmp_path / "nonfinite.json"
        p.write_text(payload)
        proc = self.run_cli(str(p))
        assert "Traceback" not in proc.stderr
        assert proc.returncode == 0
        out = json.loads(proc.stdout)
        assert [u["reason"] for u in out["cost"]["unpriced"]] == ["attributes_unknown"]
        assert out["cost"]["breakdown"] == []
        assert out["cost"]["monthly_delta_usd"] == "0.00"

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_r7_nonfinite_allocated_storage_is_attributes_unknown(self, pricing, bad):
        """BUG-2 also covered allocated_storage (tester report); pin it at the
        core level."""
        assert unpriced_reason(
            pricing,
            type_="aws_db_instance",
            after={"engine": "postgres", "instance_class": "db.t3.micro",
                   "allocated_storage": bad},
        ) == UnpricedReason.ATTRIBUTES_UNKNOWN

    @pytest.mark.parametrize(
        ("type_", "after"),
        [
            ("aws_ebs_volume", {"type": "gp3", "size": -100}),
            (
                "aws_db_instance",
                {"engine": "postgres", "instance_class": "db.t3.micro",
                 "allocated_storage": -1},
            ),
        ],
    )
    def test_r7_negative_size_fails_closed(self, pricing, type_, after):
        """BUG-3 (fixed): negative GB counts are impossible infrastructure and a
        cost-offset vector against the R14 gate -> attributes_unknown, never a
        negative delta (spec silent; direction flagged as S4)."""
        assert unpriced_reason(pricing, type_=type_, after=after) == (
            UnpricedReason.ATTRIBUTES_UNKNOWN
        )

    def test_r7_zero_size_prices_to_zero(self, pricing):
        """Zero GB is priceable and harmless: 0.00, not unpriced."""
        plan = Plan.model_validate(
            make_plan(
                [
                    make_change(
                        address="aws_ebs_volume.zero",
                        type_="aws_ebs_volume",
                        actions=["create"],
                        after={"type": "gp2", "size": 0},
                    )
                ]
            )
        )
        report = estimate(plan, pricing, "us-east-1")
        assert report.unpriced == ()
        assert str(report.breakdown[0].monthly_delta_usd) == "0.00"

    def test_r7_huge_size_does_not_crash_or_lose_precision(self, pricing, runner, tmp_path):
        """A very large (but finite) size stays exact Decimal math."""
        from decimal import ROUND_HALF_UP, Decimal

        from .conftest import load_snapshot

        snapshot = load_snapshot()
        gp3 = Decimal(snapshot["regions"]["us-east-1"]["aws_ebs_volume"]["gp3"])
        size = 10**15
        expected = (gp3 * Decimal(size)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        path = write_plan(
            tmp_path,
            make_plan(
                [
                    make_change(
                        address="aws_ebs_volume.huge",
                        type_="aws_ebs_volume",
                        actions=["create"],
                        after={"type": "gp3", "size": size},
                    )
                ],
                provider_region="us-east-1",
            ),
        )
        result = run_analyze(runner, path)
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["cost"]["monthly_delta_usd"] == str(expected)
