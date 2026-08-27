"""spend-sentinel CLI (thin wiring only; business logic lives in ``core``).

Increment 1 (R1-R3): ``spend-sentinel analyze --plan <path>`` loads and
classifies a Terraform plan and prints a minimal JSON summary to stdout.
Exit codes: 0 on success, 2 on ingestion errors (R2) with a one-line stderr
diagnostic naming the file and the problem — no traceback, no file contents.
The full verdict structure (R19) arrives in a later increment.
"""

from __future__ import annotations

import json
import sys

import click

from spend_sentinel import __version__
from spend_sentinel.core.plan import PlanError, load_plan, summarize_plan


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
def analyze(plan_path: str) -> None:
    """Analyze a Terraform plan: classify and count resource changes."""
    try:
        plan = load_plan(plan_path)
        summary, resources = summarize_plan(plan)
    except PlanError as exc:
        click.echo(f"spend-sentinel: error: {plan_path}: {exc}", err=True)
        sys.exit(2)

    output = {
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
    }
    click.echo(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
