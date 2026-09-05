from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field, field_validator, model_validator

from forge_core.change_planning import (
    ChangeImpactPreview,
    ChangeScenario,
    PlanVerification,
)
from forge_core.external_evidence_planning import (
    ExternalEvidencePlanVerification,
    RequiredExternalEvidencePlan,
)
from forge_core.models import ContractModel


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


class StoredChangeImpactPreview(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    preview_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    scenario_id: str = Field(min_length=1)
    scenario_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    baseline_snapshot_id: str = Field(min_length=1)
    baseline_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    scenario: ChangeScenario
    preview: ChangeImpactPreview
    stored_at: datetime

    @field_validator("stored_at")
    @classmethod
    def stored_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "stored_at")

    @model_validator(mode="after")
    def preview_must_bind_scenario_and_baseline(self) -> StoredChangeImpactPreview:
        if (
            self.project_id != self.scenario.project_id
            or self.project_id != self.preview.project_id
            or self.scenario_id != self.scenario.scenario_id
            or self.scenario_id != self.preview.scenario_id
            or self.scenario_hash != self.scenario.scenario_hash
            or self.scenario_hash != self.preview.scenario_hash
            or self.preview_hash != self.preview.preview_hash
            or self.baseline_snapshot_id != self.scenario.baseline_snapshot_id
            or self.baseline_snapshot_hash != self.scenario.baseline_snapshot_hash
        ):
            raise ValueError("stored preview identity does not match payload")
        if self.scenario.created_at > self.preview.generated_at:
            raise ValueError("preview cannot be generated before its scenario")
        if self.preview.generated_at > self.stored_at:
            raise ValueError("preview cannot be stored before generation")
        return self


class StoredPlanVerification(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    verification_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    scenario_id: str = Field(min_length=1)
    preview_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    actual_change_analysis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    verification: PlanVerification
    stored_at: datetime

    @field_validator("stored_at")
    @classmethod
    def stored_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "stored_at")

    @model_validator(mode="after")
    def verification_must_bind_preview_and_actual(self) -> StoredPlanVerification:
        if (
            self.project_id != self.verification.project_id
            or self.verification_hash != self.verification.verification_hash
            or self.scenario_id != self.verification.scenario_id
            or self.preview_hash != self.verification.preview_hash
            or self.actual_change_analysis_hash
            != self.verification.actual_change_analysis_hash
        ):
            raise ValueError("stored plan verification identity does not match payload")
        if self.verification.verified_at > self.stored_at:
            raise ValueError("plan verification cannot be stored before verification")
        return self


class StoredExternalEvidencePlan(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    scenario_id: str = Field(min_length=1)
    scenario_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    baseline_snapshot_id: str = Field(min_length=1)
    baseline_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    plan: RequiredExternalEvidencePlan
    stored_at: datetime

    @field_validator("stored_at")
    @classmethod
    def stored_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "stored_at")

    @model_validator(mode="after")
    def plan_must_bind_scenario_and_baseline(self) -> StoredExternalEvidencePlan:
        scenario = self.plan.operating_scenario
        if (
            self.project_id != self.plan.project_id
            or self.project_id != scenario.project_id
            or self.plan_hash != self.plan.plan_hash
            or self.scenario_id != scenario.scenario_id
            or self.scenario_hash != scenario.scenario_hash
            or self.baseline_snapshot_id != scenario.baseline_snapshot_id
            or self.baseline_snapshot_hash != scenario.baseline_snapshot_hash
        ):
            raise ValueError(
                "stored external evidence plan identity does not match payload"
            )
        if self.plan.generated_at > self.stored_at:
            raise ValueError(
                "external evidence plan cannot be stored before generation"
            )
        return self


class StoredExternalEvidencePlanVerification(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    verification_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    actual_change_analysis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    verification: ExternalEvidencePlanVerification
    stored_at: datetime

    @field_validator("stored_at")
    @classmethod
    def stored_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "stored_at")

    @model_validator(mode="after")
    def verification_must_bind_plan_and_analysis(
        self,
    ) -> StoredExternalEvidencePlanVerification:
        if (
            self.project_id != self.verification.project_id
            or self.verification_hash != self.verification.verification_hash
            or self.plan_hash != self.verification.stored_plan_hash
            or self.actual_change_analysis_hash
            != self.verification.actual_change_analysis_hash
            or any(
                item.evidence.change_analysis_hash != self.actual_change_analysis_hash
                for item in self.verification.imported_evidence
            )
        ):
            raise ValueError(
                "stored external evidence verification identity does not match payload"
            )
        if self.verification.verified_at > self.stored_at:
            raise ValueError(
                "external evidence verification cannot be stored before verification"
            )
        return self


__all__ = [
    "StoredChangeImpactPreview",
    "StoredExternalEvidencePlan",
    "StoredExternalEvidencePlanVerification",
    "StoredPlanVerification",
]
