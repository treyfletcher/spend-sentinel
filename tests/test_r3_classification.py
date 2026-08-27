"""R3: no-op exclusion and create/delete/update/replace classification.

Also covers the coder's stated assumptions: data-source ["read"] treated as a
no-op (A-i1), ["create","delete"] classified as replace (A-i2), and any other
action combination failing closed with exit 2.
"""

from __future__ import annotations

import json

import pytest

from spend_sentinel.core.models import ActionClass
from spend_sentinel.core.plan import PlanError, classify_actions, load_plan, summarize_plan

from .conftest import fixture_path, make_change, make_plan, run_analyze, write_plan


class TestClassifyActionsUnit:
    @pytest.mark.parametrize(
        ("actions", "expected"),
        [
            (["create"], ActionClass.CREATE),
            (["delete"], ActionClass.DELETE),
            (["update"], ActionClass.UPDATE),
            (["delete", "create"], ActionClass.REPLACE),
            (["create", "delete"], ActionClass.REPLACE),  # A-i2 create-before-destroy
        ],
    )
    def test_r3_recognized_combinations(self, actions, expected):
        assert classify_actions(actions) == expected

    @pytest.mark.parametrize("actions", [["no-op"], ["read"]])
    def test_r3_excluded_actions_return_none(self, actions):
        assert classify_actions(actions) is None

    @pytest.mark.parametrize(
        "actions",
        [
            [],
            ["forget"],
            ["create", "create"],
            ["delete", "delete"],
            ["create", "update"],
            ["no-op", "create"],
            ["read", "delete"],
            ["CREATE"],  # case-sensitive: not a Terraform action string
            ["delete", "create", "delete"],
        ],
    )
    def test_r3_unrecognized_combinations_fail_closed(self, actions):
        with pytest.raises(PlanError):
            classify_actions(actions)


class TestSummarizePlan:
    def test_r3_mixed_plan_counts(self):
        plan = load_plan(fixture_path("mixed_actions.json"))
        summary, classified = summarize_plan(plan)
        assert summary.created == 1
        assert summary.deleted == 1
        assert summary.updated == 1
        assert summary.replaced == 1
        assert summary.changed == 4
        # no-op and read entries are excluded from the classified list
        addresses = [c.address for c in classified]
        assert "aws_s3_bucket.untouched" not in addresses
        assert "data.aws_ami.latest" not in addresses
        assert len(classified) == 4

    def test_r3_replace_delete_create_counted(self):
        plan = load_plan(fixture_path("mixed_actions.json"))
        _, classified = summarize_plan(plan)
        replaced = [c for c in classified if c.action == ActionClass.REPLACE]
        assert [c.address for c in replaced] == ["aws_db_instance.replaced"]

    def test_r3_replace_create_before_destroy_counted(self):
        plan = load_plan(fixture_path("replace_create_before_destroy.json"))
        summary, classified = summarize_plan(plan)
        assert summary.replaced == 1
        assert classified[0].action == ActionClass.REPLACE

    def test_r3_noop_only_plan_all_counts_zero(self):
        plan = load_plan(fixture_path("noop_only.json"))
        summary, classified = summarize_plan(plan)
        assert summary.changed == 0
        assert classified == []

    def test_r3_empty_resource_changes_all_counts_zero(self):
        """A-i6: empty resource_changes is valid, not an error."""
        plan = load_plan(fixture_path("empty_changes.json"))
        summary, classified = summarize_plan(plan)
        assert (summary.created, summary.deleted, summary.updated, summary.replaced) == (
            0,
            0,
            0,
            0,
        )
        assert summary.changed == 0
        assert classified == []

    def test_r3_classified_order_follows_plan_order(self):
        plan = load_plan(fixture_path("mixed_actions.json"))
        _, classified = summarize_plan(plan)
        assert [c.address for c in classified] == [
            "aws_instance.created",
            "aws_instance.deleted",
            "aws_instance.updated",
            "aws_db_instance.replaced",
        ]

    def test_r3_unknown_action_error_names_resource(self):
        plan = load_plan(fixture_path("unknown_action.json"))
        with pytest.raises(PlanError) as excinfo:
            summarize_plan(plan)
        assert "aws_instance.forgotten" in str(excinfo.value)


class TestCliR3:
    def test_r3_cli_mixed_plan_summary_and_exclusions(self, runner):
        result = run_analyze(runner, fixture_path("mixed_actions.json"))
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["summary"] == {
            "created": 1,
            "deleted": 1,
            "updated": 1,
            "replaced": 1,
            "changed": 4,
        }
        actions = {r["address"]: r["action"] for r in payload["resources"]}
        assert actions == {
            "aws_instance.created": "create",
            "aws_instance.deleted": "delete",
            "aws_instance.updated": "update",
            "aws_db_instance.replaced": "replace",
        }

    def test_r3_cli_noop_only_plan_exits_0_zero_counts(self, runner):
        result = run_analyze(runner, fixture_path("noop_only.json"))
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["summary"]["changed"] == 0
        assert payload["resources"] == []

    def test_r3_cli_empty_changes_exits_0(self, runner):
        result = run_analyze(runner, fixture_path("empty_changes.json"))
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["summary"]["changed"] == 0

    def test_r3_cli_unknown_action_exits_2_one_line(self, runner):
        path = fixture_path("unknown_action.json")
        result = run_analyze(runner, path)
        assert result.exit_code == 2
        assert result.stdout == ""
        lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert path in lines[0]

    def test_r3_cli_read_data_source_excluded(self, runner, tmp_path):
        """A-i1: a plan of only data-source reads is a valid no-change plan."""
        entry = make_change(address="data.aws_ami.x", type_="aws_ami", actions=["read"])
        path = write_plan(tmp_path, make_plan([entry]))
        result = run_analyze(runner, path)
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["summary"]["changed"] == 0
        assert payload["resources"] == []
