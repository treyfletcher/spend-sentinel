"""spend-sentinel CLI (thin wiring only; business logic lives in ``core``).

Increment 1 (R1-R3): ``spend-sentinel analyze --plan <path>`` loads and
classifies a Terraform plan and prints a minimal JSON summary to stdout.
Increment 2 (R4-R8) adds ``--region`` and a ``cost`` section (monthly delta,
per-resource breakdown, unpriced list) priced from the bundled snapshot.
Exit codes: 0 on success, 2 on ingestion/region errors (R2, R8) with a one-line
stderr diagnostic — no traceback, no file contents. The full verdict structure
(R19) arrives in a later increment.
"""

from __future__ import annotations

import json
import sys
from typing import Any, NoReturn

import click

from spend_sentinel import __version__
from spend_sentinel.core.cost import estimate
from spend_sentinel.core.plan import (
    PlanError,
    load_plan,
    resolve_plan_region,
    summarize_plan,
)
from spend_sentinel.pricing.snapshot import SnapshotError, SnapshotPricingSource


def _fail(message: str) -> NoReturn:
    """One-line diagnostic on stderr, exit 2 (R2/R8). Never echoes file contents.

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
def analyze(plan_path: str, region_flag: str | None) -> None:
    """Analyze a Terraform plan: classify changes and estimate the monthly cost delta."""
    try:
        plan = load_plan(plan_path)
        summary, resources = summarize_plan(plan)
    except PlanError as exc:
        _fail(f"{plan_path}: {exc}")

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

    cost = estimate(plan, pricing, region)

    output: dict[str, Any] = {
        "summary": {
            "created": summary.created,
            "deleted": summary.deleted,
            "updated": summary.updated,
            "replaced": summary.replaced,
            "changed": summary.changed,
        },
        "resources": [
            {
                "address": resource.address,
                "type": resource.type,
                "provider": resource.provider,
                "action": resource.action.value,
            }
            for resource in resources
        ],
        "cost": {
            "region": region,
            "monthly_delta_usd": str(cost.monthly_delta_usd),
            "breakdown": [
                {
                    "address": line.address,
                    "type": line.type,
                    "action": line.action.value,
                    "monthly_delta_usd": str(line.monthly_delta_usd),
                }
                for line in cost.breakdown
            ],
            "unpriced": [
                {
                    "address": entry.address,
                    "type": entry.type,
                    "reason": entry.reason.value,
                }
                for entry in cost.unpriced
            ],
        },
    }
    click.echo(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
