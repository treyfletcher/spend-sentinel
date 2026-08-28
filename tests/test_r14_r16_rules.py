"""R14 (max_monthly_delta) and R16 (deletions) rule evaluators, table-driven
over constructed CostReport/Plan inputs; AC6 matrix through the CLI.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from spend_sentinel.core.models import (
    CostReport,
    Plan,
    RuleOutcome,
    UnpricedReason,
    UnpricedResource,
)
from spend_sentinel.core.policy import Policy, _eval_deletions, _eval_max_monthly_delta

from .conftest import make_change, make_plan, run_analyze_json, write_plan


def policy_with(**rules) -> Policy:
    return Policy.model_validate({"rules": rules})


def cost_report(delta: str, unpriced: int = 0) -> CostReport:
    return CostReport(
        monthly_delta_usd=Decimal(delta),
        breakdown=(),
        unpriced=tuple(
            UnpricedResource(
                address=f"aws_lambda_function.u{i}",
                type="aws_lambda_function",
                reason=UnpricedReason.UNSUPPORTED_TYPE,
            )
            for i in range(unpriced)
        ),
    )


class TestMaxMonthlyDeltaR14:
    def eval_(self, cost, **rule_cfg):
        policy = policy_with(max_monthly_delta=rule_cfg)
        return _eval_max_monthly_delta(policy.rules.max_monthly_delta, cost)

    def test_r14_block_when_over_limit_by_a_cent(self):
        result = self.eval_(cost_report("200.01"))
        assert result.result is RuleOutcome.BLOCK
        assert "200.01" in result.message
        assert "200" in result.message

    def test_r14_pass_at_exactly_the_limit(self):
        """Spec says BLOCK when delta *exceeds* the limit: 200.00 == 200 passes."""
        result = self.eval_(cost_report("200.00"))
        assert result.result is RuleOutcome.PASS

    def test_r14_pass_under_limit(self):
        assert self.eval_(cost_report("199.99")).result is RuleOutcome.PASS

    def test_r14_negative_delta_passes(self):
        assert self.eval_(cost_report("-5000.00")).result is RuleOutcome.PASS

    def test_r14_custom_limit_respected(self):
        assert self.eval_(cost_report("50.01"), limit_usd=50).result is RuleOutcome.BLOCK
        assert self.eval_(cost_report("50.00"), limit_usd=50).result is RuleOutcome.PASS

    def test_r14_null_limit_means_no_ceiling(self):
        result = self.eval_(cost_report("999999.99"), limit_usd=None)
        assert result.result is RuleOutcome.PASS
        assert "no limit" in result.message

    @pytest.mark.parametrize(
        ("treat", "expected"),
        [
            ("warn", RuleOutcome.WARN),
            ("block", RuleOutcome.BLOCK),
            ("ignore", RuleOutcome.PASS),
        ],
    )
    def test_r14_unpriced_escalation_under_limit(self, treat, expected):
        """AC-relevant matrix: unpriced resources escalate an under-limit result
        to at least warn/block; ignore leaves it at pass."""
        result = self.eval_(cost_report("10.00", unpriced=2), treat_unpriced_as=treat)
        assert result.result is expected

    def test_r14_unpriced_block_escalation_message_names_resources(self):
        result = self.eval_(cost_report("10.00", unpriced=2),
                            treat_unpriced_as="block")
        assert result.result is RuleOutcome.BLOCK
        assert "aws_lambda_function.u0" in result.message
        assert "treat_unpriced_as: block" in result.message

    def test_r14_over_limit_stays_block_with_warn_unpriced(self):
        """Escalation never downgrades: BLOCK from the limit survives a warn-
        level unpriced escalation."""
        result = self.eval_(cost_report("300.00", unpriced=1),
                            treat_unpriced_as="warn")
        assert result.result is RuleOutcome.BLOCK

    def test_r14_unpriced_with_null_limit_still_escalates(self):
        result = self.eval_(cost_report("10.00", unpriced=1), limit_usd=None,
                            treat_unpriced_as="block")
        assert result.result is RuleOutcome.BLOCK

    def test_r14_ignored_unpriced_still_mentioned(self):
        """A-i26: ignore renders pass, and the message says so."""
        result = self.eval_(cost_report("10.00", unpriced=3),
                            treat_unpriced_as="ignore")
        assert result.result is RuleOutcome.PASS
        assert "ignored by policy" in result.message


def plan_of(*changes) -> Plan:
    return Plan.model_validate(make_plan(list(changes)))


DELETE_DB = make_change(
    address="aws_db_instance.db",
    type_="aws_db_instance",
    actions=["delete"],
    before={"instance_class": "db.t3.micro"},
)
REPLACE_INSTANCE = make_change(
    address="aws_instance.repl",
    actions=["delete", "create"],
    before={"instance_type": "t3.micro"},
    after={"instance_type": "t3.small"},
)
CREATE_ONLY = make_change(
    address="aws_instance.new", actions=["create"], after={"instance_type": "t3.micro"}
)


class TestDeletionsR16:
    def eval_(self, plan, **rule_cfg):
        policy = policy_with(deletions=rule_cfg)
        return _eval_deletions(policy.rules.deletions, plan)

    def test_r16_no_deletions_passes(self):
        result = self.eval_(plan_of(CREATE_ONLY))
        assert result.result is RuleOutcome.PASS

    @pytest.mark.parametrize(
        ("action", "expected"),
        [
            ("warn", RuleOutcome.WARN),
            ("block", RuleOutcome.BLOCK),
            ("ignore", RuleOutcome.PASS),
        ],
    )
    def test_r16_action_matrix(self, action, expected):
        result = self.eval_(plan_of(DELETE_DB), action=action)
        assert result.result is expected
        assert "aws_db_instance.db" in result.message

    def test_r16_replaces_count_as_deletions(self):
        result = self.eval_(plan_of(REPLACE_INSTANCE))
        assert result.result is RuleOutcome.WARN
        assert "aws_instance.repl" in result.message

    def test_r16_delete_and_replace_both_listed(self):
        result = self.eval_(plan_of(DELETE_DB, REPLACE_INSTANCE, CREATE_ONLY))
        assert result.result is RuleOutcome.WARN
        assert "2 deletion(s)" in result.message
        assert "aws_db_instance.db" in result.message
        assert "aws_instance.repl" in result.message
        assert "aws_instance.new" not in result.message

    @pytest.mark.parametrize("action", ["warn", "block", "ignore"])
    def test_r16_protected_type_blocks_regardless_of_action(self, action):
        """AC6: protected_types beats action — including ignore."""
        result = self.eval_(
            plan_of(DELETE_DB), action=action, protected_types=["aws_db_instance"]
        )
        assert result.result is RuleOutcome.BLOCK
        assert "aws_db_instance.db" in result.message
        assert "protected" in result.message

    def test_r16_protected_replace_blocks(self):
        result = self.eval_(
            plan_of(REPLACE_INSTANCE), action="ignore", protected_types=["aws_instance"]
        )
        assert result.result is RuleOutcome.BLOCK

    def test_r16_unprotected_type_not_blocked_by_protected_list(self):
        result = self.eval_(
            plan_of(DELETE_DB), action="warn", protected_types=["aws_s3_bucket"]
        )
        assert result.result is RuleOutcome.WARN

    def test_r16_many_deletions_elided_in_message(self):
        changes = [
            make_change(address=f"aws_instance.x{i}", actions=["delete"],
                        before={"instance_type": "t3.micro"})
            for i in range(8)
        ]
        result = self.eval_(plan_of(*changes))
        assert "and 3 more" in result.message


class TestAc6ThroughCli:
    @pytest.fixture()
    def delete_plan(self, tmp_path):
        return write_plan(
            tmp_path,
            make_plan([DELETE_DB], provider_region="us-east-1"),
            name="ac6_plan.json",
        )

    @staticmethod
    def rule(payload, name):
        return {r["name"]: r for r in payload["policy"]["rules"]}[name]

    def test_ac6_default_warn_exits_0(self, runner, delete_plan, tmp_path, monkeypatch):
        """AC6: deletion under warn -> WARN verdict, exit 0."""
        monkeypatch.chdir(tmp_path)  # no spend-sentinel.yaml here
        result, payload = run_analyze_json(runner, delete_plan)
        assert result.exit_code == 0
        assert payload["verdict"] == "WARN"
        rule = self.rule(payload, "deletions")
        assert rule["result"] == "warn"
        assert "aws_db_instance.db" in rule["message"]

    def test_ac6_warn_with_fail_on_warn_exits_1(self, runner, delete_plan, tmp_path,
                                                monkeypatch):
        """AC6: the same WARN exits 1 under --fail-on-warn (R18)."""
        monkeypatch.chdir(tmp_path)
        result, payload = run_analyze_json(runner, delete_plan, "--fail-on-warn")
        assert result.exit_code == 1
        assert payload["verdict"] == "WARN"  # the verdict itself stays WARN

    def test_ac6_protected_type_blocks_despite_ignore(self, runner, delete_plan,
                                                      tmp_path):
        """AC6: protected_types -> BLOCK verdict, exit 1 (R18)."""
        policy = tmp_path / "prot.yaml"
        policy.write_text(
            "rules:\n  deletions:\n    action: ignore\n"
            "    protected_types: [aws_db_instance]\n"
        )
        result, payload = run_analyze_json(runner, delete_plan, "--policy", str(policy))
        assert result.exit_code == 1
        assert payload["verdict"] == "BLOCK"
        assert self.rule(payload, "deletions")["result"] == "block"
