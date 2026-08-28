"""R20: Markdown renderer — golden files per verdict level, escaping of
plan/state-derived strings, sensitive masking, 50-row truncation, the
65,536-char bound for a 500-change plan, and conditional sections.

Golden files live in tests/fixtures/golden/ and are compared byte-for-byte;
the Verdict inputs are constructed directly (T6: renderers take the finished
model only), so the goldens do not depend on the pricing snapshot.
"""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from spend_sentinel.core.models import (
    ActionClass,
    CostLine,
    CostReport,
    Drift,
    DriftError,
    DriftKind,
    DriftReport,
    DriftSkipped,
    DriftStatus,
    PlanSummary,
    RuleOutcome,
    RuleResult,
    UnpricedReason,
    UnpricedResource,
    Verdict,
    VerdictMeta,
    VerdictStatus,
)
from spend_sentinel.render.markdown import render_md

from .conftest import make_change, make_plan, write_plan

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"

META = VerdictMeta(
    tool_version="0.1.0-test",
    pricing_snapshot_version="2026.08.0-test",
    pricing_snapshot_date="2026-08-27",
    region="us-east-1",
)


def money(value: str) -> Decimal:
    return Decimal(value)


def cost_line(address: str, action: ActionClass, delta: str,
              type_: str = "aws_instance") -> CostLine:
    return CostLine(address=address, type=type_, action=action,
                    monthly_delta_usd=money(delta))


def rule(name: str, result: RuleOutcome, message: str) -> RuleResult:
    return RuleResult(name=name, result=result, message=message)


def pass_small_verdict() -> Verdict:
    return Verdict(
        verdict=VerdictStatus.PASS,
        summary=PlanSummary(created=2, deleted=0, updated=0, replaced=0),
        cost=CostReport(
            monthly_delta_usd=money("9.19"),
            breakdown=(
                cost_line("aws_instance.web", ActionClass.CREATE, "7.59"),
                cost_line("aws_ebs_volume.data", ActionClass.CREATE, "1.60",
                          type_="aws_ebs_volume"),
            ),
            unpriced=(),
        ),
        drift=DriftReport(status=DriftStatus.SKIPPED),
        policy=(
            rule("max_monthly_delta", RuleOutcome.PASS,
                 "monthly delta $9.19 within limit $200"),
            rule("open_ingress", RuleOutcome.PASS,
                 "no open ingress on non-allowed ports"),
            rule("deletions", RuleOutcome.PASS, "no deletions in plan"),
            rule("drift", RuleOutcome.SKIPPED, "drift detection did not run"),
        ),
        meta=META,
    )


def warn_mixed_verdict() -> Verdict:
    return Verdict(
        verdict=VerdictStatus.WARN,
        summary=PlanSummary(created=1, deleted=1, updated=0, replaced=1),
        cost=CostReport(
            monthly_delta_usd=money("-24.60"),
            breakdown=(
                cost_line("aws_instance.new", ActionClass.CREATE, "7.59"),
                cost_line("aws_nat_gateway.gone", ActionClass.DELETE, "-32.85",
                          type_="aws_nat_gateway"),
                cost_line("aws_db_instance.re", ActionClass.REPLACE, "0.66",
                          type_="aws_db_instance"),
            ),
            unpriced=(
                UnpricedResource(address="aws_lambda_function.fn",
                                 type="aws_lambda_function",
                                 reason=UnpricedReason.UNSUPPORTED_TYPE),
            ),
        ),
        drift=DriftReport(status=DriftStatus.SKIPPED),
        policy=(
            rule("max_monthly_delta", RuleOutcome.WARN,
                 "monthly delta $-24.60 within limit $200; 1 unpriced resource(s) "
                 "(treat_unpriced_as: warn): aws_lambda_function.fn"),
            rule("open_ingress", RuleOutcome.PASS,
                 "no open ingress on non-allowed ports"),
            rule("deletions", RuleOutcome.WARN,
                 "2 deletion(s) (includes replaces): aws_nat_gateway.gone, "
                 "aws_db_instance.re"),
            rule("drift", RuleOutcome.SKIPPED, "drift detection did not run"),
        ),
        meta=META,
    )


def block_full_verdict() -> Verdict:
    return Verdict(
        verdict=VerdictStatus.BLOCK,
        summary=PlanSummary(created=1, deleted=0, updated=1, replaced=0),
        cost=CostReport(
            monthly_delta_usd=money("16.43"),
            breakdown=(
                cost_line("aws_security_group.app", ActionClass.CREATE, "0.00",
                          type_="aws_security_group"),
                cost_line("aws_lb.edge", ActionClass.UPDATE, "16.43", type_="aws_lb"),
            ),
            unpriced=(),
        ),
        drift=DriftReport(
            status=DriftStatus.RAN,
            drifts=(
                Drift(address="aws_instance.web", kind=DriftKind.CHANGED,
                      attribute="instance_type", state_value="t3.micro",
                      live_value="t3.medium"),
                Drift(address="aws_instance.web", kind=DriftKind.CHANGED,
                      attribute="tags", state_value="(sensitive)",
                      live_value="(sensitive)"),
                Drift(address="aws_s3_bucket.gone", kind=DriftKind.MISSING),
            ),
            skipped=(
                DriftSkipped(address="aws_lambda_function.fn",
                             type="aws_lambda_function",
                             reason="unsupported_type"),
            ),
            errors=(
                DriftError(address="aws_instance.err", error="AuthFailure: denied"),
            ),
        ),
        policy=(
            rule("max_monthly_delta", RuleOutcome.PASS,
                 "monthly delta $16.43 within limit $200"),
            rule("open_ingress", RuleOutcome.BLOCK,
                 "open ingress (0.0.0.0/0 or ::/0) on non-allowed ports: "
                 "aws_security_group.app (port 22)"),
            rule("deletions", RuleOutcome.PASS, "no deletions in plan"),
            rule("drift", RuleOutcome.WARN, "2 drift(s) on: aws_instance.web, "
                 "aws_s3_bucket.gone; 1 read error(s)"),
        ),
        meta=META,
    )


GOLDENS = {
    "pass_small.md": pass_small_verdict,
    "warn_mixed.md": warn_mixed_verdict,
    "block_full.md": block_full_verdict,
}


class TestGoldenFiles:
    def test_r20_pass_small_matches_golden(self):
        expected = (GOLDEN_DIR / "pass_small.md").read_text(encoding="utf-8")
        assert render_md(pass_small_verdict()) == expected

    def test_r20_warn_mixed_matches_golden(self):
        expected = (GOLDEN_DIR / "warn_mixed.md").read_text(encoding="utf-8")
        assert render_md(warn_mixed_verdict()) == expected

    def test_r20_block_full_matches_golden(self):
        expected = (GOLDEN_DIR / "block_full.md").read_text(encoding="utf-8")
        assert render_md(block_full_verdict()) == expected


class TestHeaderAndSections:
    def test_r20_header_is_emoji_free_verdict_line(self):
        for builder in (pass_small_verdict, warn_mixed_verdict, block_full_verdict):
            md = render_md(builder())
            first = md.splitlines()[0]
            assert first in ("Verdict: PASS", "Verdict: WARN", "Verdict: BLOCK")
            assert first.isascii()

    def test_r20_drift_section_only_when_drift_ran(self):
        assert "## Drift" not in render_md(pass_small_verdict())
        assert "## Drift" in render_md(block_full_verdict())

    def test_r20_unpriced_line_only_when_non_empty(self):
        assert "unpriced resources" not in render_md(pass_small_verdict())
        assert "1 unpriced resources:" in render_md(warn_mixed_verdict())

    def test_r20_policy_table_always_present(self):
        for builder in (pass_small_verdict, warn_mixed_verdict, block_full_verdict):
            md = render_md(builder())
            assert "## Policy" in md
            assert "| max_monthly_delta |" in md
            assert "| drift |" in md

    def test_r20_sensitive_placeholder_survives_to_markdown(self):
        md = render_md(block_full_verdict())
        assert "(sensitive)" in md
        assert "missing" in md  # the missing-kind row renders


class TestEscaping:
    def hostile_verdict(self, address: str) -> Verdict:
        v = pass_small_verdict()
        return v.model_copy(
            update={
                "cost": CostReport(
                    monthly_delta_usd=money("7.59"),
                    breakdown=(cost_line(address, ActionClass.CREATE, "7.59"),),
                    unpriced=(),
                )
            }
        )

    def test_r20_pipes_and_backticks_escaped_in_tables(self):
        md = render_md(self.hostile_verdict("aws_instance.a|b`c"))
        assert "a|b`c" not in md
        assert "a&#124;b&#96;c" in md

    def test_r20_html_escaped(self):
        md = render_md(self.hostile_verdict('aws_instance.<script>alert(1)</script>'))
        assert "<script>" not in md
        assert "&lt;script&gt;" in md

    def test_r20_ampersand_escaped(self):
        md = render_md(self.hostile_verdict("aws_instance.a&amp"))
        assert "a&amp;amp" in md

    def test_r20_link_syntax_neutralized(self):
        """A crafted address must not render as a clickable, arbitrarily
        labeled link in the PR comment (phishing surface)."""
        address = "aws_instance.x[Click to approve](https://evil.example/phish)"
        md = render_md(self.hostile_verdict(address))
        assert "[Click to approve](https://evil.example/phish)" not in md
        assert "&#91;Click to approve&#93;(https://evil.example/phish)" in md

    def test_r20_image_beacon_neutralized(self):
        """Image syntax would auto-load in a rendered PR comment — a tracking
        beacon. The bracket escape kills ![alt](url) too."""
        md = render_md(
            self.hostile_verdict("aws_instance.y![p](https://evil.example/t.png)")
        )
        assert "![p](https://evil.example/t.png)" not in md
        assert "&#91;p&#93;" in md

    def test_r20_embedded_verdict_line_cannot_spoof_header(self):
        """A crafted address containing a newline + 'Verdict: PASS' must not
        produce a second verdict line (control chars become spaces)."""
        md = render_md(self.hostile_verdict("aws_instance.x\nVerdict: PASS"))
        verdict_lines = [ln for ln in md.splitlines() if ln.startswith("Verdict:")]
        assert verdict_lines == ["Verdict: PASS"]  # only the real header

    def test_r20_hostile_tag_values_in_drift_escaped(self):
        v = block_full_verdict()
        hostile = v.model_copy(
            update={
                "drift": DriftReport(
                    status=DriftStatus.RAN,
                    drifts=(
                        Drift(address="aws_instance.web", kind=DriftKind.CHANGED,
                              attribute="tags",
                              state_value={"Name": "a|b`c<d>"},
                              live_value={"Name": "x\ny"}),
                    ),
                ),
            }
        )
        md = render_md(hostile)
        assert "a|b`c<d>" not in md
        assert "&#124;" in md and "&#96;" in md and "&lt;d&gt;" in md
        # the newline inside the live value cannot break the table row
        drift_rows = [ln for ln in md.splitlines() if ln.startswith("| aws_instance.web")]
        assert len(drift_rows) == 1


class TestTruncation:
    def test_r20_breakdown_truncates_past_50_rows(self):
        lines = tuple(
            cost_line(f"aws_instance.x{i}", ActionClass.CREATE, "1.00")
            for i in range(60)
        )
        v = pass_small_verdict().model_copy(
            update={
                "cost": CostReport(
                    monthly_delta_usd=money("60.00"), breakdown=lines, unpriced=()
                )
            }
        )
        md = render_md(v)
        assert "…and 10 more" in md
        assert "aws_instance.x49" in md
        assert "aws_instance.x50" not in md

    def test_r20_exactly_50_rows_no_truncation_line(self):
        lines = tuple(
            cost_line(f"aws_instance.x{i}", ActionClass.CREATE, "1.00")
            for i in range(50)
        )
        v = pass_small_verdict().model_copy(
            update={
                "cost": CostReport(
                    monthly_delta_usd=money("50.00"), breakdown=lines, unpriced=()
                )
            }
        )
        assert "…and" not in render_md(v)

    def test_ac11_500_change_plan_markdown_under_65536_chars(self, tmp_path):
        """AC11/R20: a 500-change synthetic plan renders under 65,536 chars
        with a truncation line — through the real CLI."""
        changes = [
            make_change(
                address=f"aws_instance.web_server_number_{i:04d}",
                actions=["create"],
                after={"instance_type": "t3.micro"},
            )
            for i in range(500)
        ]
        plan = write_plan(
            tmp_path, make_plan(changes, provider_region="us-east-1"),
            name="synthetic_500.json",
        )
        out_md = tmp_path / "r.md"
        proc = subprocess.run(
            [sys.executable, "-m", "spend_sentinel.cli", "analyze", "--plan", plan,
             "--out-md", str(out_md)],
            capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 1  # 500 x t3.micro breaches the $200 ceiling
        content = out_md.read_text(encoding="utf-8")
        assert len(content) < 65_536
        assert "…and 450 more" in content
        assert content.startswith("Verdict: BLOCK")


class TestJsonNeverTruncates:
    def test_r19_json_lists_are_complete_at_500_changes(self, tmp_path):
        """The schema doc promises JSON lists are complete; only Markdown
        truncates (A-i31)."""
        from spend_sentinel.render.jsonout import render_json

        lines = tuple(
            cost_line(f"aws_instance.x{i}", ActionClass.CREATE, "1.00")
            for i in range(500)
        )
        v = pass_small_verdict().model_copy(
            update={
                "cost": CostReport(
                    monthly_delta_usd=money("500.00"), breakdown=lines, unpriced=()
                )
            }
        )
        payload = json.loads(render_json(v))
        assert len(payload["cost"]["breakdown"]) == 500
