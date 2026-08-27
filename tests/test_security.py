"""Security-focused tests (spec Security considerations, increment-1 surface).

Hostile/malformed input must fail closed with exit 2 and never a traceback;
diagnostics must never echo file contents; deeply nested JSON must terminate
promptly. Known defect BUG-1 (deep nesting -> RecursionError traceback) is
xfailed with a reference to the test report.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import make_change, make_plan, run_analyze, write_plan

SECRET = "hunter2-SUPER-SECRET-VALUE-af8b21"


def run_cli_subprocess(*args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "spend_sentinel.cli", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def deeply_nested_plan(path: Path, depth: int = 100_000) -> str:
    payload = (
        '{"format_version": "1.2", "resource_changes": [], "x": '
        + "[" * depth
        + "]" * depth
        + "}"
    )
    path.write_text(payload)
    return str(path)


class TestNoContentEcho:
    def test_malformed_json_diagnostic_does_not_echo_contents(self, runner, tmp_path):
        p = tmp_path / "leak.json"
        p.write_text(f'{{"password": "{SECRET}" THIS IS BROKEN')
        result = run_analyze(runner, str(p))
        assert result.exit_code == 2
        assert SECRET not in result.stderr
        assert SECRET not in result.stdout

    def test_validation_error_does_not_echo_field_values(self, runner, tmp_path):
        path = write_plan(
            tmp_path,
            {"format_version": {"leak": SECRET}, "resource_changes": []},
        )
        result = run_analyze(runner, path)
        assert result.exit_code == 2
        assert SECRET not in result.stderr

    def test_invalid_actions_value_not_echoed_when_structural(self, runner, tmp_path):
        entry = make_change()
        entry["change"]["actions"] = {"leak": SECRET}
        path = write_plan(tmp_path, make_plan([entry]))
        result = run_analyze(runner, path)
        assert result.exit_code == 2
        assert SECRET not in result.stderr

    def test_attribute_values_never_reach_output_on_error(self, runner, tmp_path):
        """A sensitive attribute value in before/after must not leak through an
        error diagnostic for a later, invalid entry."""
        good = make_change(address="aws_db_instance.db", type_="aws_db_instance",
                           actions=["create"], after={"password": SECRET})
        bad = make_change(address="aws_instance.bad", actions=["explode"])
        path = write_plan(tmp_path, make_plan([good, bad]))
        result = run_analyze(runner, path)
        assert result.exit_code == 2
        assert SECRET not in result.stderr
        assert result.stdout == ""


class TestFailClosedNoTraceback:
    @pytest.mark.parametrize(
        "content",
        [
            "null",
            "true",
            '"a string"',
            '{"format_version": null, "resource_changes": []}',
            '{"format_version": "1.2", "resource_changes": {}}',
            '{"format_version": "1.2", "resource_changes": [null]}',
            '{"format_version": "1.2", "resource_changes": [{"address": null}]}',
            '{"format_version": ["1.2"], "resource_changes": []}',
        ],
    )
    def test_hostile_structures_exit_2_no_traceback(self, tmp_path, content):
        p = tmp_path / "hostile.json"
        p.write_text(content)
        proc = run_cli_subprocess("analyze", "--plan", str(p))
        assert proc.returncode == 2
        assert proc.stdout == ""
        assert "Traceback" not in proc.stderr
        assert len([ln for ln in proc.stderr.splitlines() if ln.strip()]) == 1

    def test_deeply_nested_json_terminates_promptly(self, tmp_path):
        """Deep nesting must not hang the process (any prompt exit is fine here;
        the exit-code contract is asserted in the xfail test below)."""
        path = deeply_nested_plan(tmp_path / "deep.json")
        proc = run_cli_subprocess("analyze", "--plan", path, timeout=30)
        assert proc.returncode != 0  # never accepted

    def test_deeply_nested_json_fails_closed_exit_2(self, tmp_path):
        """BUG-1 (increment-1 report) fixed in b39ee7f: deep nesting now maps
        to the R2 contract."""
        path = deeply_nested_plan(tmp_path / "deep.json")
        proc = run_cli_subprocess("analyze", "--plan", path, timeout=30)
        assert proc.returncode == 2
        assert "Traceback" not in proc.stderr
        assert len([ln for ln in proc.stderr.splitlines() if ln.strip()]) == 1

    def test_huge_flat_resource_changes_handled(self, tmp_path):
        """A wide (not deep) plan parses fine and deterministically."""
        changes = [
            make_change(address=f"aws_instance.x{i}", actions=["create"])
            for i in range(2000)
        ]
        path = write_plan(tmp_path, make_plan(changes, provider_region="us-east-1"))
        proc = run_cli_subprocess("analyze", "--plan", path)
        assert proc.returncode == 0
        assert json.loads(proc.stdout)["summary"]["created"] == 2000
