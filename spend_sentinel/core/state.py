"""State ingestion: load a ``terraform show -json`` state file (R9).

Reuses the plan loader's fail-closed pattern (:func:`~spend_sentinel.core.plan.
load_json_document`): 50 MB size cap, pydantic validation, one-line
:class:`~spend_sentinel.core.plan.PlanError` diagnostics that never echo file
contents. An empty state (``terraform show -json`` with no resources) is valid
and yields zero resources.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from spend_sentinel.core.models import State, StateModule, StateResource
from spend_sentinel.core.plan import PlanError, load_json_document


def load_state(path: str | Path) -> State:
    """Load and validate a Terraform state JSON file, fail-closed.

    Raises:
        PlanError: if the file is missing, unreadable, over the 50 MB cap,
            not valid JSON, lacks ``format_version``, or does not validate
            against the consumed subset.
    """
    data = load_json_document(path, "state")

    if "format_version" not in data:
        raise PlanError("state JSON lacks required key 'format_version'")

    try:
        state = State.model_validate(data)
    except ValidationError as exc:
        raise PlanError(_describe_validation_error(exc)) from None
    except RecursionError:
        raise PlanError("state JSON is too deeply nested") from None

    fv = state.format_version
    if fv != "1" and not fv.startswith("1."):
        raise PlanError("unsupported state format_version (expected 1.x)")

    return state


def _describe_validation_error(exc: ValidationError) -> str:
    """Summarize a pydantic error without echoing any input values."""
    first = exc.errors(include_input=False, include_url=False)[0]
    location = ".".join(str(part) for part in first["loc"]) or "<root>"
    return f"state JSON is structurally invalid at '{location}' ({first['type']})"


def iter_state_resources(state: State) -> list[StateResource]:
    """Flatten managed resources from the root module and all child modules.

    Pure function; data-source resources (``mode != "managed"``) are excluded
    from drift detection.
    """
    if state.values is None or state.values.root_module is None:
        return []
    collected: list[StateResource] = []
    stack: list[StateModule] = [state.values.root_module]
    while stack:  # iterative: hostile module nesting cannot blow the Python stack
        module = stack.pop()
        collected.extend(module.resources)
        stack.extend(module.child_modules)
    return [r for r in collected if r.mode == "managed"]
