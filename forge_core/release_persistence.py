from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field, field_validator, model_validator

from forge_core.change_management import (
    ConnectorSnapshot,
    ReleaseEvidenceKind,
)
from forge_core.connectors import ConnectorCaptureBundle
from forge_core.hashing import canonical_sha256
from forge_core.impact_engine import (
    ChangeImpactAssessment,
    NormalizedInterfaceProjection,
    connector_snapshot_hash,
)
from forge_core.models import ContractModel
from forge_core.persistence import StoredCostEvaluation
from forge_core.release_readiness import (
    FirmwareBuildEvidence,
    ReleaseReadinessDecision,
    ReleaseReadinessPolicy,
    TestExecutionEvidence,
)


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


class StoredReleasePolicy(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    policy_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    policy: ReleaseReadinessPolicy
    stored_at: datetime

    @field_validator("stored_at")
    @classmethod
    def stored_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "stored_at")

    @model_validator(mode="after")
    def policy_hash_must_reproduce(self) -> StoredReleasePolicy:
        if canonical_sha256(self.policy) != self.policy_hash:
            raise ValueError("release policy hash does not reproduce policy")
        return self


class StoredConnectorSnapshot(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    snapshot: ConnectorSnapshot
    capture_bundles: tuple[ConnectorCaptureBundle, ...] = Field(min_length=1)
    interface_projections: tuple[NormalizedInterfaceProjection, ...] = ()
    stored_at: datetime

    @field_validator("stored_at")
    @classmethod
    def stored_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "stored_at")

    @model_validator(mode="after")
    def snapshot_and_projections_must_be_source_bound(
        self,
    ) -> StoredConnectorSnapshot:
        if (
            self.snapshot.project_id != self.project_id
            or self.snapshot.snapshot_id != self.snapshot_id
        ):
            raise ValueError(
                "stored connector snapshot identity does not match payload"
            )
        if connector_snapshot_hash(self.snapshot) != self.snapshot_hash:
            raise ValueError("snapshot_hash does not reproduce connector snapshot")
        if self.snapshot.captured_at > self.stored_at:
            raise ValueError("connector snapshot cannot be stored before capture")
        if self.capture_bundles != tuple(
            sorted(
                self.capture_bundles,
                key=lambda item: item.manifest.adapter_id,
            )
        ):
            raise ValueError("connector capture provenance must be canonically ordered")
        adapter_ids = [item.manifest.adapter_id for item in self.capture_bundles]
        if len(adapter_ids) != len(set(adapter_ids)):
            raise ValueError("connector snapshot cannot contain duplicate captures")
        if any(
            bundle.request.project_id != self.project_id
            or bundle.request.snapshot_id != self.snapshot_id
            or bundle.request.hardware_revision_id != self.snapshot.hardware_revision_id
            or bundle.request.captured_at != self.snapshot.captured_at
            for bundle in self.capture_bundles
        ):
            raise ValueError("connector capture provenance targets another snapshot")
        captured_artifacts = tuple(
            sorted(
                (
                    artifact
                    for bundle in self.capture_bundles
                    for artifact in bundle.snapshot.artifacts
                ),
                key=lambda item: (item.source_system.value, item.artifact_id),
            )
        )
        if captured_artifacts != self.snapshot.artifacts:
            raise ValueError("connector captures do not reproduce aggregate artifacts")
        captured_projections = tuple(
            sorted(
                (
                    projection
                    for bundle in self.capture_bundles
                    for projection in bundle.projections
                ),
                key=lambda item: item.projection_hash,
            )
        )
        if captured_projections != self.interface_projections:
            raise ValueError(
                "connector captures do not reproduce interface projections"
            )
        domains = [item.source_ref.domain for item in self.interface_projections]
        if len(domains) != len(set(domains)):
            raise ValueError("stored interface projections must be unique by domain")
        if self.interface_projections != tuple(
            sorted(self.interface_projections, key=lambda item: item.projection_hash)
        ):
            raise ValueError("stored interface projections must be canonically ordered")
        if any(
            item.snapshot_id != self.snapshot_id
            or item.source_ref not in self.snapshot.artifacts
            for item in self.interface_projections
        ):
            raise ValueError("stored interface projection is not bound to its snapshot")
        return self


class StoredChangeImpactAssessment(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    analysis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    from_snapshot_id: str = Field(min_length=1)
    to_snapshot_id: str = Field(min_length=1)
    assessment: ChangeImpactAssessment
    stored_at: datetime

    @field_validator("stored_at")
    @classmethod
    def stored_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "stored_at")

    @model_validator(mode="after")
    def assessment_identity_must_match(self) -> StoredChangeImpactAssessment:
        if (
            self.assessment.project_id != self.project_id
            or self.assessment.analysis_hash != self.analysis_hash
            or self.assessment.from_snapshot_id != self.from_snapshot_id
            or self.assessment.to_snapshot_id != self.to_snapshot_id
        ):
            raise ValueError("stored change assessment identity does not match payload")
        return self


RawReleaseEvidence = (
    StoredCostEvaluation | FirmwareBuildEvidence | TestExecutionEvidence
)


class StoredRawReleaseEvidence(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    evidence_kind: ReleaseEvidenceKind
    evidence_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    change_analysis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    hardware_revision_id: str = Field(min_length=1)
    evidence: RawReleaseEvidence
    stored_at: datetime

    @field_validator("stored_at")
    @classmethod
    def stored_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "stored_at")

    @model_validator(mode="after")
    def raw_evidence_must_match_its_release_target(self) -> StoredRawReleaseEvidence:
        if canonical_sha256(self.evidence) != self.evidence_hash:
            raise ValueError("evidence_hash does not reproduce raw release evidence")
        if isinstance(self.evidence, StoredCostEvaluation):
            expected_kind = ReleaseEvidenceKind.BOM_COST
            evidence_id = self.evidence.evidence_id
            project_id = self.evidence.project_id
            hardware_revision_id = self.evidence.revision_id
            occurred_at = self.evidence.evaluated_at
        elif isinstance(self.evidence, FirmwareBuildEvidence):
            expected_kind = ReleaseEvidenceKind.FIRMWARE_BUILD
            evidence_id = self.evidence.evidence_id
            project_id = self.evidence.project_id
            hardware_revision_id = self.evidence.hardware_revision_id
            occurred_at = self.evidence.completed_at
            if (
                self.evidence.snapshot_hash != self.snapshot_hash
                or self.evidence.change_analysis_hash != self.change_analysis_hash
            ):
                raise ValueError("firmware evidence target does not match wrapper")
        else:
            expected_kind = ReleaseEvidenceKind.TEST_RESULT
            evidence_id = self.evidence.evidence_id
            project_id = self.evidence.project_id
            hardware_revision_id = self.evidence.hardware_revision_id
            occurred_at = self.evidence.recorded_at
            if (
                self.evidence.snapshot_hash != self.snapshot_hash
                or self.evidence.change_analysis_hash != self.change_analysis_hash
            ):
                raise ValueError("test evidence target does not match wrapper")
        if (
            self.evidence_kind is not expected_kind
            or self.evidence_id != evidence_id
            or self.project_id != project_id
            or self.hardware_revision_id != hardware_revision_id
        ):
            raise ValueError("raw release evidence identity does not match payload")
        if occurred_at > self.stored_at:
            raise ValueError("raw release evidence cannot be stored before observation")
        return self


class StoredReleaseDecision(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    previous_decision_hash: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    decision_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    report_id: str = Field(min_length=1)
    decision: ReleaseReadinessDecision
    stored_at: datetime

    @field_validator("stored_at")
    @classmethod
    def stored_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "stored_at")

    @model_validator(mode="after")
    def decision_identity_and_chain_shape_must_match(self) -> StoredReleaseDecision:
        if (self.sequence == 1) != (self.previous_decision_hash is None):
            raise ValueError("only the first release decision may omit its predecessor")
        if (
            self.decision.report.project_id != self.project_id
            or self.decision.decision_hash != self.decision_hash
            or self.decision.report.report_id != self.report_id
        ):
            raise ValueError("stored release decision identity does not match payload")
        if self.decision.report.evaluated_at > self.stored_at:
            raise ValueError("release decision cannot be stored before evaluation")
        return self


class StoredAutomaticReverificationTrigger(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    trigger_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    project_id: str = Field(min_length=1)
    analysis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    candidate_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    preview_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    plan_verification_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evidence_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    approval_id: str = Field(min_length=1)
    approved_by: str = Field(min_length=1)
    approved_at: datetime
    local_installation_id: str = Field(min_length=1)
    org_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    created_at: datetime

    @field_validator("approved_at", "created_at")
    @classmethod
    def trigger_timestamps_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "automatic reverification timestamp")

    @model_validator(mode="after")
    def trigger_hash_must_match_payload(self) -> StoredAutomaticReverificationTrigger:
        expected = canonical_sha256(
            self.model_copy(update={"trigger_hash": "sha256:" + "0" * 64})
        )
        if self.trigger_hash != expected:
            raise ValueError("automatic reverification trigger hash does not match")
        if self.approved_at > self.created_at:
            raise ValueError(
                "automatic reverification approval cannot be from the future"
            )
        if self.approved_by != self.actor_id:
            raise ValueError(
                "automatic reverification approval actor does not match trigger actor"
            )
        return self


class StoredAutomaticReverificationReceipt(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    receipt_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    trigger_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    project_id: str = Field(min_length=1)
    analysis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    candidate_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    preview_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    plan_verification_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    decision_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    release_status: Literal["READY", "BLOCKED"]
    completed_at: datetime

    @field_validator("completed_at")
    @classmethod
    def completion_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "automatic reverification completion")

    @model_validator(mode="after")
    def receipt_hash_must_match_payload(self) -> StoredAutomaticReverificationReceipt:
        expected = canonical_sha256(
            self.model_copy(update={"receipt_hash": "sha256:" + "0" * 64})
        )
        if self.receipt_hash != expected:
            raise ValueError("automatic reverification receipt hash does not match")
        return self


class StoredAutomaticReverificationFailure(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    failure_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    trigger_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    project_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    error_code: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$"
    )
    failed_at: datetime

    @field_validator("failed_at")
    @classmethod
    def failure_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "automatic reverification failure")

    @model_validator(mode="after")
    def failure_hash_must_match_payload(self) -> StoredAutomaticReverificationFailure:
        expected = canonical_sha256(
            self.model_copy(update={"failure_hash": "sha256:" + "0" * 64})
        )
        if self.failure_hash != expected:
            raise ValueError("automatic reverification failure hash does not match")
        return self


__all__ = [
    "RawReleaseEvidence",
    "StoredChangeImpactAssessment",
    "StoredAutomaticReverificationReceipt",
    "StoredAutomaticReverificationFailure",
    "StoredAutomaticReverificationTrigger",
    "StoredConnectorSnapshot",
    "StoredRawReleaseEvidence",
    "StoredReleaseDecision",
    "StoredReleasePolicy",
]
