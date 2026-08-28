"""CLI-level contract tests via subprocess: exit codes, Markdown on stdout by
default (R19/R20), machine-readable JSON via --out-json, one-line stderr
diagnostics, determinism.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

from .conftest import fixture_path

R19_TOP_LEVEL_KEYS = {"verdict", "summary", "cost", "drift", "policy", "meta"}


def run_module(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "spend_sentinel.cli", *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


class TestExitCodesAndStreams:
    def test_cli_success_markdown_stdout_empty_stderr_exit_0(self):
        """R19: with no output flags, stdout carries the Markdown report."""
        proc = run_module("analyze", "--plan", fixture_path("create_single_instance.json"))
        assert proc.returncode == 0
        assert proc.stderr == ""
        assert proc.stdout.startswith("Verdict: PASS\n")

    def test_cli_out_json_writes_r19_keys_quiet_stdout(self, tmp_path):
        """R19 key set via --out-json; A-i30: stdout is quiet with an output flag."""
        out = tmp_path / "v.json"
        proc = run_module(
            "analyze", "--plan", fixture_path("create_single_instance.json"),
            "--out-json", str(out),
        )
        assert proc.returncode == 0
        assert proc.stdout == ""
        payload = json.loads(out.read_text())
        assert set(payload) == R19_TOP_LEVEL_KEYS
        # no --state: drift must report skipped (R11)
        assert payload["drift"]["status"] == "skipped"

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
    def test_console_script_entry_point_works(self, tmp_path):
        out = tmp_path / "v.json"
        proc = subprocess.run(
            ["spend-sentinel", "analyze", "--plan", fixture_path("empty_changes.json"),
             "--out-json", str(out)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0
        payload = json.loads(out.read_text())
        assert payload["verdict"] == "PASS"
        assert payload["summary"]["changed"] == 0
