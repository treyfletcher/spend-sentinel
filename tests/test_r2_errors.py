"""R2: ingestion failures exit 2 with a one-line stderr diagnostic naming the
file and the problem; nothing on stdout; no output files written.

Also covers the coder's stated 50 MB size cap (PR assumption / spec security
section) and AC10a (nonexistent plan path).
"""

from __future__ import annotations

import json
import os

import pytest

from spend_sentinel.core.plan import MAX_PLAN_BYTES, PlanError, load_plan

from .conftest import fixture_path, make_change, make_plan, run_analyze, write_plan


def assert_error_contract(result, plan_path: str, *needles: str):
    """Exit 2, empty stdout, exactly one stderr line naming the path (+ needles)."""
    assert result.exit_code == 2
    assert result.stdout == ""
    lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected one diagnostic line, got: {result.stderr!r}"
    assert plan_path in lines[0]
    for needle in needles:
        assert needle in lines[0]


class TestMissingAndUnreadable:
    def test_r2_missing_file_exits_2_names_path(self, runner, tmp_path):
        """AC10a: nonexistent plan path -> exit 2, stderr names the path."""
        missing = str(tmp_path / "does-not-exist.json")
        result = run_analyze(runner, missing)
        assert_error_contract(result, missing, "not found")

    def test_r2_missing_file_writes_no_output_files(self, runner, tmp_path, monkeypatch):
        """AC10a: no output files are created on the error path."""
        workdir = tmp_path / "cwd"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        result = run_analyze(runner, str(tmp_path / "nope.json"))
        assert result.exit_code == 2
        assert list(workdir.iterdir()) == []

    def test_r2_directory_as_plan_exits_2(self, runner, tmp_path):
        d = tmp_path / "adir"
        d.mkdir()
        result = run_analyze(runner, str(d))
        assert_error_contract(result, str(d))

    def test_r2_path_through_regular_file_exits_2(self, runner, tmp_path):
        """A path whose parent component is a file raises OSError, not
        FileNotFoundError — must still be a clean exit 2."""
        blocker = tmp_path / "blocker"
        blocker.write_text("{}")
        bad = str(blocker / "child.json")
        result = run_analyze(runner, bad)
        assert_error_contract(result, bad)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
    def test_r2_unreadable_file_exits_2(self, runner, tmp_path):
        p = tmp_path / "secret.json"
        p.write_text("{}")
        p.chmod(0o000)
        try:
            result = run_analyze(runner, str(p))
            assert_error_contract(result, str(p))
        finally:
            p.chmod(0o644)


class TestNotJson:
    def test_r2_malformed_json_exits_2(self, runner):
        path = fixture_path("malformed.json")
        result = run_analyze(runner, path)
        assert_error_contract(result, path, "not valid JSON")

    def test_r2_empty_file_exits_2(self, runner, tmp_path):
        p = tmp_path / "empty.json"
        p.write_text("")
        result = run_analyze(runner, str(p))
        assert_error_contract(result, str(p))

    def test_r2_binary_non_utf8_exits_2(self, runner, tmp_path):
        p = tmp_path / "binary.json"
        p.write_bytes(b"\xff\xfe\x00\x01\x80garbage")
        result = run_analyze(runner, str(p))
        assert_error_contract(result, str(p))

    def test_r2_json_scalar_top_level_exits_2(self, runner, tmp_path):
        p = tmp_path / "scalar.json"
        p.write_text("42")
        result = run_analyze(runner, str(p))
        assert_error_contract(result, str(p))

    def test_r2_json_array_top_level_exits_2(self, runner, tmp_path):
        p = tmp_path / "arr.json"
        p.write_text("[1, 2, 3]")
        result = run_analyze(runner, str(p))
        assert_error_contract(result, str(p))


class TestMissingKeysAndStructure:
    def test_r2_missing_format_version_exits_2(self, runner):
        path = fixture_path("missing_format_version.json")
        result = run_analyze(runner, path)
        assert_error_contract(result, path, "format_version")

    def test_r2_missing_resource_changes_exits_2(self, runner):
        path = fixture_path("missing_resource_changes.json")
        result = run_analyze(runner, path)
        assert_error_contract(result, path, "resource_changes")

    def test_r2_unsupported_format_version_exits_2(self, runner):
        path = fixture_path("unsupported_format_version.json")
        result = run_analyze(runner, path)
        assert_error_contract(result, path, "format_version")

    def test_r2_null_resource_changes_exits_2(self, runner, tmp_path):
        path = write_plan(tmp_path, {"format_version": "1.2", "resource_changes": None})
        result = run_analyze(runner, path)
        assert_error_contract(result, path)

    def test_r2_resource_change_entry_not_object_exits_2(self, runner, tmp_path):
        path = write_plan(tmp_path, make_plan(["not-an-object"]))
        result = run_analyze(runner, path)
        assert_error_contract(result, path)

    def test_r2_change_missing_actions_exits_2(self, runner, tmp_path):
        entry = make_change()
        del entry["change"]["actions"]
        path = write_plan(tmp_path, make_plan([entry]))
        result = run_analyze(runner, path)
        assert_error_contract(result, path)

    def test_r2_actions_wrong_type_exits_2(self, runner, tmp_path):
        entry = make_change()
        entry["change"]["actions"] = "create"
        path = write_plan(tmp_path, make_plan([entry]))
        result = run_analyze(runner, path)
        assert_error_contract(result, path)

    def test_r2_missing_address_exits_2(self, runner, tmp_path):
        entry = make_change()
        del entry["address"]
        path = write_plan(tmp_path, make_plan([entry]))
        result = run_analyze(runner, path)
        assert_error_contract(result, path)


class TestSizeCap:
    def test_r2_over_50mb_exits_2(self, runner, tmp_path):
        """One byte over the cap fails before any read (sparse file, no I/O)."""
        big = tmp_path / "big.json"
        with big.open("wb") as f:
            f.truncate(MAX_PLAN_BYTES + 1)
        result = run_analyze(runner, str(big))
        assert_error_contract(result, str(big), "50 MB")

    def test_r2_exactly_50mb_is_accepted(self, tmp_path):
        """Boundary: exactly MAX_PLAN_BYTES passes the cap (content is a valid
        plan padded with JSON whitespace)."""
        body = json.dumps({"format_version": "1.2", "resource_changes": []})
        payload = body + " " * (MAX_PLAN_BYTES - len(body))
        assert len(payload) == MAX_PLAN_BYTES
        p = tmp_path / "exact.json"
        p.write_text(payload)
        plan = load_plan(p)
        assert plan.resource_changes == []

    def test_r2_cap_constant_is_50mb(self):
        assert MAX_PLAN_BYTES == 50 * 1024 * 1024


class TestLoadPlanUnit:
    def test_r2_load_plan_raises_planerror_not_oserror(self, tmp_path):
        with pytest.raises(PlanError):
            load_plan(tmp_path / "missing.json")

    def test_r2_planerror_message_is_one_line(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json")
        with pytest.raises(PlanError) as excinfo:
            load_plan(p)
        assert "\n" not in str(excinfo.value)
