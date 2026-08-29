from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from pydantic import Field, field_serializer, field_validator, model_validator

from forge_core.models import ContractModel, Verdict


class SourceSystem(StrEnum):
    CAD = "cad"
    PLM = "plm"
    GIT = "git"
    CI = "ci"
    ERP = "erp"
    SUPPLIER = "supplier"
    MANUAL = "manual"


class ArtifactDomain(StrEnum):
    HARDWARE = "hardware"
    FIRMWARE = "firmware"
    BOM = "bom"
    TEST = "test"
    PROTOCOL = "protocol"
    DOCUMENTATION = "documentation"


class FindingSeverity(StrEnum):
    BLOCKER = "blocker"
    WARNING = "warning"


class EvidenceTier(StrEnum):
    STATIC = "static"
    SIMULATION = "simulation"
    BENCH = "bench"
    HIL = "hil"
    PHYSICAL_DEVICE = "physical_device"


class ReleaseEvidenceKind(StrEnum):
    BOM_COST = "bom_cost"
    FIRMWARE_BUILD = "firmware_build"
    TEST_RESULT = "test_result"


class RetestStatus(StrEnum):
    REQUIRED = "required"
    PASSED = "passed"
    FAILED = "failed"
    MISSING = "missing"


class ReleaseStatus(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


class ExternalArtifactRef(ContractModel):
    """Immutable pointer to an artifact owned by an external source system."""

    artifact_id: str = Field(min_length=1)
    domain: ArtifactDomain
    source_system: SourceSystem
    source_revision: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    captured_at: datetime

    @field_validator("captured_at")
    @classmethod
    def captured_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "captured_at")


class ConnectorSnapshot(ContractModel):
    """Read-only connector input; FORGE never becomes the authoring authority."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    snapshot_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    hardware_revision_id: str = Field(min_length=1)
    read_only: Literal[True] = True
    captured_at: datetime
    artifacts: tuple[ExternalArtifactRef, ...] = Field(min_length=1)

    @field_validator("captured_at")
    @classmethod
    def snapshot_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "captured_at")

    @model_validator(mode="after")
    def artifact_identity_must_be_unique_and_not_from_future(
        self,
    ) -> ConnectorSnapshot:
        identities = [(item.source_system, item.artifact_id) for item in self.artifacts]
        if len(identities) != len(set(identities)):
            raise ValueError("connector snapshot artifact identities must be unique")
        if any(item.captured_at > self.captured_at for item in self.artifacts):
            raise ValueError("artifact capture cannot occur after its snapshot")
        return self


class ChangeImpact(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    from_revision_id: str = Field(min_length=1)
    to_revision_id: str = Field(min_length=1)
    changed_domains: tuple[ArtifactDomain, ...] = Field(min_length=1)
    affected_domains: tuple[ArtifactDomain, ...] = Field(min_length=1)
    reasons: Mapping[str, tuple[str, ...]]

    @field_validator("changed_domains", "affected_domains")
    @classmethod
    def domains_must_be_unique(
        cls, value: tuple[ArtifactDomain, ...]
    ) -> tuple[ArtifactDomain, ...]:
        if len(value) != len(set(value)):
            raise ValueError("change-impact domains must be unique")
        return value

    @field_validator("reasons", mode="after")
    @classmethod
    def freeze_reasons(
        cls, value: Mapping[str, tuple[str, ...]]
    ) -> Mapping[str, tuple[str, ...]]:
        copied = dict(value)
        if any(not reasons for reasons in copied.values()):
            raise ValueError("change-impact reasons cannot be empty")
        if any(len(reasons) != len(set(reasons)) for reasons in copied.values()):
            raise ValueError("change-impact reasons must be unique")
        return MappingProxyType(copied)

    @field_serializer("reasons")
    def serialize_reasons(
        self, value: Mapping[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        return dict(value)

    @model_validator(mode="after")
    def changed_domains_must_be_affected(self) -> ChangeImpact:
        if not set(self.changed_domains).issubset(self.affected_domains):
            raise ValueError("changed domains must also be affected domains")
        expected_reason_keys = {domain.value for domain in self.affected_domains}
        if set(self.reasons) != expected_reason_keys:
            raise ValueError("reasons must exactly cover every affected domain")
        return self


class ConsistencyFinding(ContractModel):
    finding_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    severity: FindingSeverity
    summary: str = Field(min_length=1)
    affected_domains: tuple[ArtifactDomain, ...] = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)

    @field_validator("affected_domains")
    @classmethod
    def finding_domains_must_be_unique(
        cls, value: tuple[ArtifactDomain, ...]
    ) -> tuple[ArtifactDomain, ...]:
        if len(value) != len(set(value)):
            raise ValueError("finding domains must be unique")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def evidence_refs_must_be_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("finding evidence references must be unique")
        return value


class ReleaseEvidence(ContractModel):
    evidence_id: str = Field(min_length=1)
    evidence_kind: ReleaseEvidenceKind
    project_id: str = Field(min_length=1)
    hardware_revision_id: str = Field(min_length=1)
    snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    change_analysis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    test_id: str = Field(min_length=1)
    tier: EvidenceTier
    verdict: Verdict
    source_system: SourceSystem
    source_revision: str = Field(min_length=1)
    source_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    result_ref: str = Field(min_length=1)
    recorded_at: datetime
    fixture_id: str | None = Field(default=None, min_length=1)
    device_instance_id: str | None = Field(default=None, min_length=1)

    @field_validator("recorded_at")
    @classmethod
    def evidence_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "recorded_at")

    @model_validator(mode="after")
    def physical_context_must_match_tier(self) -> ReleaseEvidence:
        if self.tier is EvidenceTier.PHYSICAL_DEVICE:
            if self.device_instance_id is None:
                raise ValueError("physical-device evidence requires a device instance")
        elif self.device_instance_id is not None:
            raise ValueError("only physical-device evidence may bind a device instance")
        if self.tier in {EvidenceTier.BENCH, EvidenceTier.HIL}:
            if self.fixture_id is None:
                raise ValueError("bench and HIL evidence require a fixture")
        elif self.fixture_id is not None:
            raise ValueError("only bench and HIL evidence may bind a fixture")
        if self.evidence_kind is ReleaseEvidenceKind.BOM_COST and (
            self.test_id != "bom-provenance-validation"
            or self.tier is not EvidenceTier.STATIC
            or self.source_system is not SourceSystem.SUPPLIER
        ):
            raise ValueError(
                "BOM cost evidence requires supplier-sourced static "
                "bom-provenance-validation"
            )
        if self.evidence_kind is ReleaseEvidenceKind.FIRMWARE_BUILD and (
            self.test_id != "firmware-build" or self.tier is not EvidenceTier.STATIC
        ):
            raise ValueError(
                "firmware build evidence requires static firmware-build test ID"
            )
        return self


class RetestRequirement(ContractModel):
    retest_id: str = Field(min_length=1)
    test_id: str = Field(min_length=1)
    required_tier: EvidenceTier
    status: RetestStatus
    triggered_by: tuple[str, ...] = Field(min_length=1)
    reason_codes: tuple[str, ...] = Field(min_length=1)
    satisfied_by_evidence_id: str | None = Field(default=None, min_length=1)

    @field_validator("triggered_by", "reason_codes")
    @classmethod
    def retest_explanations_must_be_unique(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("retest triggers and reasons must be unique")
        return value

    @model_validator(mode="after")
    def passed_retest_requires_evidence(self) -> RetestRequirement:
        if self.status is RetestStatus.PASSED:
            if self.satisfied_by_evidence_id is None:
                raise ValueError("passed retests require evidence")
        elif self.satisfied_by_evidence_id is not None:
            raise ValueError("non-passed retests cannot claim satisfying evidence")
        return self


class ReleaseReadinessReport(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    report_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    hardware_revision_id: str = Field(min_length=1)
    snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    change_analysis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evaluated_at: datetime
    status: ReleaseStatus
    findings: tuple[ConsistencyFinding, ...]
    required_retests: tuple[RetestRequirement, ...]
    evidence: tuple[ReleaseEvidence, ...]
    blocker_codes: tuple[str, ...]

    @field_validator("evaluated_at")
    @classmethod
    def report_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "evaluated_at")

    @model_validator(mode="after")
    def decision_must_follow_evidence(self) -> ReleaseReadinessReport:
        finding_ids = [item.finding_id for item in self.findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("release finding IDs must be unique")
        retest_ids = [item.retest_id for item in self.required_retests]
        if len(retest_ids) != len(set(retest_ids)):
            raise ValueError("release retest IDs must be unique")
        evidence_by_id = {item.evidence_id: item for item in self.evidence}
        if len(evidence_by_id) != len(self.evidence):
            raise ValueError("release evidence IDs must be unique")
        if any(
            item.project_id != self.project_id
            or item.hardware_revision_id != self.hardware_revision_id
            or item.snapshot_hash != self.snapshot_hash
            or item.change_analysis_hash != self.change_analysis_hash
            for item in self.evidence
        ):
            raise ValueError("release evidence must target the exact report snapshot")
        if any(item.recorded_at > self.evaluated_at for item in self.evidence):
            raise ValueError("release evidence cannot be recorded after evaluation")
        unresolved: list[str] = []
        for finding in self.findings:
            if finding.severity is FindingSeverity.BLOCKER:
                unresolved.append(f"finding:{finding.rule_id}")
        for retest in self.required_retests:
            if retest.status is not RetestStatus.PASSED:
                unresolved.append(
                    f"retest:{retest.test_id}:{retest.required_tier.value}:"
                    f"{retest.status.value}"
                )
                continue
            assert retest.satisfied_by_evidence_id is not None
            evidence = evidence_by_id.get(retest.satisfied_by_evidence_id)
            if evidence is None:
                raise ValueError("passed retest references missing evidence")
            if evidence.test_id != retest.test_id:
                raise ValueError("retest evidence test ID must match exactly")
            if evidence.tier is not retest.required_tier:
                raise ValueError("retest evidence tier must match exactly")
            if evidence.verdict is not Verdict.PASS:
                raise ValueError("passed retest evidence must have PASS verdict")
            expected_kind = (
                ReleaseEvidenceKind.FIRMWARE_BUILD
                if retest.test_id == "firmware-build"
                else ReleaseEvidenceKind.BOM_COST
                if retest.test_id == "bom-provenance-validation"
                else ReleaseEvidenceKind.TEST_RESULT
            )
            if evidence.evidence_kind is not expected_kind:
                raise ValueError("retest evidence kind must match its test contract")
        expected = ReleaseStatus.READY if not unresolved else ReleaseStatus.BLOCKED
        if self.status is not expected:
            raise ValueError("release status must follow findings and retest evidence")
        expected_blockers = tuple(sorted(set(unresolved)))
        if self.blocker_codes != expected_blockers:
            raise ValueError("blocker codes must exactly explain the release decision")
        return self


__all__ = [
    "ArtifactDomain",
    "ChangeImpact",
    "ConnectorSnapshot",
    "ConsistencyFinding",
    "EvidenceTier",
    "ExternalArtifactRef",
    "FindingSeverity",
    "ReleaseEvidence",
    "ReleaseEvidenceKind",
    "ReleaseReadinessReport",
    "ReleaseStatus",
    "RetestRequirement",
    "RetestStatus",
    "SourceSystem",
]
