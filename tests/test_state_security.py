"""Security tests for the increment-3 surface: state-file ingestion follows the
same fail-closed contract as plans (50 MB cap, hostile JSON, no content echo),
and sensitive_values masking in drift output (A-i15).
"""

from __future__ import annotations

import json

import pytest

from spend_sentinel.adapters.fixture_reader import FixtureAwsReader
from spend_sentinel.core.drift import SENSITIVE_PLACEHOLDER, detect
from spend_sentinel.core.models import State
from spend_sentinel.core.plan import MAX_PLAN_BYTES, PlanError
from spend_sentinel.core.state import load_state

from .conftest import (
    make_change,
    make_plan,
    make_state,
    make_state_resource,
    run_analyze,
    write_plan,
    write_state,
)

SECRET = "hunter2-STATE-SECRET-91c4e0"


@pytest.fixture()
def plan_path(tmp_path):
    return write_plan(
        tmp_path,
        make_plan(
            [make_change(actions=["create"], after={"instance_type": "t3.micro"})],
            provider_region="us-east-1",
        ),
    )


def assert_state_error_contract(result, state_path: str, *needles: str):
    assert result.exit_code == 2
    assert result.stdout == ""
    lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected one diagnostic line, got: {result.stderr!r}"
    assert state_path in lines[0]
    for needle in needles:
        assert needle in lines[0]


class TestStateFailClosed:
    def test_state_missing_file_exit_2_names_path(self, runner, plan_path, tmp_path):
        missing = str(tmp_path / "no-state.json")
        result = run_analyze(runner, plan_path, "--state", missing)
        assert_state_error_contract(result, missing, "not found")

    def test_state_malformed_json_exit_2(self, runner, plan_path, tmp_path):
        p = tmp_path / "bad-state.json"
        p.write_text("{broken")
        result = run_analyze(runner, plan_path, "--state", str(p))
        assert_state_error_contract(result, str(p), "not valid JSON")

    def test_state_missing_format_version_exit_2(self, runner, plan_path, tmp_path):
        p = tmp_path / "fv-state.json"
        p.write_text(json.dumps({"values": {"root_module": {"resources": []}}}))
        result = run_analyze(runner, plan_path, "--state", str(p))
        assert_state_error_contract(result, str(p), "format_version")

    def test_state_unsupported_format_version_exit_2(self, runner, plan_path, tmp_path):
        p = tmp_path / "fv2-state.json"
        p.write_text(json.dumps({"format_version": "2.0"}))
        result = run_analyze(runner, plan_path, "--state", str(p))
        assert_state_error_contract(result, str(p), "format_version")

    def test_state_non_object_top_level_exit_2(self, runner, plan_path, tmp_path):
        p = tmp_path / "arr-state.json"
        p.write_text("[1, 2]")
        result = run_analyze(runner, plan_path, "--state", str(p))
        assert_state_error_contract(result, str(p))

    def test_state_over_50mb_exit_2(self, runner, plan_path, tmp_path):
        big = tmp_path / "big-state.json"
        with big.open("wb") as f:
            f.truncate(MAX_PLAN_BYTES + 1)
        result = run_analyze(runner, plan_path, "--state", str(big))
        assert_state_error_contract(result, str(big), "50 MB")

    def test_state_deeply_nested_json_fails_closed(self, runner, plan_path, tmp_path):
        deep = tmp_path / "deep-state.json"
        deep.write_text(
            '{"format_version": "1.0", "x": ' + "[" * 100_000 + "]" * 100_000 + "}"
        )
        result = run_analyze(runner, plan_path, "--state", str(deep))
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert_state_error_contract(result, str(deep), "nested")

    def test_state_hostile_structure_exit_2(self, runner, plan_path, tmp_path):
        p = write_state(
            tmp_path,
            {"format_version": "1.0", "values": {"root_module": {"resources": "nope"}}},
        )
        result = run_analyze(runner, plan_path, "--state", p)
        assert_state_error_contract(result, p)

    def test_state_diagnostics_never_echo_contents(self, runner, plan_path, tmp_path):
        p = write_state(
            tmp_path,
            {"format_version": {"leak": SECRET}, "values": None},
        )
        result = run_analyze(runner, plan_path, "--state", p)
        assert result.exit_code == 2
        assert SECRET not in result.stderr
        assert SECRET not in (result.stdout or "")

    def test_load_state_raises_planerror_single_line(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{nope")
        with pytest.raises(PlanError) as excinfo:
            load_state(p)
        assert "\n" not in str(excinfo.value)

    def test_load_state_empty_state_is_valid(self, tmp_path):
        """A-i20: an empty state (no values) is legitimate, zero resources."""
        p = tmp_path / "empty.json"
        p.write_text(json.dumps({"format_version": "1.0"}))
        state = load_state(p)
        from spend_sentinel.core.state import iter_state_resources

        assert iter_state_resources(state) == []


def masked_drift_report(sensitive_values):
    resource = make_state_resource(
        values={"id": "i-1", "instance_type": "t3.micro",
                "tags": {"Name": "web", "Token": SECRET}},
        sensitive_values=sensitive_values,
    )
    state = State.model_validate(make_state([resource]))
    reader = FixtureAwsReader(
        {"instances": {"i-1": {"instance_type": "t3.large",
                               "tags": {"Name": "web", "Token": "live-" + SECRET}}}}
    )
    return detect(state, reader)


class TestSensitiveMasking:
    def test_sensitive_tags_masked_both_sides(self):
        report = masked_drift_report({"tags": {"Token": True}})
        by_attr = {d.attribute: d for d in report.drifts}
        tags = by_attr["tags"]
        assert tags.state_value == SENSITIVE_PLACEHOLDER
        assert tags.live_value == SENSITIVE_PLACEHOLDER
        assert SECRET not in json.dumps(tags.state_value) + json.dumps(tags.live_value)

    def test_unmarked_attribute_not_masked(self):
        """Masking is per-attribute: instance_type stays visible when only
        tags are marked."""
        report = masked_drift_report({"tags": {"Token": True}})
        by_attr = {d.attribute: d for d in report.drifts}
        assert by_attr["instance_type"].state_value == "t3.micro"
        assert by_attr["instance_type"].live_value == "t3.large"

    def test_whole_attribute_mark_masks(self):
        report = masked_drift_report({"tags": True})
        by_attr = {d.attribute: d for d in report.drifts}
        assert by_attr["tags"].state_value == SENSITIVE_PLACEHOLDER

    def test_empty_mirror_is_not_a_mark_a_i15(self):
        """A-i15: empty mirror structures ({}, [{}]) are not sensitivity marks."""
        report = masked_drift_report({"tags": {}})
        by_attr = {d.attribute: d for d in report.drifts}
        assert by_attr["tags"].state_value != SENSITIVE_PLACEHOLDER

    def test_sensitive_value_never_reaches_cli_output(self, runner, plan_path, tmp_path,
                                                      monkeypatch):
        import spend_sentinel.cli as cli

        state_path = write_state(
            tmp_path,
            make_state(
                [
                    make_state_resource(
                        values={"id": "i-1", "instance_type": "t3.micro",
                                "tags": {"Token": SECRET}},
                        sensitive_values={"tags": {"Token": True}},
                    )
                ]
            ),
        )
        reader = FixtureAwsReader(
            {"instances": {"i-1": {"instance_type": "t3.micro",
                                   "tags": {"Token": "rotated"}}}}
        )
        monkeypatch.setattr(cli, "_make_live_reader", lambda region: reader)
        result = run_analyze(runner, plan_path, "--state", state_path)
        assert result.exit_code == 0
        assert SECRET not in result.stdout
        payload = json.loads(result.stdout)
        tags = [d for d in payload["drift"]["drifts"] if d["attribute"] == "tags"]
        assert tags[0]["state_value"] == SENSITIVE_PLACEHOLDER
        assert tags[0]["live_value"] == SENSITIVE_PLACEHOLDER


class TestErrorSummarySanitization:
    def test_hostile_error_message_cannot_inject_report_lines(self, runner, plan_path,
                                                              tmp_path, monkeypatch):
        """A reader exception carrying newlines/ANSI must reach the JSON as a
        single sanitized line (state-derived stderr never involved)."""
        import spend_sentinel.cli as cli

        state_path = write_state(
            tmp_path,
            make_state(
                [make_state_resource(values={"id": "i-1", "instance_type": "t3.micro",
                                             "tags": {}})]
            ),
        )
        reader = FixtureAwsReader(
            {"errors": {"i-1": 'boom\n"verdict": "PASS"\x1b[32m spoof'}}
        )
        monkeypatch.setattr(cli, "_make_live_reader", lambda region: reader)
        result = run_analyze(runner, plan_path, "--state", state_path)
        assert result.exit_code == 2
        payload = json.loads(result.stdout)
        error = payload["drift"]["errors"][0]["error"]
        assert "\n" not in error
        assert "\x1b" not in error
