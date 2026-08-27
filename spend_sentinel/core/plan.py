"""Plan ingestion: load a ``terraform show -json`` plan file and classify changes.

Implements R1 (parse the consumed subset), R2 (fail-closed error behavior for
missing/unreadable/non-JSON/structurally invalid input, surfaced as
:class:`PlanError`), and R3 (action classification with no-op exclusion).

Security posture (per the spec's Security considerations):

* a 50 MB size cap is enforced before the file is read;
* all parsing goes through the pydantic models in ``core.models`` and fails
  closed on invalid structure;
* error messages never echo file contents or environment variables — they name
  field locations and error kinds only. The caller (the CLI) prefixes the
  offending file path.

This module performs file I/O only in :func:`load_plan`; classification
(:func:`classify_actions`, :func:`summarize_plan`) is pure.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from spend_sentinel.core.models import (
    ActionClass,
    ClassifiedChange,
    Plan,
    PlanSummary,
)

#: Maximum accepted plan file size in bytes (spec: 50 MB cap on untrusted input).
MAX_PLAN_BYTES: int = 50 * 1024 * 1024


class PlanError(Exception):
    """A plan file could not be ingested.

    The message is a single line describing the problem. It intentionally does
    not include the file path (the caller adds it) and never includes file
    contents.
    """


def load_plan(path: str | Path) -> Plan:
    """Load and validate a Terraform plan JSON file (R1, R2).

    Raises:
        PlanError: if the file is missing, unreadable, over the 50 MB cap,
            not valid JSON, or does not validate against the consumed subset
            of the ``terraform show -json`` schema.
    """
    plan_path = Path(path)

    try:
        size = plan_path.stat().st_size
    except FileNotFoundError:
        raise PlanError("plan file not found") from None
    except OSError:
        raise PlanError("plan file is not readable") from None

    if plan_path.is_dir():
        raise PlanError("plan path is a directory, not a file")
    if size > MAX_PLAN_BYTES:
        raise PlanError("plan file exceeds the 50 MB size cap")

    try:
        raw = plan_path.read_bytes()
    except OSError:
        raise PlanError("plan file is not readable") from None

    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PlanError("plan file is not valid JSON") from None
    except RecursionError:
        # BUG-1: CPython's JSON parser raises RecursionError on hostile,
        # deeply nested input; map it to the R2 contract (exit 2, one line).
        raise PlanError("plan JSON is too deeply nested") from None

    if not isinstance(data, dict):
        raise PlanError("plan JSON is not an object")
    if "format_version" not in data:
        raise PlanError("plan JSON lacks required key 'format_version'")
    if "resource_changes" not in data:
        raise PlanError("plan JSON lacks required key 'resource_changes'")

    try:
        plan = Plan.model_validate(data)
    except ValidationError as exc:
        raise PlanError(_describe_validation_error(exc)) from None
    except RecursionError:
        raise PlanError("plan JSON is too deeply nested") from None

    fv = plan.format_version
    if fv != "1" and not fv.startswith("1."):
        raise PlanError("unsupported plan format_version (expected 1.x)")

    return plan


def _describe_validation_error(exc: ValidationError) -> str:
    """Summarize a pydantic error without echoing any input values."""
    first = exc.errors(include_input=False, include_url=False)[0]
    location = ".".join(str(part) for part in first["loc"]) or "<root>"
    return f"plan JSON is structurally invalid at '{location}' ({first['type']})"


def classify_actions(actions: Sequence[str]) -> ActionClass | None:
    """Classify a resource change's ``actions`` list per R3.

    Returns ``None`` for changes excluded from evaluation (``no-op``, and
    ``read`` for data sources). Raises :class:`PlanError` for any other
    unrecognized combination (fail closed).
    """
    ordered = list(actions)
    if ordered in (["no-op"], ["read"]):
        return None
    if ordered == ["create"]:
        return ActionClass.CREATE
    if ordered == ["delete"]:
        return ActionClass.DELETE
    if ordered == ["update"]:
        return ActionClass.UPDATE
    if sorted(ordered) == ["create", "delete"]:
        return ActionClass.REPLACE
    raise PlanError(f"unrecognized change actions {ordered!r}")


def summarize_plan(plan: Plan) -> tuple[PlanSummary, list[ClassifiedChange]]:
    """Classify and count every non-no-op resource change (R3). Pure function.

    Returns the summary counts and the classified changes in plan order.

    Raises:
        PlanError: if any resource change carries an unrecognized action
            combination.
    """
    counts: dict[ActionClass, int] = {action: 0 for action in ActionClass}
    classified: list[ClassifiedChange] = []

    for index, rc in enumerate(plan.resource_changes):
        try:
            action = classify_actions(rc.change.actions)
        except PlanError as exc:
            raise PlanError(f"resource_changes[{index}] ('{rc.address}'): {exc}") from None
        if action is None:
            continue
        counts[action] += 1
        classified.append(
            ClassifiedChange(
                address=rc.address,
                type=rc.type,
                provider=rc.provider_name,
                action=action,
            )
        )

    summary = PlanSummary(
        created=counts[ActionClass.CREATE],
        deleted=counts[ActionClass.DELETE],
        updated=counts[ActionClass.UPDATE],
        replaced=counts[ActionClass.REPLACE],
    )
    return summary, classified


def resolve_plan_region(plan: Plan) -> str | None:
    """Extract a constant AWS provider region from the plan's configuration (R8).

    Pure function. Returns the first constant ``region`` found among the AWS
    provider configurations (spec assumption A1: single-region plans; the first
    constant wins; ``--region`` overrides at the CLI). Returns ``None`` when no
    AWS provider carries a constant region — the caller must then require
    ``--region``.
    """
    configuration = plan.configuration
    if configuration is None or configuration.provider_config is None:
        return None
    for key in sorted(configuration.provider_config):
        pc = configuration.provider_config[key]
        is_aws = pc.name == "aws" or key == "aws" or key.startswith("aws.")
        if not is_aws or pc.expressions is None:
            continue
        region_expr = pc.expressions.get("region")
        if not isinstance(region_expr, dict):
            continue
        constant = region_expr.get("constant_value")
        if isinstance(constant, str) and constant:
            return constant
    return None
