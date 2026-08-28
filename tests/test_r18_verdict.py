"""R18: verdict aggregation (BLOCK > WARN > PASS, skipped inert) and exit-code
mapping incl. --fail-on-warn and the A5/A-i29 precedence over the R12 drift-
error exit 2. Unit matrices plus CLI-level checks.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from spend_sentinel.adapters.fixture_reader import FixtureAwsReader
from spend_sentinel.core.models import (
    CostReport,
    DriftReport,
    DriftStatus,
    PlanSummary,
    RuleOutcome,
    RuleResult,
    Verdict,
    VerdictMeta,
    VerdictStatus,
)
from spend_sentinel.core.verdict import combine, exit_code

from .conftest import (
    make_change,
    make_plan,
    make_state,
    make_state_resource,
    run_analyze_json,
    write_plan,
    write_state,
)

META = VerdictMeta(
    tool_version="0.0.0",
    pricing_snapshot_version="test",
    pricing_snapshot_date="2026-01-01",
    region="us-east-1",
)


def rules(*outcomes: RuleOutcome) -> list[RuleResult]:
    return [
        RuleResult(name=f"rule_{i}", result=o, message="m")
        for i, o in enumerate(outcomes)
    ]


def verdict_of(*outcomes: RuleOutcome) -> Verdict:
    return combine(
        PlanSummary(created=0, deleted=0, updated=0, replaced=0),
        CostReport(monthly_delta_usd=Decimal("0.00"), breakdown=(), unpriced=()),
        DriftReport(status=DriftStatus.SKIPPED),
        rules(*outcomes),
        META,
    )


class TestCombineR18:
    @pytest.mark.parametrize(
        ("outcomes", "expected"),
        [
            ((RuleOutcome.PASS, RuleOutcome.PASS), VerdictStatus.PASS),
            ((), VerdictStatus.PASS),
            ((RuleOutcome.SKIPPED,), VerdictStatus.PASS),  # skipped affects nothing
            ((RuleOutcome.PASS, RuleOutcome.WARN), VerdictStatus.WARN),
            ((RuleOutcome.WARN, RuleOutcome.SKIPPED), VerdictStatus.WARN),
            ((RuleOutcome.WARN, RuleOutcome.BLOCK), VerdictStatus.BLOCK),
            ((RuleOutcome.BLOCK, RuleOutcome.PASS), VerdictStatus.BLOCK),
            (
                (RuleOutcome.BLOCK, RuleOutcome.WARN, RuleOutcome.SKIPPED,
                 RuleOutcome.PASS),
                VerdictStatus.BLOCK,
            ),
        ],
    )
    def test_r18_aggregation_matrix(self, outcomes, expected):
        assert verdict_of(*outcomes).verdict is expected

    def test_r18_verdict_carries_all_sections(self):
        v = verdict_of(RuleOutcome.PASS)
        assert v.summary.changed == 0
        assert v.drift.status is DriftStatus.SKIPPED
        assert len(v.policy) == 1
        assert v.meta is META


class TestExitCodeMatrix:
    @pytest.mark.parametrize(
        ("outcome", "errors", "fail_on_warn", "expected"),
        [
            (RuleOutcome.PASS, False, False, 0),
            (RuleOutcome.PASS, False, True, 0),
            (RuleOutcome.PASS, True, False, 2),   # R12: errors force 2
            (RuleOutcome.PASS, True, True, 2),
            (RuleOutcome.WARN, False, False, 0),  # A6: WARN exits 0 by default
            (RuleOutcome.WARN, False, True, 1),   # --fail-on-warn
            (RuleOutcome.WARN, True, False, 2),   # errors beat a non-gating WARN
            (RuleOutcome.WARN, True, True, 1),    # A-i29: the gate outranks errors
            (RuleOutcome.BLOCK, False, False, 1),
            (RuleOutcome.BLOCK, True, False, 1),  # A5: 1 outranks 2
            (RuleOutcome.BLOCK, True, True, 1),
        ],
    )
    def test_r18_exit_code_matrix(self, outcome, errors, fail_on_warn, expected):
        v = verdict_of(outcome)
        assert exit_code(v, errors=errors, fail_on_warn=fail_on_warn) == expected


@pytest.fixture()
def warn_plan(tmp_path):
    """A deletion under the default policy -> WARN."""
    return write_plan(
        tmp_path,
        make_plan(
            [
                make_change(address="aws_instance.gone", actions=["delete"],
                            before={"instance_type": "t3.micro"})
            ],
            provider_region="us-east-1",
        ),
        name="warn_plan.json",
    )


@pytest.fixture()
def error_state(tmp_path):
    """A state whose only resource errors on read (R12)."""
    return write_state(
        tmp_path,
        make_state(
            [make_state_resource(values={"id": "i-err", "instance_type": "t3.micro",
                                         "tags": {}})]
        ),
    )


@pytest.fixture()
def erroring_reader(monkeypatch):
    import spend_sentinel.cli as cli

    reader = FixtureAwsReader({"errors": {"i-err": "AuthFailure: denied"}})
    monkeypatch.setattr(cli, "_make_live_reader", lambda region: reader)
    return reader


class TestExitCodesThroughCli:
    def test_r18_warn_exits_0_by_default(self, runner, warn_plan, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result, payload = run_analyze_json(runner, warn_plan)
        assert result.exit_code == 0
        assert payload["verdict"] == "WARN"

    def test_r18_warn_with_fail_on_warn_exits_1(self, runner, warn_plan, tmp_path,
                                                monkeypatch):
        monkeypatch.chdir(tmp_path)
        result, payload = run_analyze_json(runner, warn_plan, "--fail-on-warn")
        assert result.exit_code == 1
        assert payload["verdict"] == "WARN"

    def test_r18_pass_with_fail_on_warn_exits_0(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        plan = write_plan(
            tmp_path,
            make_plan(
                [make_change(actions=["create"], after={"instance_type": "t3.micro"})],
                provider_region="us-east-1",
            ),
        )
        result, payload = run_analyze_json(runner, plan, "--fail-on-warn")
        assert result.exit_code == 0
        assert payload["verdict"] == "PASS"

    def test_r18_block_exits_1(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        policy = tmp_path / "block.yaml"
        policy.write_text("rules:\n  deletions:\n    action: block\n")
        plan = write_plan(
            tmp_path,
            make_plan(
                [make_change(address="aws_instance.gone", actions=["delete"],
                             before={"instance_type": "t3.micro"})],
                provider_region="us-east-1",
            ),
        )
        result, payload = run_analyze_json(runner, plan, "--policy", str(policy))
        assert result.exit_code == 1
        assert payload["verdict"] == "BLOCK"


class TestA5PrecedenceThroughCli:
    """S7 (finally testable): exit 1 outranks the drift-error exit 2 (A5/AC9)."""

    def test_block_plus_drift_errors_exits_1(self, runner, warn_plan, error_state,
                                             erroring_reader, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        policy = tmp_path / "block.yaml"
        policy.write_text("rules:\n  deletions:\n    action: block\n")
        result, payload = run_analyze_json(
            runner, warn_plan, "--state", error_state, "--policy", str(policy)
        )
        assert result.exit_code == 1
        assert payload["verdict"] == "BLOCK"
        assert len(payload["drift"]["errors"]) == 1  # the error stays visible

    def test_warn_plus_drift_errors_exits_2(self, runner, warn_plan, error_state,
                                            erroring_reader, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result, payload = run_analyze_json(runner, warn_plan, "--state", error_state)
        assert result.exit_code == 2
        assert payload["verdict"] == "WARN"

    def test_warn_plus_errors_plus_fail_on_warn_exits_1(self, runner, warn_plan,
                                                        error_state, erroring_reader,
                                                        tmp_path, monkeypatch):
        """A-i29: --fail-on-warn turns the WARN into a CI gate, which outranks
        the runtime-error 2 exactly like a BLOCK would."""
        monkeypatch.chdir(tmp_path)
        result, payload = run_analyze_json(
            runner, warn_plan, "--state", error_state, "--fail-on-warn"
        )
        assert result.exit_code == 1
        assert payload["verdict"] == "WARN"


class TestNoOutputFilesOnUsageErrors:
    def test_r18_ingestion_error_writes_no_output_files(self, runner, tmp_path):
        """AC10 extended to the new flags: exit 2 must leave --out-json/--out-md
        paths uncreated."""
        from .conftest import run_analyze

        out_json = tmp_path / "v.json"
        out_md = tmp_path / "r.md"
        result = run_analyze(
            runner, str(tmp_path / "missing-plan.json"),
            "--out-json", str(out_json), "--out-md", str(out_md),
        )
        assert result.exit_code == 2
        assert not out_json.exists()
        assert not out_md.exists()

    def test_r18_policy_error_writes_no_output_files(self, runner, tmp_path):
        plan = write_plan(
            tmp_path,
            make_plan(
                [make_change(actions=["create"], after={"instance_type": "t3.micro"})],
                provider_region="us-east-1",
            ),
        )
        bad_policy = tmp_path / "bad.yaml"
        bad_policy.write_text("rules:\n  max_cpu: {}\n")
        from .conftest import run_analyze

        out_json = tmp_path / "v.json"
        result = run_analyze(
            runner, plan, "--policy", str(bad_policy), "--out-json", str(out_json)
        )
        assert result.exit_code == 2
        assert not out_json.exists()
