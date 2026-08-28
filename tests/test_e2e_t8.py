"""T8: end-to-end scenarios through the CLI entry point, mapping the spec's
scenario list (a)-(g) and the acceptance criteria that close in this
increment (AC1, AC2, AC3, AC9's exit interplay; the full AC coverage table
lives in docs/test-reports/feature-spend-sentinel-v1-increment5.md).

Expected costs are computed from the bundled pricing snapshot, never
hardcoded. Everything runs offline; drift uses FixtureAwsReader injected at
the CLI's single wiring seam.
"""

from __future__ import annotations

import subprocess
import sys
from decimal import ROUND_HALF_UP, Decimal

import pytest

from spend_sentinel.adapters.fixture_reader import FixtureAwsReader
from spend_sentinel.core.cost import HOURS_PER_MONTH

from .conftest import (
    fixture_path,
    load_snapshot,
    make_state,
    make_state_resource,
    run_analyze,
    run_analyze_json,
    write_state,
)

SNAPSHOT = load_snapshot()


def rate(region: str, service: str, key: str) -> Decimal:
    return Decimal(SNAPSHOT["regions"][region][service][key])


def cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    """No accidental ./spend-sentinel.yaml pickup in any e2e scenario."""
    monkeypatch.chdir(tmp_path)


class TestScenarioA_SmallCreatePass:
    def test_ac1_small_create_pass_exit_0_exact_cents(self, runner):
        """AC1: create_small.json -> exit 0, PASS, created == 2, delta to the
        cent from the snapshot."""
        expected = cents(rate("us-east-1", "aws_instance", "t3.micro") * HOURS_PER_MONTH)
        expected += cents(rate("us-east-1", "aws_ebs_volume", "gp3") * Decimal("20"))
        result, payload = run_analyze_json(
            runner, fixture_path("create_small.json"), "--skip-drift"
        )
        assert result.exit_code == 0
        assert payload["verdict"] == "PASS"
        assert payload["summary"]["created"] == 2
        assert payload["cost"]["monthly_delta_usd"] == str(expected)


class TestScenarioB_ExpensiveBlock:
    def test_ac2_breach_200_blocks_exit_1_with_delta_in_message(self, runner):
        """AC2: create_expensive.json breaches the $200 default -> BLOCK exit 1,
        max_monthly_delta block with the computed delta in its message."""
        expected = 3 * cents(
            rate("us-east-1", "aws_instance", "r5.large") * HOURS_PER_MONTH
        )
        assert expected > Decimal("200"), "fixture must breach the limit"
        result, payload = run_analyze_json(
            runner, fixture_path("create_expensive.json"), "--skip-drift"
        )
        assert result.exit_code == 1
        assert payload["verdict"] == "BLOCK"
        rule = {r["name"]: r for r in payload["policy"]["rules"]}["max_monthly_delta"]
        assert rule["result"] == "block"
        assert str(expected) in rule["message"]
        assert payload["cost"]["monthly_delta_usd"] == str(expected)


class TestScenarioAC3_Resize:
    def test_ac3_update_resize_breakdown_equals_rate_difference(self, runner):
        """AC3: t3.large -> t3.xlarge; breakdown entry and total both equal
        cost(t3.xlarge) - cost(t3.large)."""
        expected = cents(
            (
                rate("us-east-1", "aws_instance", "t3.xlarge")
                - rate("us-east-1", "aws_instance", "t3.large")
            )
            * HOURS_PER_MONTH
        )
        result, payload = run_analyze_json(
            runner, fixture_path("update_resize.json"), "--skip-drift"
        )
        assert result.exit_code == 0
        assert len(payload["cost"]["breakdown"]) == 1
        assert payload["cost"]["breakdown"][0]["monthly_delta_usd"] == str(expected)
        assert payload["cost"]["monthly_delta_usd"] == str(expected)


class TestScenarioC_OpenSsh:
    def test_t8c_sg_open_port_22_blocks_exit_1(self, runner):
        result, payload = run_analyze_json(
            runner, fixture_path("sg_open_ssh.json"), "--skip-drift"
        )
        assert result.exit_code == 1
        assert payload["verdict"] == "BLOCK"
        rule = {r["name"]: r for r in payload["policy"]["rules"]}["open_ingress"]
        assert rule["result"] == "block"
        assert "aws_security_group.ssh" in rule["message"]
        assert "22" in rule["message"]


class TestScenarioD_Deletions:
    def test_t8d_deletions_warn_exit_0(self, runner):
        result, payload = run_analyze_json(
            runner, fixture_path("delete_db.json"), "--skip-drift"
        )
        assert result.exit_code == 0
        assert payload["verdict"] == "WARN"
        rule = {r["name"]: r for r in payload["policy"]["rules"]}["deletions"]
        assert rule["result"] == "warn"
        assert "aws_db_instance.legacy" in rule["message"]

    def test_t8d_deletions_fail_on_warn_exit_1(self, runner):
        result, payload = run_analyze_json(
            runner, fixture_path("delete_db.json"), "--skip-drift", "--fail-on-warn"
        )
        assert result.exit_code == 1
        assert payload["verdict"] == "WARN"


class TestScenarioE_DriftWarn:
    def test_t8e_drift_via_fixture_reader_warns(self, runner, tmp_path, monkeypatch):
        """AC7-flavored e2e: FixtureAwsReader drift -> drift rule warn -> WARN."""
        import spend_sentinel.cli as cli

        state_path = write_state(
            tmp_path,
            make_state(
                [
                    make_state_resource(
                        address="aws_instance.web",
                        values={"id": "i-web", "instance_type": "t3.micro",
                                "tags": {}},
                    )
                ]
            ),
        )
        reader = FixtureAwsReader(
            {"instances": {"i-web": {"instance_type": "t3.medium", "tags": {}}}}
        )
        monkeypatch.setattr(cli, "_make_live_reader", lambda region: reader)
        result, payload = run_analyze_json(
            runner, fixture_path("create_small.json"), "--state", state_path
        )
        assert result.exit_code == 0
        assert payload["verdict"] == "WARN"
        assert payload["drift"]["status"] == "ran"
        drift = payload["drift"]["drifts"][0]
        assert drift["address"] == "aws_instance.web"
        assert drift["state_value"] == "t3.micro"
        assert drift["live_value"] == "t3.medium"
        rule = {r["name"]: r for r in payload["policy"]["rules"]}["drift"]
        assert rule["result"] == "warn"


class TestScenarioF_MalformedPlan:
    def test_t8f_malformed_plan_exit_2_no_outputs(self, runner, tmp_path):
        out_json = tmp_path / "v.json"
        result = run_analyze(
            runner, fixture_path("malformed.json"), "--out-json", str(out_json)
        )
        assert result.exit_code == 2
        assert result.stdout == ""
        assert not out_json.exists()
        assert len([ln for ln in result.stderr.splitlines() if ln.strip()]) == 1


class TestScenarioG_SkipDriftWithoutBoto3:
    def test_t8g_skip_drift_boto3_free_subprocess(self, tmp_path):
        """--skip-drift end to end in a fresh interpreter; asserts boto3 was
        never imported (works regardless of whether boto3 is installed)."""
        code = (
            "import sys\n"
            "from click.testing import CliRunner\n"
            "from spend_sentinel.cli import main\n"
            f"args = ['analyze', '--plan', {fixture_path('create_small.json')!r},"
            " '--skip-drift']\n"
            "result = CliRunner().invoke(main, args)\n"
            "assert result.exit_code == 0, result.output\n"
            "assert 'boto3' not in sys.modules\n"
            "print('ok')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "ok"


class TestAc4MarkdownHalf:
    def test_ac4_markdown_shows_n_unpriced_resources_line(self, runner):
        """AC4 (Markdown half, now implemented): the report carries the
        '2 unpriced resources' line for the mixed_unpriced fixture."""
        result = run_analyze(
            runner, fixture_path("mixed_unpriced.json"), "--skip-drift"
        )
        assert result.exit_code == 0
        assert "2 unpriced resources:" in result.stdout
        assert "unsupported_type" in result.stdout
        assert "unknown_price_key" in result.stdout


class TestMarkdownOnStdoutE2e:
    def test_t8_default_output_is_pr_ready_markdown(self, runner):
        """The default invocation emits the R20 report on stdout."""
        result = run_analyze(runner, fixture_path("create_small.json"), "--skip-drift")
        assert result.exit_code == 0
        md = result.stdout
        assert md.startswith("Verdict: PASS\n")
        assert "## Cost" in md
        assert "## Policy" in md
        assert "| aws_instance.web | create |" in md
        assert "## Drift" not in md  # drift did not run
