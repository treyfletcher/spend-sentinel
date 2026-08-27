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

SNAPSHOT_PATH = (
    Path(__file__).parent.parent / "spend_sentinel" / "data" / "pricing_snapshot.json"
)


def load_snapshot() -> dict[str, Any]:
    """The bundled pricing snapshot, parsed fresh (expected values are computed
    FROM this file, never hardcoded)."""
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def aws_provider_config(region: str) -> dict[str, Any]:
    """A plan ``configuration`` block with a constant AWS provider region (R8)."""
    return {
        "provider_config": {
            "aws": {
                "name": "aws",
                "full_name": "registry.terraform.io/hashicorp/aws",
                "expressions": {"region": {"constant_value": region}},
            }
        }
    }


def fixture_path(name: str) -> str:
    """Absolute path of a committed fixture plan."""
    path = FIXTURES / name
    assert path.is_file(), f"missing fixture: {path}"
    return str(path)


def make_plan(
    resource_changes: list[dict[str, Any]],
    format_version: str = "1.2",
    provider_region: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a minimal plan JSON object; ``provider_region`` adds a constant
    AWS provider region to the ``configuration`` block (R8)."""
    plan: dict[str, Any] = {
        "format_version": format_version,
        "resource_changes": resource_changes,
    }
    if provider_region is not None:
        plan["configuration"] = aws_provider_config(provider_region)
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


def make_state_resource(
    address: str = "aws_instance.web",
    type_: str = "aws_instance",
    values: dict[str, Any] | None = None,
    mode: str = "managed",
    sensitive_values: Any = None,
) -> dict[str, Any]:
    """Build one state resource entry (increment 3, R9-R12)."""
    resource: dict[str, Any] = {
        "address": address,
        "mode": mode,
        "type": type_,
        "name": address.split(".")[-1],
        "provider_name": "registry.terraform.io/hashicorp/aws",
        "values": values if values is not None else {},
    }
    if sensitive_values is not None:
        resource["sensitive_values"] = sensitive_values
    return resource


def make_state(
    resources: list[dict[str, Any]],
    child_modules: list[dict[str, Any]] | None = None,
    format_version: str = "1.0",
) -> dict[str, Any]:
    """Build a minimal `terraform show -json` state document."""
    root: dict[str, Any] = {"resources": resources}
    if child_modules is not None:
        root["child_modules"] = child_modules
    return {"format_version": format_version, "values": {"root_module": root}}


def write_state(tmp_path: Path, state: dict[str, Any], name: str = "state.json") -> str:
    """Serialize a state dict into tmp_path and return its path as str."""
    path = tmp_path / name
    path.write_text(json.dumps(state), encoding="utf-8")
    return str(path)


def run_analyze(runner: CliRunner, plan_path: str, *extra_args: str):  # click Result
    """Invoke `spend-sentinel analyze --plan <path> [extra args]` through CliRunner."""
    from spend_sentinel.cli import main

    return runner.invoke(main, ["analyze", "--plan", plan_path, *extra_args])
