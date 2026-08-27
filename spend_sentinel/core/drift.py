"""Drift detection (R9-R12): compare state attributes against live AWS values.

Pure logic: all live reads go through the :class:`AwsReader` protocol, so the
detector is testable with fakes and ``core`` never imports ``adapters`` (the
protocol is defined here and re-exported by ``adapters.aws_reader`` to match
the spec's layout).

Per-type comparators live in a registry ``dict[str, Comparator]`` keyed by
resource type (spec Modularity notes) covering exactly the R9 allowlists:

* ``aws_instance``: ``instance_type``, ``tags``;
* ``aws_security_group``: ingress and egress rule sets (protocol, ports, CIDR
  blocks), compared order-insensitively;
* ``aws_s3_bucket``: ``tags`` and versioning status.

R10: a supported resource absent from AWS is drift of kind ``missing``;
unsupported types land in ``skipped`` with reason ``unsupported_type``.
R12: an AWS read failure on one resource is captured in ``errors`` with a
sanitized exception summary and never kills the run.

Security: drift values derived from a state attribute marked in the resource's
``sensitive_values`` are replaced by ``"(sensitive)"`` in the report (spec
Data exposure note); error summaries are single-line, length-capped, and never
include credential material beyond what the exception message itself carries.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from spend_sentinel.core.models import (
    BucketAttrs,
    Drift,
    DriftError,
    DriftKind,
    DriftReport,
    DriftSkipped,
    DriftStatus,
    InstanceAttrs,
    SecurityGroupAttrs,
    SecurityGroupRule,
    State,
    StateResource,
)
from spend_sentinel.core.state import iter_state_resources

SENSITIVE_PLACEHOLDER = "(sensitive)"
_ERROR_SUMMARY_MAX = 200


class AwsReader(Protocol):
    """The three narrow read methods drift detection may use (R9, Security).

    Implementations return ``None`` when the resource does not exist and raise
    on read failures (auth, throttle, timeout); the detector maps ``None`` to
    ``missing`` drift and exceptions to per-resource errors (R10, R12).
    """

    def get_instance(self, instance_id: str) -> InstanceAttrs | None:
        """ec2:DescribeInstances for one instance id."""
        ...

    def get_security_group(self, sg_id: str) -> SecurityGroupAttrs | None:
        """ec2:DescribeSecurityGroups for one group id."""
        ...

    def get_bucket(self, name: str) -> BucketAttrs | None:
        """s3:GetBucketVersioning + s3:GetBucketTagging for one bucket."""
        ...


#: A comparator returns the drifts for one state resource, or a single
#: ``missing`` drift when the resource does not exist in AWS.
Comparator = Callable[[StateResource, AwsReader], list[Drift]]


def detect(state: State, reader: AwsReader) -> DriftReport:
    """Compare every supported managed state resource against live AWS (R9-R12)."""
    drifts: list[Drift] = []
    skipped: list[DriftSkipped] = []
    errors: list[DriftError] = []

    for resource in iter_state_resources(state):
        comparator = COMPARATORS.get(resource.type)
        if comparator is None:
            skipped.append(
                DriftSkipped(
                    address=resource.address, type=resource.type, reason="unsupported_type"
                )
            )
            continue
        try:
            drifts.extend(comparator(resource, reader))
        except Exception as exc:  # R12: one resource's failure must not kill the run
            errors.append(
                DriftError(address=resource.address, error=_summarize_exception(exc))
            )

    return DriftReport(
        status=DriftStatus.RAN,
        drifts=tuple(drifts),
        skipped=tuple(skipped),
        errors=tuple(errors),
    )


def skipped_report() -> DriftReport:
    """The R11 report when drift does not run (no ``--state`` or ``--skip-drift``)."""
    return DriftReport(status=DriftStatus.SKIPPED)


def _summarize_exception(exc: BaseException) -> str:
    """One-line, length-capped exception summary (R12).

    Control characters are replaced so a hostile message cannot inject lines
    into the JSON-embedded report; only the exception type and its message
    reach the report — never environment variables or credentials held by the
    tool (which accepts none).
    """
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    if len(text) > _ERROR_SUMMARY_MAX:
        text = text[: _ERROR_SUMMARY_MAX - 3] + "..."
    return text


def _is_sensitive(resource: StateResource, attribute: str) -> bool:
    """True when the state marks ``attribute`` sensitive (spec Data exposure)."""
    marks = resource.sensitive_values
    if not isinstance(marks, dict):
        return bool(marks)
    return _any_marked(marks.get(attribute))


def _any_marked(node: Any) -> bool:
    """Terraform's sensitive_values mirrors the value structure; a mark is any
    nested truthy leaf (e.g. ``{"tags": true}`` or ``{"tags": {"Secret": true}}``;
    an empty mirror like ``[{}]`` is not a mark)."""
    if isinstance(node, dict):
        return any(_any_marked(v) for v in node.values())
    if isinstance(node, list | tuple):
        return any(_any_marked(v) for v in node)
    return bool(node)


def _drift(
    resource: StateResource, attribute: str, state_value: Any, live_value: Any
) -> Drift:
    if _is_sensitive(resource, attribute):
        state_value = SENSITIVE_PLACEHOLDER
        live_value = SENSITIVE_PLACEHOLDER
    return Drift(
        address=resource.address,
        kind=DriftKind.CHANGED,
        attribute=attribute,
        state_value=state_value,
        live_value=live_value,
    )


def _missing(resource: StateResource) -> list[Drift]:
    return [Drift(address=resource.address, kind=DriftKind.MISSING)]


def _values(resource: StateResource) -> dict[str, Any]:
    return resource.values if isinstance(resource.values, dict) else {}


def _resource_id(resource: StateResource, key: str) -> str:
    """The lookup id for a state resource; missing id is a per-resource error."""
    value = _values(resource).get(key)
    if not isinstance(value, str) or not value:
        raise LookupError(f"state resource has no usable '{key}' attribute")
    return value


def _state_tags(resource: StateResource) -> dict[str, str]:
    raw = _values(resource).get("tags")
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if v is not None}


# --- aws_instance: instance_type, tags -------------------------------------


def _compare_instance(resource: StateResource, reader: AwsReader) -> list[Drift]:
    live = reader.get_instance(_resource_id(resource, "id"))
    if live is None:
        return _missing(resource)
    drifts: list[Drift] = []
    state_type = _values(resource).get("instance_type")
    if state_type != live.instance_type:
        drifts.append(_drift(resource, "instance_type", state_type, live.instance_type))
    state_tags = _state_tags(resource)
    if state_tags != live.tags:
        drifts.append(_drift(resource, "tags", state_tags, live.tags))
    return drifts


# --- aws_security_group: ingress/egress rule sets, order-insensitive --------


def _atomic_rules(rules: Any) -> set[tuple[str, int | None, int | None, str, str]]:
    """Expand rules to atomic (protocol, from, to, family, cidr) tuples.

    Order-insensitive by construction (a set), and robust to AWS grouping
    multiple CIDRs under one rule where state keeps them separate (R9).
    """
    atomic: set[tuple[str, int | None, int | None, str, str]] = set()
    if not isinstance(rules, list | tuple):
        return atomic
    for rule in rules:
        if isinstance(rule, SecurityGroupRule):
            protocol = rule.protocol
            from_port: int | None = rule.from_port
            to_port: int | None = rule.to_port
            v4 = list(rule.cidr_blocks)
            v6 = list(rule.ipv6_cidr_blocks)
        elif isinstance(rule, dict):
            protocol = str(rule.get("protocol", ""))
            from_port = rule.get("from_port") if isinstance(rule.get("from_port"), int) else None
            to_port = rule.get("to_port") if isinstance(rule.get("to_port"), int) else None
            raw_v4 = rule.get("cidr_blocks")
            raw_v6 = rule.get("ipv6_cidr_blocks")
            v4 = [str(c) for c in raw_v4] if isinstance(raw_v4, list) else []
            v6 = [str(c) for c in raw_v6] if isinstance(raw_v6, list) else []
        else:
            continue
        for cidr in v4:
            atomic.add((protocol, from_port, to_port, "ipv4", cidr))
        for cidr in v6:
            atomic.add((protocol, from_port, to_port, "ipv6", cidr))
    return atomic


def _render_rules(rules: set[tuple[str, int | None, int | None, str, str]]) -> list[str]:
    """Deterministic, JSON-friendly rendering of an atomic rule set (A-i19).

    The sort key covers the FULL atomic tuple (BUG-4: omitting ``to_port``
    let rules tying on the other fields fall back to hash-seed-dependent set
    iteration order, breaking cross-process determinism); ``None`` ports sort
    before numeric ones.
    """
    return [
        f"{protocol}:{from_port}-{to_port}:{cidr}"
        for protocol, from_port, to_port, _family, cidr in sorted(
            rules,
            key=lambda r: (
                r[0],
                -1 if r[1] is None else r[1],
                -1 if r[2] is None else r[2],
                r[3],
                r[4],
            ),
        )
    ]


def _compare_security_group(resource: StateResource, reader: AwsReader) -> list[Drift]:
    live = reader.get_security_group(_resource_id(resource, "id"))
    if live is None:
        return _missing(resource)
    drifts: list[Drift] = []
    values = _values(resource)
    for direction, live_rules in (("ingress", live.ingress), ("egress", live.egress)):
        state_set = _atomic_rules(values.get(direction))
        live_set = _atomic_rules(live_rules)
        if state_set != live_set:
            drifts.append(
                _drift(resource, direction, _render_rules(state_set), _render_rules(live_set))
            )
    return drifts


# --- aws_s3_bucket: tags, versioning status ---------------------------------


def _state_versioning_enabled(values: dict[str, Any]) -> bool:
    versioning = values.get("versioning")
    if isinstance(versioning, list) and versioning and isinstance(versioning[0], dict):
        return bool(versioning[0].get("enabled"))
    if isinstance(versioning, dict):
        return bool(versioning.get("enabled"))
    return False


def _compare_bucket(resource: StateResource, reader: AwsReader) -> list[Drift]:
    live = reader.get_bucket(_resource_id(resource, "bucket"))
    if live is None:
        return _missing(resource)
    drifts: list[Drift] = []
    state_tags = _state_tags(resource)
    if state_tags != live.tags:
        drifts.append(_drift(resource, "tags", state_tags, live.tags))
    state_versioning = _state_versioning_enabled(_values(resource))
    if state_versioning != live.versioning_enabled:
        drifts.append(_drift(resource, "versioning", state_versioning, live.versioning_enabled))
    return drifts


#: Registry keyed by resource type — adding a type later is one entry here
#: plus one adapter method (spec Modularity notes).
COMPARATORS: dict[str, Comparator] = {
    "aws_instance": _compare_instance,
    "aws_security_group": _compare_security_group,
    "aws_s3_bucket": _compare_bucket,
}
