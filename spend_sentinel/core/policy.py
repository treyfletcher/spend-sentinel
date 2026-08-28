"""Policy gates (R13-R17): config schema, loader, and the four rule evaluators.

The config schema is EXACTLY R13's keys and no others (``extra="forbid"`` at
every level); an unknown top-level key, unknown rule name, unknown enum value,
or wrong type surfaces as a :class:`~spend_sentinel.core.plan.PlanError`
naming the offending key (exit 2 at the CLI). Defaults are the safe direction
(spec Policy bypass hardening): ``treat_unpriced_as: warn``,
``allowed_ports: []``, deletions ``warn``, drift ``warn`` — an empty or absent
policy file means these built-in defaults, never "no rules".

Loading uses ``yaml.safe_load`` only, under the same 50 MB cap as plan/state
files. Evaluation is pure: ``evaluate(policy, cost, drift, plan)`` returns one
:class:`~spend_sentinel.core.models.RuleResult` per rule (R17); messages name
offending resources with control characters stripped and long lists elided
(the same injection hardening as CLI diagnostics).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from spend_sentinel.core.models import (
    ActionClass,
    CostReport,
    DriftReport,
    DriftStatus,
    Plan,
    RuleOutcome,
    RuleResult,
)
from spend_sentinel.core.plan import MAX_PLAN_BYTES, PlanError, classify_actions

#: Default policy file looked up in the CWD when ``--policy`` is not given (R13).
DEFAULT_POLICY_FILENAME = "spend-sentinel.yaml"

_MAX_LISTED = 5  # addresses named in a rule message before "and N more"


# --- R13 config schema (exactly these keys and no others) -------------------


class MaxMonthlyDeltaRule(BaseModel):
    """``rules.max_monthly_delta`` (R13, R14).

    ``limit_usd`` defaults to 200 USD (owner decision on A-i21): built-in
    defaults and policy files omitting the key get a $200 ceiling.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    limit_usd: Decimal | None = Decimal("200")
    treat_unpriced_as: Literal["warn", "ignore", "block"] = "warn"


class OpenIngressRule(BaseModel):
    """``rules.open_ingress`` (R13, R15)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_ports: tuple[int, ...] = ()

    @field_validator("allowed_ports", mode="before")
    @classmethod
    def _reject_bool_ports(cls, value: Any) -> Any:
        """R13 wrong-type contract: YAML ``true``/``false`` are not port numbers.

        pydantic's lax mode coerces bool -> int, which would silently turn a
        truthy typo into "port 1 is exempt"; bools fail closed instead,
        matching the estimator's bool-is-not-a-number convention (A-i8).
        """
        if isinstance(value, list | tuple) and any(isinstance(v, bool) for v in value):
            raise ValueError("ports must be integers, not booleans")
        return value

    @property
    def allowed(self) -> frozenset[int]:
        return frozenset(self.allowed_ports)


class DeletionsRule(BaseModel):
    """``rules.deletions`` (R13, R16)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["warn", "block", "ignore"] = "warn"
    protected_types: tuple[str, ...] = ()


class DriftRule(BaseModel):
    """``rules.drift`` (R13)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["warn", "block", "ignore"] = "warn"


class PolicyRules(BaseModel):
    """``rules`` — exactly the four rule names (R13)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_monthly_delta: MaxMonthlyDeltaRule = Field(default_factory=MaxMonthlyDeltaRule)
    open_ingress: OpenIngressRule = Field(default_factory=OpenIngressRule)
    deletions: DeletionsRule = Field(default_factory=DeletionsRule)
    drift: DriftRule = Field(default_factory=DriftRule)


class Policy(BaseModel):
    """The validated policy config; constructing it bare yields the defaults."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    rules: PolicyRules = Field(default_factory=PolicyRules)


# --- Loading (R13 error contract) -------------------------------------------


def load_policy(path: str | Path | None) -> Policy:
    """Load and validate a policy YAML file; ``None`` means built-in defaults.

    An empty file (or one containing only ``null``) also yields the defaults —
    never "no rules" (spec Policy bypass hardening).

    Raises:
        PlanError: missing/unreadable/oversized file, invalid YAML, or a
            schema violation — the message names the offending key and never
            echoes file contents.
    """
    if path is None:
        return Policy()

    policy_path = Path(path)
    try:
        size = policy_path.stat().st_size
    except FileNotFoundError:
        raise PlanError("policy file not found") from None
    except OSError:
        raise PlanError("policy file is not readable") from None
    if policy_path.is_dir():
        raise PlanError("policy path is a directory, not a file")
    if size > MAX_PLAN_BYTES:
        raise PlanError("policy file exceeds the 50 MB size cap")

    try:
        raw = policy_path.read_bytes()
    except OSError:
        raise PlanError("policy file is not readable") from None

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        raise PlanError("policy file is not valid YAML") from None
    except RecursionError:
        raise PlanError("policy YAML is too deeply nested") from None

    if data is None:
        return Policy()
    if not isinstance(data, dict):
        raise PlanError("policy YAML is not a mapping")

    try:
        return Policy.model_validate(data)
    except ValidationError as exc:
        raise PlanError(_describe_validation_error(exc)) from None


def _describe_validation_error(exc: ValidationError) -> str:
    """Name the offending key (R13) without echoing input values."""
    first = exc.errors(include_input=False, include_url=False)[0]
    loc = [str(part) for part in first["loc"]]
    error_type = first["type"]
    if error_type == "extra_forbidden":
        offender = loc[-1] if loc else "<root>"
        return f"policy contains unknown key '{'.'.join(loc) or offender}'"
    location = ".".join(loc) or "<root>"
    if error_type in ("literal_error", "enum"):
        return f"policy key '{location}' has an unknown value"
    return f"policy key '{location}' has the wrong type ({error_type})"


# --- Evaluation (R14-R17) ---------------------------------------------------


def evaluate(
    policy: Policy, cost: CostReport, drift: DriftReport, plan: Plan
) -> list[RuleResult]:
    """Evaluate the four R13 rules; every rule appears in the output (R17)."""
    return [
        _eval_max_monthly_delta(policy.rules.max_monthly_delta, cost),
        _eval_open_ingress(policy.rules.open_ingress, plan),
        _eval_deletions(policy.rules.deletions, plan),
        _eval_drift(policy.rules.drift, drift),
    ]


def _clean(text: str) -> str:
    """Strip control characters from plan-derived strings placed in messages."""
    return "".join(ch if ch.isprintable() else " " for ch in text)


def _listed(addresses: list[str]) -> str:
    shown = [_clean(a) for a in addresses[:_MAX_LISTED]]
    extra = len(addresses) - len(shown)
    return ", ".join(shown) + (f" and {extra} more" if extra > 0 else "")


def _eval_max_monthly_delta(rule: MaxMonthlyDeltaRule, cost: CostReport) -> RuleResult:
    """R14: block over limit; unpriced escalate to at least warn/block."""
    outcome = RuleOutcome.PASS
    parts: list[str] = []

    if rule.limit_usd is not None and cost.monthly_delta_usd > rule.limit_usd:
        outcome = RuleOutcome.BLOCK
        parts.append(
            f"monthly delta ${cost.monthly_delta_usd} exceeds limit ${rule.limit_usd}"
        )
    elif rule.limit_usd is not None:
        parts.append(
            f"monthly delta ${cost.monthly_delta_usd} within limit ${rule.limit_usd}"
        )
    else:
        parts.append(f"monthly delta ${cost.monthly_delta_usd}; no limit configured")

    if cost.unpriced and rule.treat_unpriced_as != "ignore":
        escalation = (
            RuleOutcome.BLOCK if rule.treat_unpriced_as == "block" else RuleOutcome.WARN
        )
        if _severity(escalation) > _severity(outcome):
            outcome = escalation
        parts.append(
            f"{len(cost.unpriced)} unpriced resource(s) "
            f"(treat_unpriced_as: {rule.treat_unpriced_as}): "
            + _listed([u.address for u in cost.unpriced])
        )
    elif cost.unpriced:
        parts.append(f"{len(cost.unpriced)} unpriced resource(s) ignored by policy")

    return RuleResult(
        name="max_monthly_delta", result=outcome, message="; ".join(parts)
    )


_SEVERITY = {RuleOutcome.PASS: 0, RuleOutcome.WARN: 1, RuleOutcome.BLOCK: 2}


def _severity(outcome: RuleOutcome) -> int:
    return _SEVERITY[outcome]


# -- open_ingress (R15, assumption A7) --


_INGRESS_TYPES = frozenset(
    {"aws_security_group", "aws_security_group_rule", "aws_vpc_security_group_ingress_rule"}
)
_OPEN_V4 = "0.0.0.0/0"
_OPEN_V6 = "::/0"


def _eval_open_ingress(rule: OpenIngressRule, plan: Plan) -> RuleResult:
    """R15: block open ingress unless every port in the rule's range is allowed."""
    offenders: list[str] = []
    for rc in plan.resource_changes:
        action = classify_actions(rc.change.actions)
        if action is None or action is ActionClass.DELETE:
            continue  # only resources that will exist after apply
        if rc.type not in _INGRESS_TYPES:
            continue
        after = rc.change.after
        if not isinstance(after, dict):
            continue
        for ports in _open_ingress_ranges(rc.type, after):
            if not _range_fully_allowed(ports, rule.allowed):
                offenders.append(f"{_clean(rc.address)} ({_describe_ports(ports)})")

    if offenders:
        return RuleResult(
            name="open_ingress",
            result=RuleOutcome.BLOCK,
            message="open ingress (0.0.0.0/0 or ::/0) on non-allowed ports: "
            + _listed(offenders),
        )
    return RuleResult(
        name="open_ingress",
        result=RuleOutcome.PASS,
        message="no open ingress on non-allowed ports",
    )


def _open_ingress_ranges(
    rtype: str, after: dict[str, Any]
) -> list[tuple[int | None, int | None, bool]]:
    """(from_port, to_port, all_ports) tuples for each open ingress rule in ``after``.

    Only IPv4/IPv6 CIDRs make a rule open; SG references and prefix lists never
    do (A7). ``all_ports`` marks protocol ``-1``.
    """
    found: list[tuple[int | None, int | None, bool]] = []
    if rtype == "aws_security_group":
        ingress = after.get("ingress")
        if isinstance(ingress, list):
            for entry in ingress:
                if isinstance(entry, dict) and _rule_is_open(entry):
                    found.append(_ports_of(entry, "protocol"))
    elif rtype == "aws_security_group_rule":
        if after.get("type") == "ingress" and _rule_is_open(after):
            found.append(_ports_of(after, "protocol"))
    else:  # aws_vpc_security_group_ingress_rule (always ingress)
        if after.get("cidr_ipv4") == _OPEN_V4 or after.get("cidr_ipv6") == _OPEN_V6:
            found.append(_ports_of(after, "ip_protocol"))
    return found


def _rule_is_open(entry: dict[str, Any]) -> bool:
    v4 = entry.get("cidr_blocks")
    v6 = entry.get("ipv6_cidr_blocks")
    open_v4 = isinstance(v4, list) and _OPEN_V4 in v4
    open_v6 = isinstance(v6, list) and _OPEN_V6 in v6
    return open_v4 or open_v6


def _ports_of(
    entry: dict[str, Any], protocol_key: str
) -> tuple[int | None, int | None, bool]:
    protocol = entry.get(protocol_key)
    all_ports = str(protocol) == "-1"
    from_port = entry.get("from_port")
    to_port = entry.get("to_port")
    return (
        from_port if isinstance(from_port, int) else None,
        to_port if isinstance(to_port, int) else None,
        all_ports,
    )


def _range_fully_allowed(
    ports: tuple[int | None, int | None, bool], allowed: frozenset[int]
) -> bool:
    """True only when every port in the range is in ``allowed`` (R15)."""
    from_port, to_port, all_ports = ports
    if all_ports:  # protocol -1 blocks regardless
        return False
    if from_port is None or to_port is None or to_port < from_port:
        return False  # unverifiable range fails closed
    if to_port - from_port + 1 > len(allowed):
        return False
    return all(port in allowed for port in range(from_port, to_port + 1))


def _describe_ports(ports: tuple[int | None, int | None, bool]) -> str:
    from_port, to_port, all_ports = ports
    if all_ports:
        return "all ports, protocol -1"
    if from_port is None or to_port is None:
        return "unspecified ports"
    if from_port == to_port:
        return f"port {from_port}"
    return f"ports {from_port}-{to_port}"


# -- deletions (R16) --


def _eval_deletions(rule: DeletionsRule, plan: Plan) -> RuleResult:
    """R16: warn/block/ignore deletions; protected types always block."""
    deleted: list[str] = []
    protected: list[str] = []
    protected_types = set(rule.protected_types)
    for rc in plan.resource_changes:
        action = classify_actions(rc.change.actions)
        if action not in (ActionClass.DELETE, ActionClass.REPLACE):
            continue  # replaces count as deletions (R16)
        deleted.append(rc.address)
        if rc.type in protected_types:
            protected.append(rc.address)

    if protected:
        return RuleResult(
            name="deletions",
            result=RuleOutcome.BLOCK,
            message=f"deletion of protected type(s): {_listed(protected)}"
            + (
                f"; {len(deleted)} deletion(s) total: {_listed(deleted)}"
                if len(deleted) > len(protected)
                else ""
            ),
        )
    if not deleted:
        return RuleResult(
            name="deletions", result=RuleOutcome.PASS, message="no deletions in plan"
        )
    if rule.action == "ignore":
        return RuleResult(
            name="deletions",
            result=RuleOutcome.PASS,
            message=f"{len(deleted)} deletion(s) ignored by policy: {_listed(deleted)}",
        )
    outcome = RuleOutcome.BLOCK if rule.action == "block" else RuleOutcome.WARN
    return RuleResult(
        name="deletions",
        result=outcome,
        message=f"{len(deleted)} deletion(s) (includes replaces): {_listed(deleted)}",
    )


# -- drift (R11, R17) --


def _eval_drift(rule: DriftRule, drift: DriftReport) -> RuleResult:
    """Drift rule: ``skipped`` when drift did not run (R11), else per config."""
    if drift.status is DriftStatus.SKIPPED:
        return RuleResult(
            name="drift",
            result=RuleOutcome.SKIPPED,
            message="drift detection did not run",
        )
    suffix = f"; {len(drift.errors)} read error(s)" if drift.errors else ""
    if not drift.drifts:
        return RuleResult(
            name="drift", result=RuleOutcome.PASS, message="no drift detected" + suffix
        )
    addresses = sorted({d.address for d in drift.drifts})
    if rule.action == "ignore":
        return RuleResult(
            name="drift",
            result=RuleOutcome.PASS,
            message=f"{len(drift.drifts)} drift(s) ignored by policy: "
            f"{_listed(addresses)}{suffix}",
        )
    outcome = RuleOutcome.BLOCK if rule.action == "block" else RuleOutcome.WARN
    return RuleResult(
        name="drift",
        result=outcome,
        message=f"{len(drift.drifts)} drift(s) on: {_listed(addresses)}{suffix}",
    )
