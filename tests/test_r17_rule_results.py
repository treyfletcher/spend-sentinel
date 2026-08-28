"""R17: every rule evaluation appears in the verdict with name, result, and a
human-readable message naming the offending resources; drift rule is `skipped`
when drift did not run (R11) and reacts per config when it did (A-i25 errors
suffix, A-i26 ignore-renders-pass); messages are sanitized.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from spend_sentinel.core.models import (
    CostReport,
    Drift,
    DriftError,
    DriftKind,
    DriftReport,
    DriftStatus,
    Plan,
    RuleOutcome,
)
from spend_sentinel.core.policy import Policy, _eval_drift, evaluate

from .conftest import make_change, make_plan, run_analyze, write_plan

RULE_NAMES = ["max_monthly_delta", "open_ingress", "deletions", "drift"]


def empty_cost() -> CostReport:
    return CostReport(monthly_delta_usd=Decimal("0.00"), breakdown=(), unpriced=())


def ran_drift(*addresses, errors=0) -> DriftReport:
    return DriftReport(
        status=DriftStatus.RAN,
        drifts=tuple(
            Drift(address=a, kind=DriftKind.CHANGED, attribute="tags",
                  state_value={}, live_value={"x": "y"})
            for a in addresses
        ),
        errors=tuple(
            DriftError(address=f"aws_instance.err{i}", error="AuthFailure: denied")
            for i in range(errors)
        ),
    )


class TestEveryRulePresent:
    def test_r17_all_four_rules_always_present_with_fields(self):
        results = evaluate(
            Policy(), empty_cost(), DriftReport(status=DriftStatus.SKIPPED),
            Plan.model_validate(make_plan([])),
        )
        assert [r.name for r in results] == RULE_NAMES
        for r in results:
            assert r.result in set(RuleOutcome)
            assert isinstance(r.message, str) and r.message

    def test_r17_cli_policy_section_lists_all_rules(self, runner, tmp_path):
        plan_path = write_plan(
            tmp_path,
            make_plan(
                [make_change(actions=["create"], after={"instance_type": "t3.micro"})],
                provider_region="us-east-1",
            ),
        )
        result = run_analyze(runner, plan_path)
        assert result.exit_code == 0
        rules = json.loads(result.stdout)["policy"]["rules"]
        assert [r["name"] for r in rules] == RULE_NAMES
        assert all(set(r) == {"name", "result", "message"} for r in rules)


class TestDriftRule:
    def eval_(self, drift, action="warn"):
        policy = Policy.model_validate({"rules": {"drift": {"action": action}}})
        return _eval_drift(policy.rules.drift, drift)

    def test_r17_drift_rule_skipped_when_drift_did_not_run(self):
        result = self.eval_(DriftReport(status=DriftStatus.SKIPPED))
        assert result.result is RuleOutcome.SKIPPED
        assert "did not run" in result.message

    def test_r17_drift_rule_pass_when_ran_clean(self):
        result = self.eval_(ran_drift())
        assert result.result is RuleOutcome.PASS

    @pytest.mark.parametrize(
        ("action", "expected"),
        [
            ("warn", RuleOutcome.WARN),
            ("block", RuleOutcome.BLOCK),
            ("ignore", RuleOutcome.PASS),
        ],
    )
    def test_r17_drift_rule_action_matrix(self, action, expected):
        result = self.eval_(ran_drift("aws_instance.web"), action=action)
        assert result.result is expected
        assert "aws_instance.web" in result.message

    def test_r17_drift_ignore_renders_pass_with_ignored_message_a_i26(self):
        result = self.eval_(ran_drift("aws_instance.web"), action="ignore")
        assert result.result is RuleOutcome.PASS
        assert "ignored by policy" in result.message

    def test_r17_drift_errors_appear_as_count_suffix_a_i25(self):
        result = self.eval_(ran_drift("aws_instance.web", errors=2))
        assert "2 read error(s)" in result.message
        # errors alone (no drifts) still surface the count on a pass
        clean = self.eval_(ran_drift(errors=1))
        assert clean.result is RuleOutcome.PASS
        assert "1 read error(s)" in clean.message

    def test_r17_drift_addresses_deduplicated(self):
        drift = DriftReport(
            status=DriftStatus.RAN,
            drifts=(
                Drift(address="aws_instance.web", kind=DriftKind.CHANGED,
                      attribute="tags"),
                Drift(address="aws_instance.web", kind=DriftKind.CHANGED,
                      attribute="instance_type"),
            ),
        )
        result = self.eval_(drift)
        assert result.message.count("aws_instance.web") == 1
        assert "2 drift(s)" in result.message


class TestMessageSanitization:
    def test_r17_hostile_address_control_chars_stripped_in_messages(self):
        hostile = 'aws_instance.x\n"policy": []\x1b[31m'
        plan = Plan.model_validate(
            make_plan(
                [
                    make_change(
                        address=hostile,
                        actions=["delete"],
                        before={"instance_type": "t3.micro"},
                    )
                ]
            )
        )
        results = evaluate(
            Policy(), empty_cost(), DriftReport(status=DriftStatus.SKIPPED), plan
        )
        deletions = {r.name: r for r in results}["deletions"]
        assert "\n" not in deletions.message
        assert "\x1b" not in deletions.message
        assert "aws_instance.x" in deletions.message  # printable part survives

    def test_r17_hostile_drift_address_sanitized(self):
        result = _eval_drift(
            Policy().rules.drift, ran_drift("aws_instance.y\r\nspoof")
        )
        assert "\n" not in result.message
        assert "\r" not in result.message

    def test_r17_long_offender_lists_elided(self):
        plan = Plan.model_validate(
            make_plan(
                [
                    make_change(address=f"aws_instance.d{i}", actions=["delete"],
                                before={"instance_type": "t3.micro"})
                    for i in range(9)
                ]
            )
        )
        results = evaluate(
            Policy(), empty_cost(), DriftReport(status=DriftStatus.SKIPPED), plan
        )
        message = {r.name: r for r in results}["deletions"].message
        assert "and 4 more" in message

    def test_r17_hostile_address_never_multiline_in_cli_json(self, runner, tmp_path):
        hostile = "aws_instance.evil\u2028line\u0085x"  # unicode line separators
        plan_path = write_plan(
            tmp_path,
            make_plan(
                [
                    make_change(
                        address=hostile,
                        actions=["delete"],
                        before={"instance_type": "t3.micro"},
                    )
                ],
                provider_region="us-east-1",
            ),
        )
        result = run_analyze(runner, plan_path)
        assert result.exit_code == 0
        rules = {r["name"]: r for r in
                 json.loads(result.stdout)["policy"]["rules"]}
        message = rules["deletions"]["message"]
        assert "\u2028" not in message
        assert "\u0085" not in message
        assert "aws_instance.evil" in message
