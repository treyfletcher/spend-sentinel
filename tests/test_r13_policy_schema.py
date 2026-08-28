"""R13: policy config schema, loading, resolution order, and error contract.

Covers the owner decision on A-i21 (limit_usd defaults to 200; explicit null
means no ceiling), A-i24 (version optional, Literal[1]), A-i27 (50 MB cap),
hostile YAML fail-closed (safe_load only), and AC10's policy half (unknown
rule `max_cpu` -> exit 2 naming it, no output files).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from spend_sentinel.core.plan import MAX_PLAN_BYTES, PlanError
from spend_sentinel.core.policy import DEFAULT_POLICY_FILENAME, Policy, load_policy

from .conftest import make_change, make_plan, run_analyze, write_plan


def write_policy(tmp_path: Path, text: str, name: str = "policy.yaml") -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest.fixture()
def plan_path(tmp_path):
    return write_plan(
        tmp_path,
        make_plan(
            [make_change(actions=["create"], after={"instance_type": "t3.micro"})],
            provider_region="us-east-1",
        ),
    )


class TestDefaults:
    def test_r13_builtin_defaults(self):
        """Built-in defaults are the safe direction, with the owner-decided
        $200 ceiling."""
        policy = load_policy(None)
        rules = policy.rules
        assert rules.max_monthly_delta.limit_usd == Decimal("200")
        assert rules.max_monthly_delta.treat_unpriced_as == "warn"
        assert rules.open_ingress.allowed_ports == ()
        assert rules.deletions.action == "warn"
        assert rules.deletions.protected_types == ()
        assert rules.drift.action == "warn"
        assert policy.version == 1

    def test_r13_empty_file_means_defaults_not_no_rules(self, tmp_path):
        path = write_policy(tmp_path, "")
        assert load_policy(path) == Policy()

    def test_r13_null_document_means_defaults(self, tmp_path):
        path = write_policy(tmp_path, "null\n")
        assert load_policy(path) == Policy()

    def test_r13_partial_file_fills_defaults(self, tmp_path):
        path = write_policy(tmp_path, "rules:\n  deletions:\n    action: block\n")
        policy = load_policy(path)
        assert policy.rules.deletions.action == "block"
        assert policy.rules.max_monthly_delta.limit_usd == Decimal("200")
        assert policy.rules.drift.action == "warn"

    def test_r13_explicit_null_limit_means_no_ceiling(self, tmp_path):
        """Owner decision: `limit_usd: null` disables the ceiling."""
        path = write_policy(
            tmp_path, "rules:\n  max_monthly_delta:\n    limit_usd: null\n"
        )
        assert load_policy(path).rules.max_monthly_delta.limit_usd is None

    def test_r13_omitted_limit_gets_200_default(self, tmp_path):
        path = write_policy(
            tmp_path, "rules:\n  max_monthly_delta:\n    treat_unpriced_as: block\n"
        )
        policy = load_policy(path)
        assert policy.rules.max_monthly_delta.limit_usd == Decimal("200")
        assert policy.rules.max_monthly_delta.treat_unpriced_as == "block"

    def test_r13_version_optional_and_literal_1(self, tmp_path):
        assert load_policy(write_policy(tmp_path, "version: 1\n")).version == 1
        with pytest.raises(PlanError) as excinfo:
            load_policy(write_policy(tmp_path, "version: 2\n", name="v2.yaml"))
        assert "version" in str(excinfo.value)


class TestSchemaErrors:
    @pytest.mark.parametrize(
        ("yaml_text", "named_key"),
        [
            ("surprise: 1\n", "surprise"),  # unknown top-level key
            ("rules:\n  max_cpu:\n    limit: 5\n", "max_cpu"),  # unknown rule
            (  # unknown enum value
                "rules:\n  deletions:\n    action: explode\n",
                "deletions.action",
            ),
            (  # unknown enum value on treat_unpriced_as
                "rules:\n  max_monthly_delta:\n    treat_unpriced_as: maybe\n",
                "treat_unpriced_as",
            ),
            (  # wrong type: scalar where list expected
                "rules:\n  open_ingress:\n    allowed_ports: 80\n",
                "allowed_ports",
            ),
            (  # wrong type inside the list
                "rules:\n  open_ingress:\n    allowed_ports: [80, http]\n",
                "allowed_ports",
            ),
            (  # wrong type: string where Decimal expected
                "rules:\n  max_monthly_delta:\n    limit_usd: lots\n",
                "limit_usd",
            ),
            (  # unknown nested key inside a known rule
                "rules:\n  drift:\n    action: warn\n    severity: high\n",
                "severity",
            ),
            (  # wrong type: rules not a mapping
                "rules: []\n",
                "rules",
            ),
        ],
    )
    def test_r13_schema_violation_names_offending_key(self, tmp_path, yaml_text,
                                                      named_key):
        path = write_policy(tmp_path, yaml_text)
        with pytest.raises(PlanError) as excinfo:
            load_policy(path)
        message = str(excinfo.value)
        assert named_key in message
        assert "\n" not in message

    def test_r13_non_mapping_document_rejected(self, tmp_path):
        with pytest.raises(PlanError) as excinfo:
            load_policy(write_policy(tmp_path, "- just\n- a\n- list\n"))
        assert "mapping" in str(excinfo.value)

    def test_r13_invalid_yaml_rejected(self, tmp_path):
        with pytest.raises(PlanError) as excinfo:
            load_policy(write_policy(tmp_path, "rules: [unclosed\n"))
        assert "YAML" in str(excinfo.value)

    def test_r13_missing_policy_file_flag_is_error(self, tmp_path):
        with pytest.raises(PlanError) as excinfo:
            load_policy(tmp_path / "absent.yaml")
        assert "not found" in str(excinfo.value)


class TestHostileYaml:
    def test_r13_python_object_tag_fails_closed_no_execution(self, tmp_path, runner,
                                                             plan_path, monkeypatch):
        """safe_load only: a !!python tag must be rejected, never constructed."""
        workdir = tmp_path / "wd"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        canary = workdir / "pwned"
        payload = (
            "exploit: !!python/object/apply:pathlib.Path.touch "
            f'[!!python/object/apply:pathlib.Path ["{canary}"]]\n'
        )
        path = write_policy(tmp_path, payload)
        result = run_analyze(runner, plan_path, "--policy", path)
        assert result.exit_code == 2
        assert not canary.exists()
        assert "YAML" in result.stderr

    def test_r13_alias_heavy_yaml_fails_closed_quickly(self, tmp_path):
        """PyYAML aliases are shared references (no billion-laughs expansion);
        an alias-heavy document must still fail schema validation cleanly."""
        text = (
            'a: &a ["x","x","x","x","x","x","x","x","x","x"]\n'
            "b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a,*a]\n"
            "c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b,*b]\n"
            "d: [*c,*c,*c,*c,*c,*c,*c,*c,*c,*c]\n"
        )
        with pytest.raises(PlanError):
            load_policy(write_policy(tmp_path, text))

    def test_r13_deeply_nested_yaml_fails_closed(self, tmp_path):
        depth = 5000
        text = "a:\n" + "".join(
            f"{'  ' * (i + 1)}a:\n" for i in range(depth)
        )
        path = write_policy(tmp_path, text)
        with pytest.raises(PlanError):
            load_policy(path)

    def test_r13_policy_over_50mb_rejected(self, tmp_path):
        """A-i27: same size cap as plan/state, checked before the read."""
        big = tmp_path / "big.yaml"
        with big.open("wb") as f:
            f.truncate(MAX_PLAN_BYTES + 1)
        with pytest.raises(PlanError) as excinfo:
            load_policy(big)
        assert "50 MB" in str(excinfo.value)

    def test_r13_binary_policy_rejected(self, tmp_path):
        p = tmp_path / "bin.yaml"
        p.write_bytes(b"\x00\xff\xfe binary")
        with pytest.raises(PlanError):
            load_policy(p)

    def test_r13_diagnostics_never_echo_values(self, tmp_path):
        secret = "hunter2-POLICY-SECRET"
        path = write_policy(
            tmp_path, f"rules:\n  deletions:\n    action: {secret}\n"
        )
        with pytest.raises(PlanError) as excinfo:
            load_policy(path)
        assert secret not in str(excinfo.value)


class TestResolutionOrder:
    """--policy > ./spend-sentinel.yaml > built-in defaults (CWD-sensitive)."""

    def deletions_result(self, result):
        payload = json.loads(result.stdout)
        return {r["name"]: r["result"] for r in payload["policy"]["rules"]}["deletions"]

    @pytest.fixture()
    def delete_plan(self, tmp_path):
        return write_plan(
            tmp_path,
            make_plan(
                [
                    make_change(
                        address="aws_instance.gone",
                        actions=["delete"],
                        before={"instance_type": "t3.micro"},
                    )
                ],
                provider_region="us-east-1",
            ),
            name="delete_plan.json",
        )

    def test_r13_builtin_defaults_when_no_file(self, runner, delete_plan, tmp_path,
                                               monkeypatch):
        workdir = tmp_path / "empty-cwd"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        result = run_analyze(runner, delete_plan)
        assert result.exit_code == 0
        assert self.deletions_result(result) == "warn"  # default action

    def test_r13_cwd_file_auto_picked_up(self, runner, delete_plan, tmp_path,
                                         monkeypatch):
        workdir = tmp_path / "cwd"
        workdir.mkdir()
        (workdir / DEFAULT_POLICY_FILENAME).write_text(
            "rules:\n  deletions:\n    action: block\n"
        )
        monkeypatch.chdir(workdir)
        result = run_analyze(runner, delete_plan)
        assert result.exit_code == 0
        assert self.deletions_result(result) == "block"

    def test_r13_policy_flag_wins_over_cwd_file(self, runner, delete_plan, tmp_path,
                                                monkeypatch):
        workdir = tmp_path / "cwd2"
        workdir.mkdir()
        (workdir / DEFAULT_POLICY_FILENAME).write_text(
            "rules:\n  deletions:\n    action: block\n"
        )
        flag_policy = write_policy(
            tmp_path, "rules:\n  deletions:\n    action: ignore\n", name="flag.yaml"
        )
        monkeypatch.chdir(workdir)
        result = run_analyze(runner, delete_plan, "--policy", flag_policy)
        assert result.exit_code == 0
        assert self.deletions_result(result) == "pass"  # ignore renders pass (A-i26)

    def test_r13_broken_cwd_file_fails_run(self, runner, delete_plan, tmp_path,
                                           monkeypatch):
        """An invalid auto-picked ./spend-sentinel.yaml must fail the run, not
        silently fall back to defaults."""
        workdir = tmp_path / "cwd3"
        workdir.mkdir()
        (workdir / DEFAULT_POLICY_FILENAME).write_text("rules:\n  max_cpu: {}\n")
        monkeypatch.chdir(workdir)
        result = run_analyze(runner, delete_plan)
        assert result.exit_code == 2
        assert "max_cpu" in result.stderr


class TestAc10PolicyHalf:
    def test_ac10_unknown_rule_exit_2_names_key_no_output_files(
        self, runner, plan_path, tmp_path, monkeypatch
    ):
        workdir = tmp_path / "ac10"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        bad = write_policy(tmp_path, "rules:\n  max_cpu:\n    limit: 5\n")
        result = run_analyze(runner, plan_path, "--policy", bad)
        assert result.exit_code == 2
        assert result.stdout == ""
        lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert "max_cpu" in lines[0]
        assert bad in lines[0]
        assert list(workdir.iterdir()) == []
