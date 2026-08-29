from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from forge_core.hashing import canonical_sha256


class ContractModel(BaseModel):
    """Base contract: reject unknown data and prevent top-level mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


class SpecStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    SUPERSEDED = "superseded"


class RequirementPriority(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INDETERMINATE = "INDETERMINATE"


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ApprovalSubjectKind(StrEnum):
    ENGINEERING_SPEC = "engineering_spec"
    SYSTEM_DESIGN_REVISION = "system_design_revision"


class RunLifecycleStatus(StrEnum):
    PREPARED = "prepared"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SourceRef(ContractModel):
    kind: Literal["user", "dataset", "formula", "plugin", "artifact"]
    identifier: str = Field(min_length=1)
    version: str | None = None
    hash: str | None = None

    @field_validator("hash")
    @classmethod
    def hash_must_be_canonical_sha256(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
            raise ValueError("hash must use canonical sha256:<64 lowercase hex> format")
        return value

    @model_validator(mode="after")
    def require_versioned_machine_source(self) -> SourceRef:
        if self.kind != "user" and (not self.version or not self.hash):
            raise ValueError("machine sources require version and hash")
        return self


class Uncertainty(ContractModel):
    absolute: float = Field(ge=0)
    unit: str = Field(min_length=1)

    @field_validator("absolute")
    @classmethod
    def uncertainty_must_be_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("uncertainty must be finite")
        return value


class Quantity(ContractModel):
    value: float
    unit: str = Field(min_length=1)
    dimension: str = Field(min_length=1)
    source: SourceRef
    uncertainty: Uncertainty | None = None

    @field_validator("value")
    @classmethod
    def value_must_be_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("quantity value must be finite")
        return value


class Requirement(ContractModel):
    id: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    operator: Literal["<", "<=", "==", ">=", ">"]
    target: Quantity
    priority: RequirementPriority
    tolerance: Uncertainty | None = None

    @model_validator(mode="after")
    def tolerance_unit_must_match_target(self) -> Requirement:
        if self.tolerance is not None and self.tolerance.unit != self.target.unit:
            raise ValueError("requirement tolerance unit must match target unit")
        return self


class PluginRef(ContractModel):
    plugin_id: str = Field(min_length=1)
    plugin_version: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    artifact_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ApprovalRef(ContractModel):
    """Immutable reference to the exact subject a local user approved."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    approval_id: str = Field(min_length=1)
    subject_kind: ApprovalSubjectKind
    project_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    subject_version: int = Field(ge=1)
    subject_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    approved_by: str = Field(min_length=1)
    approved_at: datetime

    @field_validator("approved_at")
    @classmethod
    def approval_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "approved_at")


class MaterialSnapshot(ContractModel):
    material_id: str = Field(min_length=1)
    dataset_version: str = Field(min_length=1)
    elastic_modulus: Quantity | None = None
    yield_strength: Quantity | None = None


class EngineeringSpec(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    spec_id: str = Field(min_length=1)
    spec_version: int = Field(ge=1)
    status: SpecStatus
    intent: str = Field(min_length=1)
    trust_mode: Literal["explorer"] = "explorer"
    model: PluginRef | None = None
    parameters: Mapping[str, Quantity]
    material: MaterialSnapshot | None = None
    requirements: tuple[Requirement, ...]
    assumptions: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    approved_at: datetime | None = None

    @field_validator("parameters", mode="after")
    @classmethod
    def freeze_parameters(cls, value: Mapping[str, Quantity]) -> Mapping[str, Quantity]:
        return MappingProxyType(dict(value))

    @field_serializer("parameters")
    def serialize_parameters(
        self, value: Mapping[str, Quantity]
    ) -> dict[str, Quantity]:
        return dict(value)

    @model_validator(mode="after")
    def validate_approval_timestamp(self) -> EngineeringSpec:
        if self.status is SpecStatus.APPROVED and self.approved_at is None:
            raise ValueError("approved specs require approved_at")
        if self.status is SpecStatus.APPROVED and self.model is None:
            raise ValueError("approved specs require an immutable plugin reference")
        if self.status is not SpecStatus.APPROVED and self.approved_at is not None:
            raise ValueError("only approved specs may have approved_at")
        requirement_ids = [requirement.id for requirement in self.requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("requirement IDs must be unique")
        return self


class PreflightResult(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    spec_id: str
    spec_version: int
    accepted: bool
    reason_codes: tuple[str, ...] = ()
    missing_inputs: tuple[str, ...] = ()
    policy_violations: tuple[str, ...] = ()
    requirement_reasons: Mapping[str, tuple[str, ...]] = Field(default_factory=dict)

    @field_validator("requirement_reasons", mode="after")
    @classmethod
    def freeze_requirement_reasons(
        cls, value: Mapping[str, tuple[str, ...]]
    ) -> Mapping[str, tuple[str, ...]]:
        return MappingProxyType(dict(value))

    @field_serializer("requirement_reasons")
    def serialize_requirement_reasons(
        self, value: Mapping[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        return dict(value)

    @model_validator(mode="after")
    def accepted_preflight_has_no_rejection_evidence(self) -> PreflightResult:
        rejection_evidence = (
            self.reason_codes
            or self.missing_inputs
            or self.policy_violations
            or self.requirement_reasons
        )
        if self.accepted and rejection_evidence:
            raise ValueError("accepted preflight cannot contain rejection evidence")
        if not self.accepted and not rejection_evidence:
            raise ValueError("rejected preflight requires rejection evidence")
        return self


class PreparedAnalysisBinding(ContractModel):
    """Inputs and approvals frozen by preflight before a run may exist."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    spec_id: str = Field(min_length=1)
    spec_version: int = Field(ge=1)
    spec_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    revision_id: str | None = Field(default=None, min_length=1)
    revision_hash: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    revision_number: int | None = Field(default=None, ge=1)
    engine_version: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    plugin: PluginRef
    input_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    expected_requirement_ids: tuple[str, ...]
    expected_metrics: tuple[str, ...]
    approval_refs: tuple[ApprovalRef, ...] = ()
    preflight: PreflightResult

    @model_validator(mode="after")
    def validate_binding(self) -> PreparedAnalysisBinding:
        revision_values = (
            self.revision_id,
            self.revision_hash,
            self.revision_number,
        )
        if any(item is None for item in revision_values) and not all(
            item is None for item in revision_values
        ):
            raise ValueError(
                "revision_id, revision_hash and revision_number "
                "must be provided together"
            )
        if len(self.expected_requirement_ids) != len(
            set(self.expected_requirement_ids)
        ):
            raise ValueError("expected requirement IDs must be unique")
        if len(self.expected_metrics) != len(set(self.expected_metrics)):
            raise ValueError("expected metrics must be unique")
        if any(not item for item in self.expected_requirement_ids):
            raise ValueError("expected requirement IDs cannot be empty")
        if any(not item for item in self.expected_metrics):
            raise ValueError("expected metrics cannot be empty")
        approval_ids = [item.approval_id for item in self.approval_refs]
        if len(approval_ids) != len(set(approval_ids)):
            raise ValueError("approval IDs must be unique")
        approval_subjects = [
            (item.subject_kind, item.subject_id, item.subject_version)
            for item in self.approval_refs
        ]
        if len(approval_subjects) != len(set(approval_subjects)):
            raise ValueError("approval subjects must be unique")
        if any(item.project_id != self.project_id for item in self.approval_refs):
            raise ValueError("approvals must belong to the prepared project")
        if (
            self.preflight.spec_id != self.spec_id
            or self.preflight.spec_version != self.spec_version
        ):
            raise ValueError("preflight identity must match the prepared spec")
        spec_approval = next(
            (
                item
                for item in self.approval_refs
                if item.subject_kind is ApprovalSubjectKind.ENGINEERING_SPEC
            ),
            None,
        )
        if self.preflight.accepted and (
            spec_approval is None
            or spec_approval.subject_id != self.spec_id
            or spec_approval.subject_version != self.spec_version
            or spec_approval.subject_hash != self.spec_hash
        ):
            raise ValueError("accepted preflight requires the exact spec approval")
        revision_approval = next(
            (
                item
                for item in self.approval_refs
                if item.subject_kind is ApprovalSubjectKind.SYSTEM_DESIGN_REVISION
            ),
            None,
        )
        if (
            self.preflight.accepted
            and self.revision_id is not None
            and (
                revision_approval is None
                or revision_approval.subject_id != self.revision_id
                or revision_approval.subject_version != self.revision_number
                or revision_approval.subject_hash != self.revision_hash
            )
        ):
            raise ValueError(
                "accepted revision-bound preflight requires the exact revision approval"
            )
        return self


class PreparedAnalysis(ContractModel):
    """Tamper-evident preflight artifact persisted before execution."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    preparation_id: str = Field(min_length=1)
    binding: PreparedAnalysisBinding
    prepare_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def prepare_hash_must_match_binding(self) -> PreparedAnalysis:
        if self.prepare_hash != canonical_sha256(self.binding):
            raise ValueError("prepare_hash must match the prepared binding")
        return self


class RunStateEvent(ContractModel):
    """One append-only transition in the analysis execution lifecycle."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    event_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    preparation_id: str = Field(min_length=1)
    prepare_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sequence: int = Field(ge=1)
    previous_status: RunLifecycleStatus | None
    status: RunLifecycleStatus
    actor: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)
    occurred_at: datetime
    run_record_hash: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )

    @field_validator("occurred_at")
    @classmethod
    def event_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "occurred_at")

    @model_validator(mode="after")
    def validate_transition(self) -> RunStateEvent:
        if self.sequence == 1:
            if (
                self.previous_status is not None
                or self.status is not RunLifecycleStatus.PREPARED
            ):
                raise ValueError("the first run event must enter prepared")
            if self.run_record_hash is not None:
                raise ValueError("prepared event cannot reference a run record")
            return self
        if self.previous_status is None:
            raise ValueError("later run events require previous_status")
        allowed = {
            RunLifecycleStatus.PREPARED: {RunLifecycleStatus.QUEUED},
            RunLifecycleStatus.QUEUED: {
                RunLifecycleStatus.RUNNING,
                RunLifecycleStatus.CANCELLED,
            },
            RunLifecycleStatus.RUNNING: {
                RunLifecycleStatus.SUCCEEDED,
                RunLifecycleStatus.FAILED,
                RunLifecycleStatus.CANCELLED,
            },
        }
        if self.status not in allowed.get(self.previous_status, set()):
            raise ValueError("invalid run lifecycle transition")
        if self.status in {
            RunLifecycleStatus.SUCCEEDED,
            RunLifecycleStatus.FAILED,
        }:
            if self.run_record_hash is None:
                raise ValueError("completed run events require run_record_hash")
        elif self.run_record_hash is not None:
            raise ValueError("non-result run events cannot reference a run record")
        return self


class AnalysisResult(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    outputs: Mapping[str, Quantity]
    intermediates: Mapping[str, float] = Field(default_factory=dict)
    formula_id: str
    formula_version: str
    warnings: tuple[str, ...] = ()

    @field_validator("outputs", mode="after")
    @classmethod
    def freeze_outputs(cls, value: Mapping[str, Quantity]) -> Mapping[str, Quantity]:
        return MappingProxyType(dict(value))

    @field_validator("intermediates", mode="after")
    @classmethod
    def freeze_intermediates(cls, value: Mapping[str, float]) -> Mapping[str, float]:
        if any(not math.isfinite(item) for item in value.values()):
            raise ValueError("analysis intermediates must be finite")
        return MappingProxyType(dict(value))

    @field_serializer("outputs")
    def serialize_outputs(self, value: Mapping[str, Quantity]) -> dict[str, Quantity]:
        return dict(value)

    @field_serializer("intermediates")
    def serialize_intermediates(self, value: Mapping[str, float]) -> dict[str, float]:
        return dict(value)


class VerificationResult(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    requirement_id: str
    priority: RequirementPriority
    status: Verdict
    observed: Quantity | None = None
    target: Quantity | None = None
    applied_tolerance: Uncertainty | None = None
    margin: float | None = None
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    reason_code: str | None = None

    @field_validator("margin")
    @classmethod
    def margin_must_be_finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("verification margin must be finite")
        return value

    @model_validator(mode="after")
    def validate_evidence_shape(self) -> VerificationResult:
        if self.status in (Verdict.PASS, Verdict.FAIL):
            if self.observed is None or self.target is None or self.margin is None:
                raise ValueError(
                    "PASS and FAIL require observed, target, margin and evidence"
                )
        elif not self.reason_code:
            raise ValueError("INDETERMINATE requires a reason_code")
        return self


class VerificationBundle(ContractModel):
    results: tuple[VerificationResult, ...]
    overall_verdict: Verdict


class RunManifest(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    spec_hash: str
    plugin_id: str
    plugin_version: str
    plugin_schema_version: str
    plugin_artifact_hash: str
    engine_version: str
    policy_version: str
    input_hash: str
    output_hash: str
    verification_hash: str


class AnalysisRunRecord(ContractModel):
    run_id: str
    status: RunStatus
    analysis_result: AnalysisResult | None = None
    manifest: RunManifest | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def validate_run_shape(self) -> AnalysisRunRecord:
        if self.status is RunStatus.SUCCEEDED:
            if self.analysis_result is None or self.manifest is None or self.error_code:
                raise ValueError("succeeded run requires result and manifest only")
        elif not self.error_code or self.manifest is not None:
            raise ValueError("failed run requires error_code and no manifest")
        return self


class ExecutionOutcome(ContractModel):
    preflight: PreflightResult
    run: AnalysisRunRecord | None
    verifications: tuple[VerificationResult, ...]
    overall_verdict: Verdict

    @model_validator(mode="after")
    def validate_outcome_consistency(self) -> ExecutionOutcome:
        required = tuple(
            item
            for item in self.verifications
            if item.priority is RequirementPriority.REQUIRED
        )
        if any(item.status is Verdict.FAIL for item in required):
            expected = Verdict.FAIL
        elif not required or any(
            item.status is Verdict.INDETERMINATE for item in required
        ):
            expected = Verdict.INDETERMINATE
        else:
            expected = Verdict.PASS

        if self.overall_verdict is not expected:
            raise ValueError("overall verdict does not match verification results")
        if not self.preflight.accepted:
            if (
                self.run is not None
                or self.overall_verdict is not Verdict.INDETERMINATE
            ):
                raise ValueError(
                    "rejected preflight cannot create a run or final verdict"
                )
        elif self.run is None:
            raise ValueError("accepted preflight requires an analysis run")
        elif (
            self.run.status is RunStatus.FAILED
            and self.overall_verdict is not Verdict.INDETERMINATE
        ):
            raise ValueError("failed run must be INDETERMINATE")
        return self
