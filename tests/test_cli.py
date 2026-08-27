"""CLI-level contract tests via subprocess: exit codes 0/2, JSON on stdout
only on success, one-line stderr diagnostics, determinism (R1-R3 surface).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

from .conftest import fixture_path


def run_module(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "spend_sentinel.cli", *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


class TestExitCodesAndStreams:
    def test_cli_success_json_stdout_empty_stderr_exit_0(self):
        proc = run_module("analyze", "--plan", fixture_path("create_single_instance.json"))
        assert proc.returncode == 0
        assert proc.stderr == ""
        payload = json.loads(proc.stdout)
        assert set(payload) == {"summary", "resources", "cost"}

    def test_cli_error_one_line_stderr_empty_stdout_exit_2(self, tmp_path):
        missing = str(tmp_path / "absent.json")
        proc = run_module("analyze", "--plan", missing)
        assert proc.returncode == 2
        assert proc.stdout == ""
        lines = proc.stderr.splitlines()
        assert len(lines) == 1
        assert missing in lines[0]

    def test_cli_missing_plan_flag_exits_2(self):
        proc = run_module("analyze")
        assert proc.returncode == 2
        assert proc.stdout == ""

    def test_cli_version_flag(self):
        proc = run_module("--version")
        assert proc.returncode == 0
        assert "spend-sentinel" in proc.stdout

    def test_cli_output_is_deterministic_across_runs(self):
        path = fixture_path("mixed_actions.json")
        first = run_module("analyze", "--plan", path)
        second = run_module("analyze", "--plan", path)
        assert first.returncode == second.returncode == 0
        assert first.stdout == second.stdout


class TestConsoleScript:
    @pytest.mark.skipif(
        shutil.which("spend-sentinel") is None,
        reason="spend-sentinel console script not on PATH",
    )
    def test_console_script_entry_point_works(self):
        proc = subprocess.run(
            ["spend-sentinel", "analyze", "--plan", fixture_path("empty_changes.json")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout)["summary"]["changed"] == 0
