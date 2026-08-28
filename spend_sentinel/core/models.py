"""Pydantic models for the subset of ``terraform show -json`` plan output we consume.

Only the fields spend-sentinel reads are modeled (R1): for every entry in
``resource_changes`` — address, type, provider, change actions, and the
``before``/``after`` attribute maps. Unknown sibling keys in the plan JSON are
ignored; the modeled fields themselves are validated strictly and fail closed.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ActionClass(StrEnum):
    """Classification of a resource change per R3."""

    CREATE = "create"
    DELETE = "delete"
    UPDATE = "update"
    REPLACE = "replace"


class Change(BaseModel):
    """The ``change`` block of a resource change entry."""

    model_config = ConfigDict(extra="ignore", strict=False)

    actions: list[str]
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


class ResourceChange(BaseModel):
    """One entry of the plan's ``resource_changes`` array."""

    model_config = ConfigDict(extra="ignore", strict=False)

    address: str
    type: str
    provider_name: str = ""
    change: Change


class ProviderConfig(BaseModel):
    """One entry of the plan's ``configuration.provider_config`` map (R8)."""

    model_config = ConfigDict(extra="ignore", strict=False)

    name: str | None = None
    expressions: dict[str, Any] | None = None


class Configuration(BaseModel):
    """The plan's ``configuration`` block — only ``provider_config`` is consumed (R8)."""

    model_config = ConfigDict(extra="ignore", strict=False)

    provider_config: dict[str, ProviderConfig] | None = None


class Plan(BaseModel):
    """A parsed Terraform plan (the subset consumed by spend-sentinel)."""

    model_config = ConfigDict(extra="ignore", strict=False)

    format_version: str
    resource_changes: list[ResourceChange]
    configuration: Configuration | None = None


class ClassifiedChange(BaseModel):
    """A non-no-op resource change with its R3 action classification."""

    model_config = ConfigDict(frozen=True)

    address: str
    type: str
    provider: str
    action: ActionClass


class PlanSummary(BaseModel):
    """Counts of classified resource changes (R3)."""

    model_config = ConfigDict(frozen=True)

    created: int = Field(ge=0)
    deleted: int = Field(ge=0)
    updated: int = Field(ge=0)
    replaced: int = Field(ge=0)

    @property
    def changed(self) -> int:
        """Total number of changed (non-no-op) resources."""
        return self.created + self.deleted + self.updated + self.replaced


class UnpricedReason(StrEnum):
    """Why a resource change could not be priced (R7 taxonomy)."""

    UNSUPPORTED_TYPE = "unsupported_type"
    UNKNOWN_PRICE_KEY = "unknown_price_key"
    ATTRIBUTES_UNKNOWN = "attributes_unknown"


class CostLine(BaseModel):
    """Per-resource monthly cost delta, rounded half-up to cents (R5, R6)."""

    model_config = ConfigDict(frozen=True)

    address: str
    type: str
    action: ActionClass
    monthly_delta_usd: Decimal


class UnpricedResource(BaseModel):
    """A resource change that appears in ``cost.unpriced`` instead of the breakdown (R7)."""

    model_config = ConfigDict(frozen=True)

    address: str
    type: str
    reason: UnpricedReason


class CostReport(BaseModel):
    """The estimator's result: total delta, per-resource breakdown, unpriced list (R6, R7)."""

    model_config = ConfigDict(frozen=True)

    monthly_delta_usd: Decimal
    breakdown: tuple[CostLine, ...]
    unpriced: tuple[UnpricedResource, ...]


# --- State ingestion (R9): subset of `terraform show -json` on the state ---


class StateResource(BaseModel):
    """One resource instance in the state's ``values`` tree."""

    model_config = ConfigDict(extra="ignore", strict=False)

    address: str
    mode: str = "managed"
    type: str
    values: dict[str, Any] | None = None
    sensitive_values: Any = None


class StateModule(BaseModel):
    """A module node: its resources plus child modules (recursive)."""

    model_config = ConfigDict(extra="ignore", strict=False)

    resources: list[StateResource] = Field(default_factory=list)
    child_modules: list[StateModule] = Field(default_factory=list)


class StateValues(BaseModel):
    """The state's ``values`` block — only ``root_module`` is consumed."""

    model_config = ConfigDict(extra="ignore", strict=False)

    root_module: StateModule | None = None


class State(BaseModel):
    """A parsed Terraform state (the subset consumed for drift detection)."""

    model_config = ConfigDict(extra="ignore", strict=False)

    format_version: str
    values: StateValues | None = None


# --- Live AWS attribute models (returned by AwsReader implementations, R9) ---


class InstanceAttrs(BaseModel):
    """Live EC2 instance attributes in the R9 allowlist."""

    model_config = ConfigDict(frozen=True)

    instance_type: str
    tags: dict[str, str] = Field(default_factory=dict)


class SecurityGroupRule(BaseModel):
    """One security-group rule, normalized for order-insensitive comparison."""

    model_config = ConfigDict(frozen=True)

    protocol: str
    from_port: int | None = None
    to_port: int | None = None
    cidr_blocks: tuple[str, ...] = ()
    ipv6_cidr_blocks: tuple[str, ...] = ()


class SecurityGroupAttrs(BaseModel):
    """Live security-group rule sets in the R9 allowlist."""

    model_config = ConfigDict(frozen=True)

    ingress: tuple[SecurityGroupRule, ...] = ()
    egress: tuple[SecurityGroupRule, ...] = ()


class BucketAttrs(BaseModel):
    """Live S3 bucket attributes in the R9 allowlist."""

    model_config = ConfigDict(frozen=True)

    tags: dict[str, str] = Field(default_factory=dict)
    versioning_enabled: bool = False


# --- Drift results (R10-R12) ---


class DriftKind(StrEnum):
    """How a resource drifted (R10)."""

    CHANGED = "changed"
    MISSING = "missing"


class DriftStatus(StrEnum):
    """Whether drift detection ran (R11)."""

    RAN = "ran"
    SKIPPED = "skipped"


class Drift(BaseModel):
    """One detected drift: address, attribute path, state vs live value (R10)."""

    model_config = ConfigDict(frozen=True)

    address: str
    kind: DriftKind
    attribute: str | None = None
    state_value: Any = None
    live_value: Any = None


class DriftSkipped(BaseModel):
    """A state resource drift detection did not evaluate (R10)."""

    model_config = ConfigDict(frozen=True)

    address: str
    type: str
    reason: str


class DriftError(BaseModel):
    """A per-resource AWS read failure that did not kill the run (R12)."""

    model_config = ConfigDict(frozen=True)

    address: str
    error: str


class DriftReport(BaseModel):
    """The drift detector's result (R9-R12)."""

    model_config = ConfigDict(frozen=True)

    status: DriftStatus
    drifts: tuple[Drift, ...] = ()
    skipped: tuple[DriftSkipped, ...] = ()
    errors: tuple[DriftError, ...] = ()


# --- Policy rule results (R17) ---


class RuleOutcome(StrEnum):
    """Result of one policy rule evaluation (R17)."""

    PASS = "pass"
    WARN = "warn"
    BLOCK = "block"
    SKIPPED = "skipped"


class RuleResult(BaseModel):
    """One rule evaluation: name, result, human-readable message (R17)."""

    model_config = ConfigDict(frozen=True)

    name: str
    result: RuleOutcome
    message: str
