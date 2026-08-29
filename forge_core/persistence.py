from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from forge_core.constraints import (
    BOMLine,
    CostEvaluation,
    QuoteSnapshot,
    evaluate_cost,
)
from forge_core.design import SystemDesignRevision
from forge_core.hashing import canonical_sha256
from forge_core.models import (
    AnalysisRunRecord,
    ContractModel,
    EngineeringSpec,
    PreparedAnalysis,
    RequirementPriority,
    RunLifecycleStatus,
    RunStateEvent,
    RunStatus,
    SpecStatus,
    Verdict,
    VerificationBundle,
)


def _require_utc(value: datetime, field_name: str) -> datetime:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if offset.total_seconds() != 0:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


class PersistenceError(RuntimeError):
    """Stable base error for repository failures."""

    code = "storage_error"


class StorageBusyError(PersistenceError):
    code = "storage_busy"


class RecordNotFoundError(PersistenceError):
    code = "not_found"


class VersionConflictError(PersistenceError):
    code = "conflict"


class IntegrityConflictError(PersistenceError):
    code = "immutable_record"


class IdempotencyConflictError(PersistenceError):
    code = "idempotency_conflict"


class MigrationError(PersistenceError):
    code = "migration_error"


class CorruptRecordError(PersistenceError):
    code = "integrity_error"


class ProjectRecord(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime

    @field_validator("project_id")
    @classmethod
    def project_id_must_be_export_safe(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is None:
            raise ValueError("project_id must be an export-safe identifier")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "project timestamp")

    @model_validator(mode="after")
    def update_cannot_precede_creation(self) -> ProjectRecord:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class StoredSpec(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    spec_id: str = Field(min_length=1)
    spec_version: int = Field(ge=1)
    spec_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    spec: EngineeringSpec
    stored_at: datetime

    @field_validator("stored_at")
    @classmethod
    def stored_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "stored_at")

    @model_validator(mode="after")
    def identity_and_hash_must_match(self) -> StoredSpec:
        if (
            self.spec.project_id != self.project_id
            or self.spec.spec_id != self.spec_id
            or self.spec.spec_version != self.spec_version
        ):
            raise ValueError("stored spec identity must match its payload")
        if canonical_sha256(self.spec) != self.spec_hash:
            raise ValueError("spec_hash must match the stored spec")
        if self.spec.approved_at is not None:
            _require_utc(self.spec.approved_at, "spec approved_at")
        return self


class StoredRevision(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    revision_number: int = Field(ge=1)
    revision_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    revision: SystemDesignRevision
    stored_at: datetime

    @field_validator("stored_at")
    @classmethod
    def stored_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "stored_at")

    @model_validator(mode="after")
    def identity_and_hash_must_match(self) -> StoredRevision:
        if (
            self.revision.project_id != self.project_id
            or self.revision.revision_id != self.revision_id
            or self.revision.revision_number != self.revision_number
        ):
            raise ValueError("stored revision identity must match its payload")
        if canonical_sha256(self.revision) != self.revision_hash:
            raise ValueError("revision_hash must match the stored revision")
        _require_utc(self.revision.approved_at, "revision approved_at")
        return self


class StoredPreparation(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    preparation: PreparedAnalysis
    stored_at: datetime

    @field_validator("stored_at")
    @classmethod
    def stored_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "stored_at")

    @model_validator(mode="after")
    def project_must_match_binding(self) -> StoredPreparation:
        if self.preparation.binding.project_id != self.project_id:
            raise ValueError("stored preparation must belong to its project")
        return self


class SpecStateEvent(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    event_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    spec_id: str = Field(min_length=1)
    spec_version: int = Field(ge=1)
    sequence: int = Field(ge=1, le=3)
    previous_status: SpecStatus | None
    status: SpecStatus
    actor: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def event_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "occurred_at")

    @model_validator(mode="after")
    def validate_transition(self) -> SpecStateEvent:
        expected = {
            1: (None, SpecStatus.DRAFT),
            2: (SpecStatus.DRAFT, SpecStatus.APPROVED),
            3: (SpecStatus.APPROVED, SpecStatus.SUPERSEDED),
        }
        if (self.previous_status, self.status) != expected[self.sequence]:
            raise ValueError("invalid immutable spec state transition")
        return self


class StoredCostEvaluation(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    bom_artifact_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    dependency_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    bom: tuple[BOMLine, ...]
    quotes: tuple[QuoteSnapshot, ...]
    evaluated_at: datetime
    evaluation: CostEvaluation

    @field_validator("evaluated_at")
    @classmethod
    def evaluated_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "evaluated_at")

    @model_validator(mode="after")
    def provenance_must_reproduce_evaluation(self) -> StoredCostEvaluation:
        for quote in self.quotes:
            _require_utc(quote.observed_at, "quote observed_at")
            _require_utc(quote.expires_at, "quote expires_at")
        reproduced = evaluate_cost(
            self.bom,
            self.quotes,
            currency=self.evaluation.currency,
            budget_limit=self.evaluation.budget_limit,
            reserve_rate=self.evaluation.reserve_rate,
            evaluated_at=self.evaluated_at,
        )
        if reproduced != self.evaluation:
            raise ValueError("stored quote provenance must reproduce cost evaluation")
        return self


class StoredRun(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    preparation_id: str = Field(min_length=1)
    events: tuple[RunStateEvent, ...] = Field(min_length=1)
    result: AnalysisRunRecord | None = None
    verification: VerificationBundle | None = None

    @model_validator(mode="after")
    def validate_run_aggregate(self) -> StoredRun:
        event_ids: set[str] = set()
        for index, event in enumerate(self.events, start=1):
            if (
                event.project_id != self.project_id
                or event.run_id != self.run_id
                or event.preparation_id != self.preparation_id
                or event.sequence != index
            ):
                raise ValueError("run events must form one contiguous run sequence")
            if event.event_id in event_ids:
                raise ValueError("run event IDs must be unique")
            event_ids.add(event.event_id)
            if event.prepare_hash != self.events[0].prepare_hash:
                raise ValueError("run events must keep one preparation hash")
            if index > 1 and event.previous_status is not self.events[index - 2].status:
                raise ValueError("run event status chain must be contiguous")
            if index > 1 and event.occurred_at < self.events[index - 2].occurred_at:
                raise ValueError("run event timestamps must be monotonic")
        if self.result is None:
            if self.verification is not None:
                raise ValueError("verification requires a stored run result")
            if self.events[-1].status in {
                RunLifecycleStatus.SUCCEEDED,
                RunLifecycleStatus.FAILED,
            }:
                raise ValueError("completed lifecycle events require a stored result")
            return self
        if self.verification is None:
            raise ValueError("stored run results require verification evidence")
        if self.result.run_id != self.run_id:
            raise ValueError("stored result run_id must match its aggregate")
        latest = self.events[-1]
        if latest.status not in {
            RunLifecycleStatus.SUCCEEDED,
            RunLifecycleStatus.FAILED,
        }:
            raise ValueError("a stored result requires a completed lifecycle event")
        if latest.run_record_hash != canonical_sha256(self.result):
            raise ValueError("completed event hash must match the stored result")
        expected_status = (
            RunLifecycleStatus.SUCCEEDED
            if self.result.status is RunStatus.SUCCEEDED
            else RunLifecycleStatus.FAILED
        )
        if latest.status is not expected_status:
            raise ValueError("run result status must match its lifecycle event")
        if self.result.status is RunStatus.SUCCEEDED:
            if self.result.manifest is None:
                raise ValueError("succeeded runs require stored verification evidence")
            if self.result.analysis_result is None:
                raise ValueError("succeeded runs require an analysis result")
            if (
                canonical_sha256(self.result.analysis_result)
                != self.result.manifest.output_hash
            ):
                raise ValueError("run manifest output hash must match the result")
            if (
                canonical_sha256(self.verification)
                != self.result.manifest.verification_hash
            ):
                raise ValueError("run manifest verification hash must match evidence")
        elif self.verification.overall_verdict is not Verdict.INDETERMINATE:
            raise ValueError("failed runs cannot store a final PASS or FAIL verdict")
        required = tuple(
            item
            for item in self.verification.results
            if item.priority is RequirementPriority.REQUIRED
        )
        if any(item.status is Verdict.FAIL for item in required):
            expected_verdict = Verdict.FAIL
        elif not required or any(
            item.status is Verdict.INDETERMINATE for item in required
        ):
            expected_verdict = Verdict.INDETERMINATE
        else:
            expected_verdict = Verdict.PASS
        if self.verification.overall_verdict is not expected_verdict:
            raise ValueError("verification bundle verdict is inconsistent")
        return self


class IdempotencyRecord(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    operation: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    local_installation_id: str = Field(min_length=1)
    key: str = Field(min_length=1)
    request_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    response_json: str = Field(min_length=1)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "created_at")

    @field_validator("response_json")
    @classmethod
    def response_must_be_json(cls, value: str) -> str:
        try:
            json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("response_json must contain valid JSON") from exc
        return value


class InterruptedRun(ContractModel):
    project_id: str = Field(min_length=1)
    project_version: int = Field(ge=1)
    run_id: str = Field(min_length=1)
    preparation_id: str = Field(min_length=1)
    prepare_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    latest_sequence: int = Field(ge=1)
    latest_status: Literal[RunLifecycleStatus.RUNNING] = RunLifecycleStatus.RUNNING


@dataclass(frozen=True, slots=True)
class EvidenceExport:
    filename: str
    project_version: int
    content: bytes


@dataclass(frozen=True, slots=True)
class IdempotentWriteResult:
    response_json: str
    project_version: int | None
    replayed: bool


__all__ = [
    "CorruptRecordError",
    "EvidenceExport",
    "IdempotencyConflictError",
    "IdempotencyRecord",
    "IdempotentWriteResult",
    "IntegrityConflictError",
    "InterruptedRun",
    "MigrationError",
    "PersistenceError",
    "ProjectRecord",
    "RecordNotFoundError",
    "StorageBusyError",
    "SpecStateEvent",
    "StoredCostEvaluation",
    "StoredPreparation",
    "StoredRevision",
    "StoredRun",
    "StoredSpec",
    "VersionConflictError",
]
