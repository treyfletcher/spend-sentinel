"""R1: parse a Terraform plan JSON and extract the consumed subset.

Covers: address/type/provider/actions/before/after extraction, the spec's
"single aws_instance create -> changed count 1" example, tolerance of unknown
sibling keys (coder assumption A-i3), unicode addresses, and null
before/after maps.
"""

from __future__ import annotations

import json

from spend_sentinel.core.plan import load_plan, summarize_plan

from .conftest import fixture_path, run_analyze_json


class TestLoadPlanExtraction:
    def test_r1_single_create_fields_extracted(self):
        plan = load_plan(fixture_path("create_single_instance.json"))
        assert plan.format_version == "1.2"
        assert len(plan.resource_changes) == 1
        rc = plan.resource_changes[0]
        assert rc.address == "aws_instance.web"
        assert rc.type == "aws_instance"
        assert rc.provider_name == "registry.terraform.io/hashicorp/aws"
        assert rc.change.actions == ["create"]
        assert rc.change.before is None
        assert rc.change.after == {
            "instance_type": "t3.micro",
            "ami": "ami-12345678",
            "tags": {"Name": "web"},
        }

    def test_r1_single_create_changed_count_is_1(self):
        """Spec R1: a plan containing only an aws_instance create -> changed == 1."""
        plan = load_plan(fixture_path("create_single_instance.json"))
        summary, classified = summarize_plan(plan)
        assert summary.changed == 1
        assert summary.created == 1
        assert len(classified) == 1

    def test_r1_before_after_maps_preserved(self):
        plan = load_plan(fixture_path("mixed_actions.json"))
        by_addr = {rc.address: rc for rc in plan.resource_changes}
        updated = by_addr["aws_instance.updated"]
        assert updated.change.before == {"instance_type": "t3.large"}
        assert updated.change.after == {"instance_type": "t3.xlarge"}

    def test_r1_unknown_sibling_keys_ignored(self):
        """A-i3: unrelated top-level keys (planned_values, configuration, ...)
        must not make a valid plan fail."""
        plan = load_plan(fixture_path("unknown_toplevel_keys.json"))
        assert len(plan.resource_changes) == 1
        assert plan.resource_changes[0].address == "aws_instance.k"

    def test_r1_unicode_address_roundtrips(self):
        plan = load_plan(fixture_path("unicode_address.json"))
        assert plan.resource_changes[0].address == 'aws_instance.serveur_répliqué["日本-α"]'  # noqa: RUF001

    def test_r1_null_before_after_accepted(self):
        plan = load_plan(fixture_path("null_before_after.json"))
        create, delete = plan.resource_changes
        assert create.change.before is None
        assert delete.change.after is None
        summary, _ = summarize_plan(plan)
        assert (summary.created, summary.deleted) == (1, 1)

    def test_r1_format_version_1_0_and_bare_1_accepted(self, tmp_path):
        for fv in ("1", "1.0", "1.2"):
            path = tmp_path / f"fv_{fv}.json"
            path.write_text(json.dumps({"format_version": fv, "resource_changes": []}))
            plan = load_plan(path)
            assert plan.format_version == fv


class TestCliR1:
    def test_r1_cli_single_create_summary(self, runner):
        result, payload = run_analyze_json(runner, fixture_path("create_single_instance.json"))
        assert result.exit_code == 0
        assert payload["summary"] == {
            "created": 1,
            "deleted": 0,
            "updated": 0,
            "replaced": 0,
            "changed": 1,
        }
        # R19: the per-resource view lives in cost.breakdown
        entry = payload["cost"]["breakdown"][0]
        assert entry["address"] == "aws_instance.web"
        assert entry["type"] == "aws_instance"
        assert entry["action"] == "create"

    def test_r1_cli_unicode_address_in_output(self, runner):
        result, payload = run_analyze_json(runner, fixture_path("unicode_address.json"))
        assert result.exit_code == 0
        address = payload["cost"]["breakdown"][0]["address"]
        assert address == 'aws_instance.serveur_répliqué["日本-α"]'  # noqa: RUF001
