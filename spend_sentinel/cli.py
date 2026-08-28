"""spend-sentinel CLI (thin wiring only; business logic lives in ``core``).

``spend-sentinel analyze`` loads a Terraform plan (``terraform show -json``),
classifies its changes (R1-R3), prices them from the bundled snapshot
(R4-R8), optionally detects drift against live AWS (R9-R12), evaluates the
policy gates (R13-R17), and emits the verdict (R18-R20): Markdown to stdout by
default, or to files via ``--out-json``/``--out-md``.

Exit codes (R18, A5): 0 for PASS and (by default) WARN; 1 for BLOCK, and for
WARN with ``--fail-on-warn``; 2 for usage/runtime errors (R2/R8/R13 ingestion
failures — which write no output files — and R12 drift read errors, which
still produce the verdict but are outranked by an exit 1). Diagnostics are one
sanitized line on stderr — no tracebacks, no file contents.

The live boto3 adapter is imported only when drift will actually run
(R11/R21); this module is the only production wiring point (Modularity notes).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import click

from spend_sentinel import __version__
from spend_sentinel.core.cost import estimate
from spend_sentinel.core.drift import AwsReader, detect, skipped_report
from spend_sentinel.core.models import DriftStatus, VerdictMeta
from spend_sentinel.core.plan import (
    PlanError,
    load_plan,
    resolve_plan_region,
    summarize_plan,
)
from spend_sentinel.core.policy import DEFAULT_POLICY_FILENAME, evaluate, load_policy
from spend_sentinel.core.state import load_state
from spend_sentinel.core.verdict import combine, exit_code
from spend_sentinel.pricing.snapshot import SnapshotError, SnapshotPricingSource
from spend_sentinel.render.jsonout import render_json
from spend_sentinel.render.markdown import render_md

if TYPE_CHECKING:
    from spend_sentinel.pricing.live import LivePricingSource


def _fail(message: str) -> NoReturn:
    """One-line diagnostic on stderr, exit 2 (R2/R8/R13). Never echoes file contents.

    Plan-derived identifiers (resource addresses, provider_config keys, a
    plan-constant region) can reach diagnostics per A-i5 and are
    attacker-influenced in a PR context; control characters are replaced with
    spaces so a crafted plan cannot break R2's one-line contract or spoof
    additional diagnostic lines on stderr.
    """
    sanitized = "".join(ch if ch.isprintable() else " " for ch in message)
    click.echo(f"spend-sentinel: error: {sanitized}", err=True)
    sys.exit(2)


@click.group()
@click.version_option(version=__version__, prog_name="spend-sentinel")
def main() -> None:
    """Terraform drift & cost sentinel."""


@main.command()
@click.version_option(version=__version__, prog_name="spend-sentinel")
@click.option(
    "--plan",
    "plan_path",
    required=True,
    type=str,
    help="Path to a Terraform plan JSON file (output of `terraform show -json`).",
)
@click.option(
    "--region",
    "region_flag",
    default=None,
    type=str,
    help="Pricing region (overrides the region in the plan's provider configuration).",
)
@click.option(
    "--state",
    "state_path",
    default=None,
    type=str,
    help="Path to a Terraform state JSON file (output of `terraform show -json`) "
    "to check for drift against live AWS.",
)
@click.option(
    "--skip-drift",
    is_flag=True,
    default=False,
    help="Skip drift detection even when --state is given; no AWS call is made.",
)
@click.option(
    "--policy",
    "policy_flag",
    default=None,
    type=str,
    help="Path to a policy YAML file (default: ./spend-sentinel.yaml if present, "
    "else built-in defaults).",
)
@click.option(
    "--out-json",
    "out_json",
    default=None,
    type=str,
    help="Write the JSON verdict to this path (schema: docs/verdict-schema.md).",
)
@click.option(
    "--out-md",
    "out_md",
    default=None,
    type=str,
    help="Write the Markdown report to this path. With neither --out-json nor "
    "--out-md, the Markdown goes to stdout.",
)
@click.option(
    "--fail-on-warn",
    is_flag=True,
    default=False,
    help="Exit 1 on a WARN verdict (default: WARN exits 0).",
)
@click.option(
    "--live-pricing",
    is_flag=True,
    default=False,
    help="Resolve rates from the AWS Pricing API (pricing:GetProducts, needs "
    "the [aws] extra) with per-key fallback to the bundled snapshot; any "
    "failure degrades to snapshot and never fails the run. Endpoint region "
    "defaults to us-east-1 (override: SPEND_SENTINEL_PRICING_ENDPOINT_REGION).",
)
def analyze(
    plan_path: str,
    region_flag: str | None,
    state_path: str | None,
    skip_drift: bool,
    policy_flag: str | None,
    out_json: str | None,
    out_md: str | None,
    fail_on_warn: bool,
    live_pricing: bool,
) -> None:
    """Analyze a Terraform plan: cost delta, drift, policy gates, verdict."""
    try:
        plan = load_plan(plan_path)
        summary, _resources = summarize_plan(plan)
    except PlanError as exc:
        _fail(f"{plan_path}: {exc}")

    # Policy resolution (R13): --policy wins, else ./spend-sentinel.yaml if
    # present, else built-in defaults. Loaded early so a bad policy fails fast
    # (exit 2, no output files written - AC10).
    policy_path: str | None = policy_flag
    if policy_path is None and Path(DEFAULT_POLICY_FILENAME).is_file():
        policy_path = DEFAULT_POLICY_FILENAME
    try:
        policy = load_policy(policy_path)
    except PlanError as exc:
        _fail(f"{policy_path}: {exc}")

    try:
        pricing = SnapshotPricingSource()
    except SnapshotError as exc:
        _fail(str(exc))

    region = region_flag or resolve_plan_region(plan)
    if region is None:
        _fail(
            f"{plan_path}: no constant provider region found in the plan's "
            "configuration; pass --region"
        )
    if region not in pricing.supported_regions:
        supported = ", ".join(pricing.supported_regions)
        _fail(f"region '{region}' is not in the pricing snapshot (supported: {supported})")

    # Live pricing (v1.1, R22/R24/R27): opt-in; the default path constructs
    # exactly the v1 snapshot source and imports no live-pricing module.
    live_source = _make_live_pricing_source(pricing) if live_pricing else None
    cost = estimate(plan, live_source if live_source is not None else pricing, region)

    # Drift (R9-R12). R11: with --skip-drift or no --state, no AwsReader call
    # path is exercised and the boto3 adapter module is never imported.
    if skip_drift or state_path is None:
        drift = skipped_report()
    else:
        try:
            state = load_state(state_path)
        except PlanError as exc:
            _fail(f"{state_path}: {exc}")
        reader = _make_live_reader(region)
        drift = detect(state, reader)

    # Verdict (R18, R19).
    results = evaluate(policy, cost, drift, plan)
    verdict = combine(
        summary,
        cost,
        drift,
        results,
        VerdictMeta(
            tool_version=__version__,
            pricing_snapshot_version=pricing.meta.version,
            pricing_snapshot_date=pricing.meta.snapshot_date,
            region=region,
            live_pricing=live_source.report() if live_source is not None else None,
        ),
    )

    # R27: one stderr line per distinct degradation reason; never fails the
    # run and never changes the exit code (A11). Reasons are internal enums.
    if verdict.meta.live_pricing is not None:
        seen_reasons: set[str] = set()
        for warning in verdict.meta.live_pricing.warnings:
            if warning.reason not in seen_reasons:
                seen_reasons.add(warning.reason)
                click.echo(
                    "spend-sentinel: warning: live pricing degraded "
                    f"({warning.reason}); snapshot fallback used",
                    err=True,
                )

    # Outputs (R19): files when requested; Markdown to stdout with no flags.
    if out_json is not None:
        try:
            Path(out_json).write_text(render_json(verdict), encoding="utf-8")
        except OSError:
            _fail(f"{out_json}: cannot write JSON verdict file")
    if out_md is not None:
        try:
            Path(out_md).write_text(render_md(verdict), encoding="utf-8")
        except OSError:
            _fail(f"{out_md}: cannot write Markdown report file")
    if out_json is None and out_md is None:
        click.echo(render_md(verdict), nl=False)

    # R12: read failures still surface on stderr (count only — addresses and
    # error text are attacker-influenced and live in the report instead).
    errors = drift.status is DriftStatus.RAN and bool(drift.errors)
    if errors:
        click.echo(
            f"spend-sentinel: error: {len(drift.errors)} resource(s) could not be "
            "read during drift detection (see drift.errors in the report)",
            err=True,
        )

    # Exit mapping (R18) with A5 precedence: BLOCK's 1 outranks the error 2.
    code = exit_code(verdict, errors=errors, fail_on_warn=fail_on_warn)
    if code != 0:
        sys.exit(code)


def _make_live_pricing_source(snapshot: SnapshotPricingSource) -> LivePricingSource:
    """Wire live pricing (v1.1); imported only here and only under --live-pricing.

    Never fails the run: a transport that cannot be built yields a source
    pre-disabled with the run-level reason (boto3_missing/client_init_error).
    """
    from spend_sentinel.adapters.boto3_pricing import (
        Boto3PricingClient,
        PricingClientUnavailable,
        resolve_endpoint_region,
    )
    from spend_sentinel.pricing.live import LivePricingSource

    endpoint_region = resolve_endpoint_region()
    try:
        client = Boto3PricingClient()
    except PricingClientUnavailable as exc:
        return LivePricingSource(
            None, snapshot, endpoint_region=endpoint_region, disabled_reason=exc.reason
        )
    return LivePricingSource(client, snapshot, endpoint_region=endpoint_region)


def _make_live_reader(region: str) -> AwsReader:
    """Wire the boto3 adapter; imported only here and only when drift runs (R21)."""
    from spend_sentinel.adapters.boto3_reader import Boto3AwsReader, Boto3NotInstalledError

    try:
        return Boto3AwsReader(region=region)
    except Boto3NotInstalledError as exc:
        _fail(str(exc))


if __name__ == "__main__":
    main()
