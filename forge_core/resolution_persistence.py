from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field, field_validator, model_validator

from forge_core.models import ContractModel
from forge_core.resolution_planning import (
    BlockerDiagnosis,
    DesignProposalSet,
    FixProposalSet,
    ProposalSelection,
)


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


class StoredDesignProposalSet(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    preview_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    proposal: DesignProposalSet
    stored_at: datetime

    @field_validator("stored_at")
    @classmethod
    def stored_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "stored_at")

    @model_validator(mode="after")
    def proposal_must_bind_preview(self) -> StoredDesignProposalSet:
        if (
            self.project_id != self.proposal.project_id
            or self.proposal_hash != self.proposal.proposal_hash
            or self.preview_hash != self.proposal.preview_hash
        ):
            raise ValueError("stored design proposal identity does not match payload")
        return self


class StoredReleaseDiagnosis(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    diagnosis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    decision_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    diagnosis: BlockerDiagnosis
    fix_proposal_sets: tuple[FixProposalSet, ...]
    stored_at: datetime

    @field_validator("stored_at")
    @classmethod
    def stored_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "stored_at")

    @model_validator(mode="after")
    def diagnosis_and_fixes_must_bind_decision(self) -> StoredReleaseDiagnosis:
        if (
            self.project_id != self.diagnosis.project_id
            or self.diagnosis_hash != self.diagnosis.diagnosis_hash
            or self.decision_hash != self.diagnosis.decision_hash
        ):
            raise ValueError("stored release diagnosis identity does not match payload")
        ordered = tuple(
            sorted(self.fix_proposal_sets, key=lambda item: item.diagnosis_item_hash)
        )
        if self.fix_proposal_sets != ordered:
            raise ValueError("stored fix proposal sets must be canonically ordered")
        expected_items = {item.item_hash for item in self.diagnosis.items}
        actual_items = {item.diagnosis_item_hash for item in self.fix_proposal_sets}
        if actual_items != expected_items or len(actual_items) != len(
            self.fix_proposal_sets
        ):
            raise ValueError("stored fixes must cover each diagnosis item exactly once")
        if any(
            item.project_id != self.project_id
            or item.diagnosis_hash != self.diagnosis_hash
            for item in self.fix_proposal_sets
        ):
            raise ValueError("stored fixes do not bind the release diagnosis")
        return self


class StoredResolutionPlan(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    diagnosis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    fix_proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    selection: ProposalSelection
    stored_at: datetime

    @field_validator("stored_at")
    @classmethod
    def stored_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "stored_at")

    @model_validator(mode="after")
    def plan_must_bind_exact_selection(self) -> StoredResolutionPlan:
        if (
            self.project_id != self.selection.project_id
            or self.plan_hash != self.selection.selection_hash
            or self.diagnosis_hash != self.selection.fix_set.diagnosis_hash
            or self.fix_proposal_hash != self.selection.selected_fix_hash
        ):
            raise ValueError("stored resolution plan identity does not match selection")
        if self.selection.selected_at > self.stored_at:
            raise ValueError("resolution plan cannot be stored before selection")
        return self


__all__ = [
    "StoredDesignProposalSet",
    "StoredReleaseDiagnosis",
    "StoredResolutionPlan",
]
