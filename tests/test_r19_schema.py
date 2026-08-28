"""R19: the JSON verdict matches docs/verdict-schema.md exactly — key sets,
types, enums, money-as-2-decimal-strings, meta provenance — and both outputs
are byte-identical across runs (AC11 determinism). A-i30: output flags keep
stdout quiet.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

import spend_sentinel

from .conftest import (
    load_snapshot,
    make_change,
    make_plan,
    make_state,
    make_state_resource,
    run_analyze,
    run_analyze_json,
    write_plan,
    write_state,
)

SNAPSHOT = load_snapshot()

TOP_LEVEL = {"verdict", "summary", "cost", "drift", "policy", "meta"}
VERDICTS = {"PASS", "WARN", "BLOCK"}
ACTIONS = {"create", "delete", "update", "replace"}
UNPRICED_REASONS = {"unsupported_type", "unknown_price_key", "attributes_unknown"}
DRIFT_KINDS = {"changed", "missing"}
RULE_NAMES = {"max_monthly_delta", "open_ingress", "deletions", "drift"}
RULE_RESULTS = {"pass", "warn", "block", "skipped"}


def is_money(value) -> bool:
    """Documented money format: a string with exactly two decimals."""
    if not isinstance(value, str):
        return False
    head, _, tail = value.partition(".")
    return len(tail) == 2 and tail.isdigit() and head.lstrip("-").isdigit()


def assert_schema(payload: dict) -> None:
    """Walk the documented structure of docs/verdict-schema.md: exact key sets
    at every level, correct value types, enum membership."""
    assert set(payload) == TOP_LEVEL
    assert payload["verdict"] in VERDICTS

    summary = payload["summary"]
    assert set(summary) == {"created", "deleted", "updated", "replaced", "changed"}
    assert all(isinstance(v, int) and v >= 0 for v in summary.values())
    assert summary["changed"] == (
        summary["created"] + summary["deleted"] + summary["updated"]
        + summary["replaced"]
    )

    cost = payload["cost"]
    assert set(cost) == {"monthly_delta_usd", "breakdown", "unpriced"}
    assert is_money(cost["monthly_delta_usd"])
    for line in cost["breakdown"]:
        assert set(line) == {"address", "type", "action", "monthly_delta_usd"}
        assert isinstance(line["address"], str) and isinstance(line["type"], str)
        assert line["action"] in ACTIONS
        assert is_money(line["monthly_delta_usd"])
    for u in cost["unpriced"]:
        assert set(u) == {"address", "type", "reason"}
        assert u["reason"] in UNPRICED_REASONS

    drift = payload["drift"]
    assert set(drift) == {"status", "drifts", "skipped", "errors"}
    assert drift["status"] in {"ran", "skipped"}
    for d in drift["drifts"]:
        assert set(d) == {"address", "kind", "attribute", "state_value", "live_value"}
        assert d["kind"] in DRIFT_KINDS
        if d["kind"] == "missing":
            assert d["attribute"] is None
            assert d["state_value"] is None and d["live_value"] is None
    for s in drift["skipped"]:
        assert set(s) == {"address", "type", "reason"}
    for e in drift["errors"]:
        assert set(e) == {"address", "error"}
        assert isinstance(e["error"], str)

    policy = payload["policy"]
    assert set(policy) == {"rules"}
    assert [r["name"] for r in policy["rules"]] == [
        "max_monthly_delta", "open_ingress", "deletions", "drift"
    ]
    for r in policy["rules"]:
        assert set(r) == {"name", "result", "message"}
        assert r["name"] in RULE_NAMES
        assert r["result"] in RULE_RESULTS
        assert isinstance(r["message"], str) and r["message"]

    meta = payload["meta"]
    assert set(meta) == {
        "tool_version", "pricing_snapshot_version", "pricing_snapshot_date", "region"
    }
    assert all(isinstance(v, str) and v for v in meta.values())


@pytest.fixture()
def rich_plan(tmp_path):
    """A plan producing every cost section: priced (all actions), unpriced."""
    return write_plan(
        tmp_path,
        make_plan(
            [
                make_change(address="aws_instance.new", actions=["create"],
                            after={"instance_type": "t3.micro"}),
                make_change(address="aws_instance.old", actions=["delete"],
                            before={"instance_type": "t3.small"}),
                make_change(address="aws_instance.up", actions=["update"],
                            before={"instance_type": "t3.large"},
                            after={"instance_type": "t3.xlarge"}),
                make_change(address="aws_db_instance.re", type_="aws_db_instance",
                            actions=["delete", "create"],
                            before={"engine": "postgres",
                                    "instance_class": "db.t3.micro",
                                    "allocated_storage": 20},
                            after={"engine": "postgres",
                                   "instance_class": "db.t3.small",
                                   "allocated_storage": 20}),
                make_change(address="aws_lambda_function.fn",
                            type_="aws_lambda_function", actions=["create"],
                            after={"function_name": "fn"}),
            ],
            provider_region="us-east-1",
        ),
        name="rich_plan.json",
    )


@pytest.fixture()
def rich_state_and_reader(tmp_path, monkeypatch):
    """Drift ran with: changed drift, missing, skipped, error."""
    import spend_sentinel.cli as cli
    from spend_sentinel.adapters.fixture_reader import FixtureAwsReader

    state = write_state(
        tmp_path,
        make_state(
            [
                make_state_resource(address="aws_instance.drifted",
                                    values={"id": "i-1", "instance_type": "t3.micro",
                                            "tags": {}}),
                make_state_resource(address="aws_s3_bucket.gone",
                                    type_="aws_s3_bucket",
                                    values={"bucket": "gone", "tags": {}}),
                make_state_resource(address="aws_iam_role.r", type_="aws_iam_role",
                                    values={}),
                make_state_resource(address="aws_instance.err",
                                    values={"id": "i-err",
                                            "instance_type": "t3.micro", "tags": {}}),
            ]
        ),
    )
    reader = FixtureAwsReader(
        {
            "instances": {"i-1": {"instance_type": "t3.large", "tags": {}}},
            "errors": {"i-err": "AuthFailure: denied"},
        }
    )
    monkeypatch.setattr(cli, "_make_live_reader", lambda region: reader)
    return state


class TestSchemaConformance:
    def test_r19_full_scenario_matches_documented_schema(self, runner, rich_plan,
                                                         rich_state_and_reader,
                                                         tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result, payload = run_analyze_json(
            runner, rich_plan, "--state", rich_state_and_reader
        )
        assert result.exit_code == 2  # drift error, WARN verdict
        assert_schema(payload)
        # everything is populated in this scenario
        assert payload["cost"]["breakdown"] and payload["cost"]["unpriced"]
        assert payload["drift"]["drifts"] and payload["drift"]["skipped"]
        assert payload["drift"]["errors"]
        kinds = {d["kind"] for d in payload["drift"]["drifts"]}
        assert kinds == {"changed", "missing"}

    def test_r19_minimal_scenario_matches_documented_schema(self, runner, tmp_path,
                                                            monkeypatch):
        monkeypatch.chdir(tmp_path)
        plan = write_plan(tmp_path, make_plan([], provider_region="us-east-1"))
        result, payload = run_analyze_json(runner, plan)
        assert result.exit_code == 0
        assert_schema(payload)
        assert payload["verdict"] == "PASS"
        assert payload["drift"]["status"] == "skipped"

    def test_r19_meta_provenance(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        plan = write_plan(tmp_path, make_plan([], provider_region="eu-west-1"))
        _, payload = run_analyze_json(runner, plan)
        meta = payload["meta"]
        assert meta["tool_version"] == spend_sentinel.__version__
        assert meta["pricing_snapshot_version"] == SNAPSHOT["meta"]["version"]
        assert meta["pricing_snapshot_date"] == SNAPSHOT["meta"]["snapshot_date"]
        assert meta["region"] == "eu-west-1"

    def test_r19_negative_money_is_two_decimal_string(self, runner, tmp_path,
                                                      monkeypatch):
        monkeypatch.chdir(tmp_path)
        plan = write_plan(
            tmp_path,
            make_plan(
                [make_change(address="aws_nat_gateway.gone", type_="aws_nat_gateway",
                             actions=["delete"], before={})],
                provider_region="us-east-1",
            ),
        )
        _, payload = run_analyze_json(runner, plan)
        total = payload["cost"]["monthly_delta_usd"]
        assert is_money(total)
        assert total.startswith("-")


class TestDeterminismAc11:
    def test_ac11_both_outputs_byte_identical_across_runs(self, rich_plan, tmp_path):
        """AC11: --out-md and --out-json byte-identical across two runs."""
        def run(idx: int) -> tuple[bytes, bytes]:
            oj = tmp_path / f"v{idx}.json"
            om = tmp_path / f"r{idx}.md"
            proc = subprocess.run(
                [sys.executable, "-m", "spend_sentinel.cli", "analyze",
                 "--plan", rich_plan, "--out-json", str(oj), "--out-md", str(om)],
                capture_output=True, text=True, timeout=60,
            )
            assert proc.returncode == 0
            return oj.read_bytes(), om.read_bytes()

        first_json, first_md = run(1)
        second_json, second_md = run(2)
        assert first_json == second_json
        assert first_md == second_md


class TestOutputFlagBehavior:
    def test_a_i30_out_md_quiet_stdout(self, runner, rich_plan, tmp_path):
        out_md = tmp_path / "r.md"
        result = run_analyze(runner, rich_plan, "--out-md", str(out_md))
        assert result.exit_code == 0
        assert result.stdout == ""
        content = out_md.read_text()
        assert content.startswith("Verdict: ")

    def test_r19_both_flags_write_both_files(self, runner, rich_plan, tmp_path):
        oj = tmp_path / "v.json"
        om = tmp_path / "r.md"
        result = run_analyze(
            runner, rich_plan, "--out-json", str(oj), "--out-md", str(om)
        )
        assert result.exit_code == 0
        assert result.stdout == ""
        assert_schema(json.loads(oj.read_text()))
        assert om.read_text().startswith("Verdict: ")

    def test_r19_unwritable_out_json_exits_2(self, runner, rich_plan, tmp_path):
        target = tmp_path / "adir"
        target.mkdir()
        result = run_analyze(runner, rich_plan, "--out-json", str(target))
        assert result.exit_code == 2
        lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert str(target) in lines[0]
