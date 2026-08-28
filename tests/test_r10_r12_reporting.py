"""R10 (drift reporting: missing kind, skipped taxonomy, full drift records)
and R12 (per-resource AWS read failures never kill the run; exit code 2).

CLI-level R12 tests inject a FixtureAwsReader by monkeypatching the CLI's
reader factory — the only seam the wiring exposes without boto3.

BUG-4 (rule-set rendering nondeterminism across processes,
docs/test-reports/feature-spend-sentinel-v1-increment3.md) was fixed in review;
its former xfail now runs as a regular regression test.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from spend_sentinel.adapters.fixture_reader import FixtureAwsReader
from spend_sentinel.core.drift import _summarize_exception, detect
from spend_sentinel.core.models import DriftKind, State

from .conftest import (
    make_change,
    make_plan,
    make_state,
    make_state_resource,
    write_plan,
    write_state,
)


def state_of(*resources) -> State:
    return State.model_validate(make_state(list(resources)))


class TestMissingKindR10:
    @pytest.mark.parametrize(
        ("type_", "values"),
        [
            ("aws_instance", {"id": "i-none", "instance_type": "t3.micro", "tags": {}}),
            ("aws_security_group", {"id": "sg-none", "ingress": [], "egress": []}),
            ("aws_s3_bucket", {"bucket": "b-none", "tags": {}}),
        ],
    )
    def test_r10_supported_resource_absent_from_aws_is_missing(self, type_, values):
        resource = make_state_resource(address=f"{type_}.gone", type_=type_, values=values)
        report = detect(state_of(resource), FixtureAwsReader({}))
        assert len(report.drifts) == 1
        drift = report.drifts[0]
        assert drift.kind is DriftKind.MISSING
        assert drift.address == f"{type_}.gone"
        assert report.errors == ()

    def test_r10_unsupported_types_skipped_with_reason(self):
        resources = [
            make_state_resource(address="aws_lambda_function.f", type_="aws_lambda_function",
                                values={}),
            make_state_resource(address="aws_iam_role.r", type_="aws_iam_role", values={}),
        ]
        report = detect(state_of(*resources), FixtureAwsReader({}))
        assert [(s.address, s.reason) for s in report.skipped] == [
            ("aws_lambda_function.f", "unsupported_type"),
            ("aws_iam_role.r", "unsupported_type"),
        ]
        assert report.drifts == ()

    def test_r10_drift_record_is_complete(self):
        resource = make_state_resource(
            values={"id": "i-1", "instance_type": "t3.micro", "tags": {}}
        )
        report = detect(
            state_of(resource),
            FixtureAwsReader({"instances": {"i-1": {"instance_type": "c5.large",
                                                    "tags": {}}}}),
        )
        drift = report.drifts[0]
        assert drift.address == "aws_instance.web"
        assert drift.attribute == "instance_type"
        assert drift.state_value == "t3.micro"
        assert drift.live_value == "c5.large"

    def test_r10_missing_id_is_error_not_missing_a_i14(self):
        """A supported resource with no usable lookup id cannot be proven
        missing — it must land in errors (A-i14), never be silently skipped."""
        resource = make_state_resource(values={"instance_type": "t3.micro"})  # no id
        report = detect(state_of(resource), FixtureAwsReader({}))
        assert report.drifts == ()
        assert report.skipped == ()
        assert [e.address for e in report.errors] == ["aws_instance.web"]
        assert "id" in report.errors[0].error


class RaisingReader:
    """AwsReader stub that raises a configured exception for every call."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def _boom(self, _ident: str):
        self.calls += 1
        raise self.exc

    get_instance = _boom
    get_security_group = _boom
    get_bucket = _boom


class TestErrorsR12Core:
    def test_r12_one_error_does_not_kill_the_run(self):
        resources = [
            make_state_resource(address="aws_instance.bad",
                                values={"id": "i-bad", "instance_type": "t3.micro",
                                        "tags": {}}),
            make_state_resource(address="aws_instance.good",
                                values={"id": "i-good", "instance_type": "t3.micro",
                                        "tags": {}}),
        ]
        reader = FixtureAwsReader(
            {
                "instances": {"i-good": {"instance_type": "t3.large", "tags": {}}},
                "errors": {"i-bad": "AuthFailure: token expired"},
            }
        )
        report = detect(state_of(*resources), reader)
        assert [e.address for e in report.errors] == ["aws_instance.bad"]
        assert "AuthFailure" in report.errors[0].error
        assert [d.address for d in report.drifts] == ["aws_instance.good"]

    def test_r12_every_resource_erroring_still_returns_report(self):
        resources = [
            make_state_resource(address=f"aws_instance.x{i}",
                                values={"id": f"i-{i}", "instance_type": "t3.micro",
                                        "tags": {}})
            for i in range(3)
        ]
        reader = RaisingReader(TimeoutError("read timed out"))
        report = detect(state_of(*resources), reader)
        assert len(report.errors) == 3
        assert reader.calls == 3
        assert all("TimeoutError" in e.error for e in report.errors)

    def test_r12_hostile_exception_type_is_captured_a_i18(self):
        """A-i18: ANY exception from the reader is captured, not only AWS ones."""

        class WeirdError(Exception):
            pass

        report = detect(
            state_of(make_state_resource(values={"id": "i-1", "instance_type": "x",
                                                 "tags": {}})),
            RaisingReader(WeirdError("boom")),
        )
        assert [e.error for e in report.errors] == ["WeirdError: boom"]


class TestExceptionSummary:
    def test_r12_summary_is_type_and_message(self):
        assert _summarize_exception(ValueError("nope")) == "ValueError: nope"

    def test_r12_summary_without_message_is_type_only(self):
        assert _summarize_exception(ValueError()) == "ValueError"

    def test_r12_summary_is_single_line_control_chars_replaced(self):
        summary = _summarize_exception(ValueError("line1\nline2\r\x1b[31mred\x00"))
        assert "\n" not in summary
        assert "\r" not in summary
        assert "\x1b" not in summary
        assert "\x00" not in summary

    def test_r12_summary_is_length_capped(self):
        summary = _summarize_exception(ValueError("A" * 10_000))
        assert len(summary) <= 200
        assert summary.endswith("...")


class TestErrorsR12Cli:
    """R12 through the CLI: report still printed, exit 2 iff drift errors."""

    @pytest.fixture()
    def plan_path(self, tmp_path):
        return write_plan(
            tmp_path,
            make_plan(
                [make_change(actions=["create"], after={"instance_type": "t3.micro"})],
                provider_region="us-east-1",
            ),
        )

    def analyze_with_reader(self, runner, monkeypatch, plan_path, state_path, reader):
        import spend_sentinel.cli as cli

        from .conftest import run_analyze_json

        monkeypatch.setattr(cli, "_make_live_reader", lambda region: reader)
        return run_analyze_json(runner, plan_path, "--state", state_path)

    def test_r12_cli_error_exits_2_report_still_produced(
        self, runner, monkeypatch, tmp_path, plan_path
    ):
        state_path = write_state(
            tmp_path,
            make_state(
                [
                    make_state_resource(address="aws_instance.bad",
                                        values={"id": "i-bad", "instance_type": "t3.micro",
                                                "tags": {}}),
                    make_state_resource(address="aws_instance.good",
                                        values={"id": "i-good",
                                                "instance_type": "t3.micro", "tags": {}}),
                ]
            ),
        )
        reader = FixtureAwsReader(
            {
                "instances": {"i-good": {"instance_type": "t3.micro", "tags": {}}},
                "errors": {"i-bad": "AuthFailure: not authorized"},
            }
        )
        result, payload = self.analyze_with_reader(
            runner, monkeypatch, plan_path, state_path, reader
        )
        assert result.exit_code == 2
        assert payload is not None  # the report was produced before exiting (R12)
        assert payload["drift"]["status"] == "ran"
        assert [e["address"] for e in payload["drift"]["errors"]] == ["aws_instance.bad"]
        assert "AuthFailure" in payload["drift"]["errors"][0]["error"]
        # a one-line stderr notice explains the exit 2 without echoing
        # attacker-influenced addresses or error text
        lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert "drift" in lines[0]
        assert "aws_instance.bad" not in lines[0]
        assert "AuthFailure" not in lines[0]

    def test_r12_cli_error_plus_clean_drift_still_exit_2(
        self, runner, monkeypatch, tmp_path, plan_path
    ):
        state_path = write_state(
            tmp_path,
            make_state(
                [
                    make_state_resource(address="aws_instance.bad",
                                        values={"id": "i-bad", "instance_type": "t3.micro",
                                                "tags": {}}),
                ]
            ),
        )
        reader = FixtureAwsReader({"errors": {"i-bad": "Throttling: rate exceeded"}})
        result, payload = self.analyze_with_reader(
            runner, monkeypatch, plan_path, state_path, reader
        )
        # verdict is PASS (no drifts, no deletions) but the read error forces 2
        assert result.exit_code == 2
        assert payload["verdict"] == "PASS"

    def test_r12_cli_no_errors_exits_0_even_with_drift(
        self, runner, monkeypatch, tmp_path, plan_path
    ):
        state_path = write_state(
            tmp_path,
            make_state(
                [
                    make_state_resource(values={"id": "i-1", "instance_type": "t3.micro",
                                                "tags": {}}),
                ]
            ),
        )
        reader = FixtureAwsReader(
            {"instances": {"i-1": {"instance_type": "t3.large", "tags": {}}}}
        )
        result, payload = self.analyze_with_reader(
            runner, monkeypatch, plan_path, state_path, reader
        )
        # drift detected -> drift rule WARN -> WARN verdict, still exit 0 (A6)
        assert result.exit_code == 0
        assert payload["verdict"] == "WARN"
        assert len(payload["drift"]["drifts"]) == 1


class TestRuleRenderingDeterminism:
    def test_r10_rule_rendering_stable_across_hash_seeds(self):
        """Rendered rule lists must be byte-identical across interpreter
        processes (PYTHONHASHSEED 0 and 5 are known to produce different set
        iteration orders for these tuples)."""
        code = (
            "from spend_sentinel.core.drift import _render_rules, _atomic_rules\n"
            "rules = ["
            "{'protocol': 'tcp', 'from_port': 80, 'to_port': 80,"
            " 'cidr_blocks': ['10.0.0.0/8']},"
            "{'protocol': 'tcp', 'from_port': 80, 'to_port': 8080,"
            " 'cidr_blocks': ['10.0.0.0/8']}]\n"
            "print(_render_rules(_atomic_rules(rules)))\n"
        )
        outputs = set()
        for seed in ("0", "5"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            proc = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True, text=True, env=env, timeout=30,
            )
            assert proc.returncode == 0, proc.stderr
            outputs.add(proc.stdout)
        assert len(outputs) == 1, f"nondeterministic rendering: {outputs}"
