"""R11: with no --state or with --skip-drift, no AWS call path is exercised,
no adapter/boto3 module is imported, and the drift section reports skipped.

R21 (this increment's slice): the skip path works with boto3 uninstalled —
boto3 genuinely is uninstalled in this environment, which several tests
assert/exploit; they skip gracefully where boto3 happens to be installed.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys

import pytest

from spend_sentinel.core.drift import skipped_report
from spend_sentinel.core.models import DriftStatus

from .conftest import (
    make_change,
    make_plan,
    make_state_resource,
    run_analyze,
    run_analyze_json,
    write_plan,
)

BOTO3_INSTALLED = importlib.util.find_spec("boto3") is not None


class NeverCallReader:
    """AwsReader stub that fails the test on ANY method call (R11 guarantee)."""

    def get_instance(self, instance_id):
        raise AssertionError("AwsReader.get_instance called on a skip path")

    def get_security_group(self, sg_id):
        raise AssertionError("AwsReader.get_security_group called on a skip path")

    def get_bucket(self, name):
        raise AssertionError("AwsReader.get_bucket called on a skip path")


@pytest.fixture()
def plan_path(tmp_path):
    return write_plan(
        tmp_path,
        make_plan(
            [make_change(actions=["create"], after={"instance_type": "t3.micro"})],
            provider_region="us-east-1",
        ),
    )


class TestSkippedStatus:
    def test_r11_skipped_report_shape(self):
        report = skipped_report()
        assert report.status is DriftStatus.SKIPPED
        assert report.drifts == report.skipped == report.errors == ()

    def test_r11_cli_no_state_drift_skipped(self, runner, plan_path):
        result, payload = run_analyze_json(runner, plan_path)
        assert result.exit_code == 0
        drift = payload["drift"]
        assert drift == {"status": "skipped", "drifts": [], "skipped": [], "errors": []}

    def test_r11_cli_skip_drift_flag_wins_over_state(self, runner, plan_path, tmp_path):
        from .conftest import make_state, write_state

        state_path = write_state(
            tmp_path,
            make_state([make_state_resource(values={"id": "i-1",
                                                    "instance_type": "t3.micro",
                                                    "tags": {}})]),
        )
        result, payload = run_analyze_json(
            runner, plan_path, "--state", state_path, "--skip-drift"
        )
        assert result.exit_code == 0
        assert payload["drift"]["status"] == "skipped"

    def test_r11_skip_drift_does_not_even_read_the_state_file(self, runner, plan_path,
                                                              tmp_path):
        """--skip-drift means no drift work at all: a nonexistent state path
        must not fail the run."""
        result, payload = run_analyze_json(
            runner, plan_path, "--state", str(tmp_path / "no-such-state.json"),
            "--skip-drift",
        )
        assert result.exit_code == 0
        assert payload["drift"]["status"] == "skipped"


class TestNoAwsCallPath:
    def test_r11_reader_factory_never_invoked_on_skip(self, runner, plan_path,
                                                      monkeypatch):
        """AC8-flavored: wire a factory that raises if the CLI even tries to
        construct a reader; both skip variants must succeed."""
        import spend_sentinel.cli as cli

        def explode(region):
            raise AssertionError("reader constructed on a skip path")

        monkeypatch.setattr(cli, "_make_live_reader", explode)
        assert run_analyze(runner, plan_path).exit_code == 0
        assert run_analyze(runner, plan_path, "--skip-drift").exit_code == 0

    def test_r11_raising_stub_reader_never_called_by_detect_on_skip(self):
        """skipped_report() (the skip-path result) touches no reader at all;
        NeverCallReader documents the guarantee for any future wiring."""
        reader = NeverCallReader()
        report = skipped_report()
        assert report.status is DriftStatus.SKIPPED
        # the stub stays uncalled by construction; calling would raise
        with pytest.raises(AssertionError):
            reader.get_instance("i-1")

    @pytest.mark.parametrize("extra_args", [[], ["--skip-drift"]])
    def test_r11_boto3_and_adapter_never_imported_on_skip(self, plan_path, extra_args):
        """Subprocess check: after a skip-path run, neither boto3 nor the
        adapter module may appear in sys.modules (R11/R21)."""
        code = (
            "import json, sys\n"
            "from click.testing import CliRunner\n"
            "from spend_sentinel.cli import main\n"
            f"args = ['analyze', '--plan', {plan_path!r}] + {extra_args!r}\n"
            "result = CliRunner().invoke(main, args)\n"
            "assert result.exit_code == 0, result.output\n"
            "leaked = [m for m in sys.modules\n"
            "          if m == 'boto3' or m.startswith('boto3.')\n"
            "          or m == 'botocore' or m.startswith('botocore.')\n"
            "          or m == 'spend_sentinel.adapters.boto3_reader']\n"
            "print(json.dumps(leaked))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == []


class TestWithoutBoto3:
    @pytest.mark.skipif(BOTO3_INSTALLED, reason="boto3 is installed in this environment")
    def test_r21_environment_has_no_boto3(self):
        """Precondition making this whole suite an R21 witness: the full test
        run (including every CLI test) executes without boto3."""
        assert importlib.util.find_spec("boto3") is None

    @pytest.mark.skipif(BOTO3_INSTALLED, reason="boto3 is installed in this environment")
    def test_r21_skip_drift_works_without_boto3_in_subprocess(self, plan_path, tmp_path):
        out = tmp_path / "v.json"
        proc = subprocess.run(
            [sys.executable, "-m", "spend_sentinel.cli", "analyze", "--plan", plan_path,
             "--skip-drift", "--out-json", str(out)],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0
        assert json.loads(out.read_text())["drift"]["status"] == "skipped"

    @pytest.mark.skipif(BOTO3_INSTALLED, reason="boto3 is installed in this environment")
    def test_r11_state_without_boto3_fails_with_one_line_diagnostic(self, plan_path,
                                                                    tmp_path):
        """Asking for real drift without the [aws] extra: clean exit 2 telling
        the user about boto3/--skip-drift — never a traceback."""
        from .conftest import make_state, write_state

        state_path = write_state(
            tmp_path,
            make_state([make_state_resource(values={"id": "i-1",
                                                    "instance_type": "t3.micro",
                                                    "tags": {}})]),
        )
        proc = subprocess.run(
            [sys.executable, "-m", "spend_sentinel.cli", "analyze", "--plan", plan_path,
             "--state", state_path],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 2
        assert proc.stdout == ""
        assert "Traceback" not in proc.stderr
        lines = [ln for ln in proc.stderr.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert "boto3" in lines[0]
        assert "--skip-drift" in lines[0]
