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
