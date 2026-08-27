"""R8: pricing region resolution — --region wins; else the plan's constant
provider region; else exit 2 telling the user to pass --region; a region
absent from the snapshot exits 2 naming it and the supported regions.

Covers AC12 and the coder's A-i10 (sorted provider_config scan order).
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal

import pytest

from spend_sentinel.core.cost import HOURS_PER_MONTH
from spend_sentinel.core.models import Plan
from spend_sentinel.core.plan import load_plan, resolve_plan_region

from .conftest import (
    fixture_path,
    load_snapshot,
    make_change,
    make_plan,
    run_analyze,
    write_plan,
)

SNAPSHOT = load_snapshot()


def instance_monthly(region: str, itype: str) -> str:
    raw = Decimal(SNAPSHOT["regions"][region]["aws_instance"][itype]) * HOURS_PER_MONTH
    return str(raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def plan_with_providers(provider_config) -> Plan:
    return Plan.model_validate(
        {
            "format_version": "1.2",
            "resource_changes": [],
            "configuration": {"provider_config": provider_config},
        }
    )


class TestResolvePlanRegionUnit:
    def test_r8_constant_region_resolved(self):
        plan = load_plan(fixture_path("region_constant_eu_west.json"))
        assert resolve_plan_region(plan) == "eu-west-1"

    def test_r8_no_configuration_returns_none(self):
        plan = Plan.model_validate({"format_version": "1.2", "resource_changes": []})
        assert resolve_plan_region(plan) is None

    def test_r8_empty_provider_config_returns_none(self):
        assert resolve_plan_region(plan_with_providers({})) is None

    def test_r8_non_constant_region_expression_returns_none(self):
        """A region from a variable reference is not a constant (spec R8)."""
        plan = plan_with_providers(
            {
                "aws": {
                    "name": "aws",
                    "expressions": {"region": {"references": ["var.region"]}},
                }
            }
        )
        assert resolve_plan_region(plan) is None

    def test_r8_non_string_constant_ignored(self):
        plan = plan_with_providers(
            {"aws": {"name": "aws", "expressions": {"region": {"constant_value": 7}}}}
        )
        assert resolve_plan_region(plan) is None

    def test_r8_non_aws_provider_region_ignored(self):
        plan = plan_with_providers(
            {
                "google": {
                    "name": "google",
                    "expressions": {"region": {"constant_value": "europe-west1"}},
                }
            }
        )
        assert resolve_plan_region(plan) is None

    def test_r8_aliased_aws_provider_resolves(self):
        plan = plan_with_providers(
            {
                "aws.west": {
                    "name": "aws",
                    "alias": "west",
                    "expressions": {"region": {"constant_value": "us-west-2"}},
                }
            }
        )
        assert resolve_plan_region(plan) == "us-west-2"

    def test_r8_primary_provider_wins_over_alias_ai10(self):
        """A-i10: sorted scan — the root `aws` entry sorts before `aws.z`."""
        plan = plan_with_providers(
            {
                "aws.z": {
                    "name": "aws",
                    "alias": "z",
                    "expressions": {"region": {"constant_value": "us-west-2"}},
                },
                "aws": {
                    "name": "aws",
                    "expressions": {"region": {"constant_value": "eu-west-1"}},
                },
            }
        )
        assert resolve_plan_region(plan) == "eu-west-1"

    def test_r8_alias_with_constant_beats_primary_without(self):
        plan = plan_with_providers(
            {
                "aws": {
                    "name": "aws",
                    "expressions": {"region": {"references": ["var.region"]}},
                },
                "aws.backup": {
                    "name": "aws",
                    "alias": "backup",
                    "expressions": {"region": {"constant_value": "us-west-2"}},
                },
            }
        )
        assert resolve_plan_region(plan) == "us-west-2"


class TestRegionThroughCliAc12:
    def test_r8_ac12_plan_constant_region_prices_apply(self, runner):
        """AC12: constant eu-west-1, no --region -> eu-west-1 prices used."""
        result = run_analyze(runner, fixture_path("region_constant_eu_west.json"))
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["cost"]["region"] == "eu-west-1"
        assert payload["cost"]["monthly_delta_usd"] == instance_monthly("eu-west-1", "t3.micro")
        # meaningful only if the two regions actually price differently
        assert instance_monthly("eu-west-1", "t3.micro") != instance_monthly(
            "us-east-1", "t3.micro"
        )

    def test_r8_region_flag_overrides_plan_constant(self, runner):
        result = run_analyze(
            runner, fixture_path("region_constant_eu_west.json"), "--region", "us-east-1"
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["cost"]["region"] == "us-east-1"
        assert payload["cost"]["monthly_delta_usd"] == instance_monthly("us-east-1", "t3.micro")

    def test_r8_ac12_no_resolvable_region_exit_2_mentions_flag(self, runner, tmp_path):
        """AC12: no constant region, no flag -> exit 2 telling the user to
        pass --region, naming the plan file, one line, nothing on stdout."""
        path = write_plan(
            tmp_path,
            make_plan([make_change(actions=["create"], after={"instance_type": "t3.micro"})]),
        )
        result = run_analyze(runner, path)
        assert result.exit_code == 2
        assert result.stdout == ""
        lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert "--region" in lines[0]
        assert path in lines[0]

    @pytest.mark.parametrize("via_flag", [True, False])
    def test_r8_unknown_region_exit_2_names_region_and_supported(
        self, runner, tmp_path, via_flag
    ):
        """R8: a region absent from the snapshot exits 2 naming the region and
        the snapshot's supported regions — whether it came from --region or
        from the plan's constant."""
        region = "mars-north-1"
        if via_flag:
            path = write_plan(tmp_path, make_plan([]))
            result = run_analyze(runner, path, "--region", region)
        else:
            path = write_plan(tmp_path, make_plan([], provider_region=region))
            result = run_analyze(runner, path)
        assert result.exit_code == 2
        assert result.stdout == ""
        lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert region in lines[0]
        for supported in sorted(SNAPSHOT["regions"]):
            assert supported in lines[0]

    def test_r8_plan_errors_take_precedence_over_region_errors(self, runner, tmp_path):
        """A malformed plan reports the R2 diagnostic, not a region complaint."""
        p = tmp_path / "broken.json"
        p.write_text("{not json")
        result = run_analyze(runner, str(p), "--region", "mars-north-1")
        assert result.exit_code == 2
        assert "not valid JSON" in result.stderr
        assert "mars-north-1" not in result.stderr

    def test_r8_us_west_2_prices_used_when_flagged(self, runner, tmp_path):
        path = write_plan(
            tmp_path,
            make_plan(
                [
                    make_change(
                        address="aws_instance.w",
                        actions=["create"],
                        after={"instance_type": "m5.large"},
                    )
                ]
            ),
        )
        result = run_analyze(runner, path, "--region", "us-west-2")
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["cost"]["region"] == "us-west-2"
        assert payload["cost"]["monthly_delta_usd"] == instance_monthly("us-west-2", "m5.large")
