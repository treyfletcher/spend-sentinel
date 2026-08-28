"""R15: open_ingress — BLOCK on 0.0.0.0/0 or ::/0 ingress unless every port in
the rule's range is allowed. AC5 matrix plus the coder's A-i22 (replaced SGs
inspected) and A-i23 (unverifiable ranges fail closed), and spec A7 (SG
references and prefix lists never open).
"""

from __future__ import annotations

import json

import pytest

from spend_sentinel.core.models import Plan, RuleOutcome
from spend_sentinel.core.policy import Policy, _eval_open_ingress

from .conftest import make_change, make_plan, run_analyze, write_plan


def evaluate_open_ingress(changes, allowed_ports=()):
    policy = Policy.model_validate(
        {"rules": {"open_ingress": {"allowed_ports": list(allowed_ports)}}}
    )
    plan = Plan.model_validate(make_plan(list(changes)))
    return _eval_open_ingress(policy.rules.open_ingress, plan)


def sg_change(ingress, actions=None, address="aws_security_group.app"):
    return make_change(
        address=address,
        type_="aws_security_group",
        actions=actions or ["create"],
        after={"name": "app", "ingress": ingress, "egress": []},
    )


def ingress_rule(from_port, to_port, protocol="tcp", cidrs=("0.0.0.0/0",), v6=()):
    return {
        "protocol": protocol,
        "from_port": from_port,
        "to_port": to_port,
        "cidr_blocks": list(cidrs),
        "ipv6_cidr_blocks": list(v6),
    }


class TestAc5Matrix:
    def test_ac5_port_22_open_blocks_naming_address_and_port(self):
        result = evaluate_open_ingress(
            [sg_change([ingress_rule(22, 22)])], allowed_ports=[80, 443]
        )
        assert result.result is RuleOutcome.BLOCK
        assert "aws_security_group.app" in result.message
        assert "22" in result.message

    def test_ac5_allowed_port_443_passes(self):
        result = evaluate_open_ingress(
            [sg_change([ingress_rule(443, 443)])], allowed_ports=[80, 443]
        )
        assert result.result is RuleOutcome.PASS

    def test_r15_range_spanning_non_allowed_port_blocks(self):
        """Range 80-81 with only 80 (and 443) allowed: 81 is not allowed."""
        result = evaluate_open_ingress(
            [sg_change([ingress_rule(80, 81)])], allowed_ports=[80, 443]
        )
        assert result.result is RuleOutcome.BLOCK

    def test_r15_range_fully_allowed_passes(self):
        result = evaluate_open_ingress(
            [sg_change([ingress_rule(80, 81)])], allowed_ports=[80, 81]
        )
        assert result.result is RuleOutcome.PASS

    def test_r15_protocol_minus_1_blocks_even_with_allowed_ports(self):
        result = evaluate_open_ingress(
            [sg_change([ingress_rule(0, 0, protocol="-1")])],
            allowed_ports=[0, 80, 443],
        )
        assert result.result is RuleOutcome.BLOCK
        assert "-1" in result.message

    def test_r15_ipv6_open_cidr_blocks(self):
        result = evaluate_open_ingress(
            [sg_change([ingress_rule(22, 22, cidrs=(), v6=("::/0",))])]
        )
        assert result.result is RuleOutcome.BLOCK

    def test_r15_non_open_cidr_passes(self):
        result = evaluate_open_ingress(
            [sg_change([ingress_rule(22, 22, cidrs=("10.0.0.0/8",))])]
        )
        assert result.result is RuleOutcome.PASS

    def test_r15_sg_reference_never_open_a7(self):
        rule = {
            "protocol": "tcp",
            "from_port": 22,
            "to_port": 22,
            "cidr_blocks": [],
            "ipv6_cidr_blocks": [],
            "security_groups": ["sg-12345"],
        }
        result = evaluate_open_ingress([sg_change([rule])])
        assert result.result is RuleOutcome.PASS

    def test_r15_prefix_list_never_open_a7(self):
        rule = {
            "protocol": "tcp",
            "from_port": 22,
            "to_port": 22,
            "cidr_blocks": [],
            "ipv6_cidr_blocks": [],
            "prefix_list_ids": ["pl-12345"],
        }
        result = evaluate_open_ingress([sg_change([rule])])
        assert result.result is RuleOutcome.PASS


class TestActionsScope:
    def test_r15_updated_sg_inspected(self):
        result = evaluate_open_ingress(
            [sg_change([ingress_rule(22, 22)], actions=["update"])]
        )
        assert result.result is RuleOutcome.BLOCK

    @pytest.mark.parametrize("actions", [["delete", "create"], ["create", "delete"]])
    def test_r15_replaced_sg_inspected_a_i22(self, actions):
        """A-i22: a replaced SG exists after apply — inspected like a create."""
        result = evaluate_open_ingress([sg_change([ingress_rule(22, 22)],
                                                  actions=actions)])
        assert result.result is RuleOutcome.BLOCK

    def test_r15_deleted_sg_exempt(self):
        change = make_change(
            address="aws_security_group.gone",
            type_="aws_security_group",
            actions=["delete"],
            before={"ingress": [ingress_rule(22, 22)]},
        )
        result = evaluate_open_ingress([change])
        assert result.result is RuleOutcome.PASS

    def test_r15_noop_sg_exempt(self):
        change = make_change(
            address="aws_security_group.same",
            type_="aws_security_group",
            actions=["no-op"],
            before={"ingress": [ingress_rule(22, 22)]},
            after={"ingress": [ingress_rule(22, 22)]},
        )
        result = evaluate_open_ingress([change])
        assert result.result is RuleOutcome.PASS

    def test_r15_non_sg_types_ignored(self):
        change = make_change(
            address="aws_instance.web",
            actions=["create"],
            after={"instance_type": "t3.micro",
                   "ingress": [ingress_rule(22, 22)]},  # decoy
        )
        result = evaluate_open_ingress([change])
        assert result.result is RuleOutcome.PASS


class TestStandaloneRuleResources:
    def test_r15_sg_rule_resource_ingress_open_blocks(self):
        change = make_change(
            address="aws_security_group_rule.open_ssh",
            type_="aws_security_group_rule",
            actions=["create"],
            after={
                "type": "ingress",
                "protocol": "tcp",
                "from_port": 22,
                "to_port": 22,
                "cidr_blocks": ["0.0.0.0/0"],
            },
        )
        result = evaluate_open_ingress([change])
        assert result.result is RuleOutcome.BLOCK
        assert "aws_security_group_rule.open_ssh" in result.message

    def test_r15_sg_rule_resource_egress_exempt(self):
        change = make_change(
            address="aws_security_group_rule.out",
            type_="aws_security_group_rule",
            actions=["create"],
            after={
                "type": "egress",
                "protocol": "tcp",
                "from_port": 22,
                "to_port": 22,
                "cidr_blocks": ["0.0.0.0/0"],
            },
        )
        result = evaluate_open_ingress([change])
        assert result.result is RuleOutcome.PASS

    def test_r15_vpc_ingress_rule_resource_open_v4_blocks(self):
        change = make_change(
            address="aws_vpc_security_group_ingress_rule.open",
            type_="aws_vpc_security_group_ingress_rule",
            actions=["create"],
            after={
                "ip_protocol": "tcp",
                "from_port": 22,
                "to_port": 22,
                "cidr_ipv4": "0.0.0.0/0",
            },
        )
        result = evaluate_open_ingress([change])
        assert result.result is RuleOutcome.BLOCK

    def test_r15_vpc_ingress_rule_resource_open_v6_blocks(self):
        change = make_change(
            address="aws_vpc_security_group_ingress_rule.open6",
            type_="aws_vpc_security_group_ingress_rule",
            actions=["create"],
            after={
                "ip_protocol": "tcp",
                "from_port": 8080,
                "to_port": 8080,
                "cidr_ipv6": "::/0",
            },
        )
        result = evaluate_open_ingress([change])
        assert result.result is RuleOutcome.BLOCK

    def test_r15_vpc_ingress_rule_allowed_port_passes(self):
        change = make_change(
            address="aws_vpc_security_group_ingress_rule.https",
            type_="aws_vpc_security_group_ingress_rule",
            actions=["create"],
            after={
                "ip_protocol": "tcp",
                "from_port": 443,
                "to_port": 443,
                "cidr_ipv4": "0.0.0.0/0",
            },
        )
        result = evaluate_open_ingress([change], allowed_ports=[443])
        assert result.result is RuleOutcome.PASS

    def test_r15_vpc_ingress_rule_protocol_minus_1_blocks(self):
        change = make_change(
            address="aws_vpc_security_group_ingress_rule.all",
            type_="aws_vpc_security_group_ingress_rule",
            actions=["create"],
            after={"ip_protocol": "-1", "cidr_ipv4": "0.0.0.0/0"},
        )
        result = evaluate_open_ingress([change], allowed_ports=[80, 443])
        assert result.result is RuleOutcome.BLOCK


class TestFailClosedRanges:
    @pytest.mark.parametrize(
        "rule",
        [
            ingress_rule(None, 22),                       # missing from_port
            ingress_rule(22, None),                       # missing to_port
            ingress_rule("22", "22"),                     # non-integer ports
            ingress_rule(443, 80),                        # inverted range
        ],
    )
    def test_r15_unverifiable_range_blocks_a_i23(self, rule):
        result = evaluate_open_ingress([sg_change([rule])],
                                       allowed_ports=[22, 80, 443])
        assert result.result is RuleOutcome.BLOCK

    def test_r15_wide_range_blocks_fast(self):
        result = evaluate_open_ingress(
            [sg_change([ingress_rule(0, 65535)])], allowed_ports=[80, 443]
        )
        assert result.result is RuleOutcome.BLOCK

    def test_r15_multiple_offenders_all_listed(self):
        changes = [
            sg_change([ingress_rule(22, 22)], address="aws_security_group.a"),
            sg_change([ingress_rule(3306, 3306)], address="aws_security_group.b"),
        ]
        result = evaluate_open_ingress(changes)
        assert result.result is RuleOutcome.BLOCK
        assert "aws_security_group.a" in result.message
        assert "aws_security_group.b" in result.message


class TestAc5ThroughCli:
    def test_ac5_cli_block_and_pass(self, runner, tmp_path):
        plan_path = write_plan(
            tmp_path,
            make_plan(
                [sg_change([ingress_rule(22, 22)])], provider_region="us-east-1"
            ),
            name="ac5_plan.json",
        )
        policy = tmp_path / "ac5.yaml"
        policy.write_text("rules:\n  open_ingress:\n    allowed_ports: [80, 443]\n")
        result = run_analyze(runner, plan_path, "--policy", str(policy))
        assert result.exit_code == 0  # informational until R18
        rules = {r["name"]: r for r in
                 json.loads(result.stdout)["policy"]["rules"]}
        assert rules["open_ingress"]["result"] == "block"
        assert "aws_security_group.app" in rules["open_ingress"]["message"]
        assert "22" in rules["open_ingress"]["message"]

        # same plan, rule only on 443, allowed
        plan443 = write_plan(
            tmp_path,
            make_plan(
                [sg_change([ingress_rule(443, 443)])], provider_region="us-east-1"
            ),
            name="ac5_443.json",
        )
        result2 = run_analyze(runner, plan443, "--policy", str(policy))
        rules2 = {r["name"]: r for r in
                  json.loads(result2.stdout)["policy"]["rules"]}
        assert rules2["open_ingress"]["result"] == "pass"
