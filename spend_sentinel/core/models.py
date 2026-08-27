"""Pydantic models for the subset of ``terraform show -json`` plan output we consume.

Only the fields spend-sentinel reads are modeled (R1): for every entry in
``resource_changes`` — address, type, provider, change actions, and the
``before``/``after`` attribute maps. Unknown sibling keys in the plan JSON are
ignored; the modeled fields themselves are validated strictly and fail closed.
"""

from __future__ import annotations

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


class Plan(BaseModel):
    """A parsed Terraform plan (the subset consumed by spend-sentinel)."""

    model_config = ConfigDict(extra="ignore", strict=False)

    format_version: str
    resource_changes: list[ResourceChange]


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
