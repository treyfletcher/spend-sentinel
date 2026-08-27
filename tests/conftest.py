"""Shared test helpers for the spend-sentinel increment-1 suite (R1-R3).

All tests are offline, deterministic, and order-independent. Fixture plans
live in tests/fixtures/plans/; large or hostile inputs are generated into
tmp_path at test time and never committed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

FIXTURES = Path(__file__).parent / "fixtures" / "plans"


def fixture_path(name: str) -> str:
    """Absolute path of a committed fixture plan."""
    path = FIXTURES / name
    assert path.is_file(), f"missing fixture: {path}"
    return str(path)


def make_plan(
    resource_changes: list[dict[str, Any]],
    format_version: str = "1.2",
    **extra: Any,
) -> dict[str, Any]:
    """Build a minimal plan JSON object."""
    plan: dict[str, Any] = {
        "format_version": format_version,
        "resource_changes": resource_changes,
    }
    plan.update(extra)
    return plan


def make_change(
    address: str = "aws_instance.example",
    type_: str = "aws_instance",
    actions: list[str] | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    provider_name: str = "registry.terraform.io/hashicorp/aws",
) -> dict[str, Any]:
    """Build one resource_changes entry."""
    return {
        "address": address,
        "mode": "managed",
        "type": type_,
        "name": address.split(".")[-1],
        "provider_name": provider_name,
        "change": {
            "actions": actions if actions is not None else ["create"],
            "before": before,
            "after": after,
        },
    }


def write_plan(tmp_path: Path, plan: dict[str, Any], name: str = "plan.json") -> str:
    """Serialize a plan dict into tmp_path and return its path as str."""
    path = tmp_path / name
    path.write_text(json.dumps(plan), encoding="utf-8")
    return str(path)


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def run_analyze(runner: CliRunner, plan_path: str):  # click Result (untyped)
    """Invoke `spend-sentinel analyze --plan <path>` through CliRunner."""
    from spend_sentinel.cli import main

    return runner.invoke(main, ["analyze", "--plan", plan_path])
