"""Verdict aggregation and exit-code mapping (R18, R19, A5). Pure logic.

``combine`` folds the finished cost/drift/policy results into the
:class:`~spend_sentinel.core.models.Verdict` model that both renderers consume;
``exit_code`` maps the verdict to the CI contract of R18 with the A5
precedence rule (a BLOCK's exit 1 outranks the runtime-error exit 2).
"""

from __future__ import annotations

from spend_sentinel.core.models import (
    CostReport,
    DriftReport,
    PlanSummary,
    RuleOutcome,
    RuleResult,
    Verdict,
    VerdictMeta,
    VerdictStatus,
)


def combine(
    summary: PlanSummary,
    cost: CostReport,
    drift: DriftReport,
    policy: list[RuleResult],
    meta: VerdictMeta,
) -> Verdict:
    """Build the aggregate verdict: BLOCK if any rule blocks, else WARN if any
    warns, else PASS (R18); ``skipped`` rules affect nothing."""
    outcomes = {r.result for r in policy}
    if RuleOutcome.BLOCK in outcomes:
        status = VerdictStatus.BLOCK
    elif RuleOutcome.WARN in outcomes:
        status = VerdictStatus.WARN
    else:
        status = VerdictStatus.PASS
    return Verdict(
        verdict=status,
        summary=summary,
        cost=cost,
        drift=drift,
        policy=tuple(policy),
        meta=meta,
    )


def exit_code(verdict: Verdict, errors: bool, fail_on_warn: bool) -> int:
    """Map the verdict to the process exit code (R18, R12, A5).

    * BLOCK -> 1;
    * WARN with ``--fail-on-warn`` -> 1 (it gates CI like a block, so it also
      outranks the runtime-error 2, same A5 rationale);
    * runtime errors (e.g. drift read failures, R12) -> 2;
    * otherwise 0 (PASS, and WARN by default per A6).
    """
    if verdict.verdict is VerdictStatus.BLOCK:
        return 1
    if verdict.verdict is VerdictStatus.WARN and fail_on_warn:
        return 1
    if errors:
        return 2
    return 0
