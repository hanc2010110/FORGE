from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from pydantic import Field, field_serializer, field_validator, model_validator

from forge_core.hashing import canonical_sha256
from forge_core.models import ContractModel, Verdict


class ArtifactKind(StrEnum):
    SYSTEM_SPEC = "system_spec"
    MECHANICAL = "mechanical"
    ELECTRICAL = "electrical"
    INTERFACE = "interface"
    BOM = "bom"
    BUDGET_POLICY = "budget_policy"
    SOFTWARE = "software"
    TEST_PLAN = "test_plan"
    DOCUMENTATION = "documentation"
    DEVICE_PROFILE = "device_profile"
    TOOLCHAIN = "toolchain"


class EvidenceClass(StrEnum):
    ANALYSIS = "analysis"
    COST = "cost"
    ELECTRICAL_RULES = "electrical_rules"
    SOFTWARE_BUILD = "software_build"
    SIL = "sil"
    HIL = "hil"
    COMMISSIONING = "commissioning"


class DesignMaturity(StrEnum):
    SPECIFIED = "specified"
    ANALYZED = "analyzed"
    BUILDABLE = "buildable"
    BENCH_VERIFIED = "bench_verified"
    COMMISSIONED = "commissioned"


class RevisionArtifact(ContractModel):
    artifact_id: str = Field(min_length=1)
    kind: ArtifactKind
    version: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class SystemDesignRevision(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    revision_number: int = Field(ge=1)
    parent_revision_id: str | None = None
    approved_at: datetime
    artifacts: tuple[RevisionArtifact, ...]

    @model_validator(mode="after")
    def validate_revision_shape(self) -> SystemDesignRevision:
        kinds = [artifact.kind for artifact in self.artifacts]
        if len(kinds) != len(set(kinds)):
            raise ValueError("a design revision may contain one artifact per kind")
        required = {
            ArtifactKind.SYSTEM_SPEC,
            ArtifactKind.INTERFACE,
            ArtifactKind.BOM,
            ArtifactKind.BUDGET_POLICY,
        }
        missing = required.difference(kinds)
        if missing:
            names = ", ".join(sorted(item.value for item in missing))
            raise ValueError(f"design revision is missing required artifacts: {names}")
        if self.revision_number == 1 and self.parent_revision_id is not None:
            raise ValueError("the first revision cannot have a parent")
        if self.revision_number > 1 and self.parent_revision_id is None:
            raise ValueError("later revisions require parent_revision_id")
        return self

    def artifact_map(self) -> Mapping[ArtifactKind, RevisionArtifact]:
        return MappingProxyType(
            {artifact.kind: artifact for artifact in self.artifacts}
        )


class EvidenceDependencySnapshot(ContractModel):
    evidence_class: EvidenceClass
    artifacts: Mapping[str, str]

    @field_validator("artifacts", mode="after")
    @classmethod
    def freeze_artifacts(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        return MappingProxyType(dict(value))

    @field_serializer("artifacts")
    def serialize_artifacts(self, value: Mapping[str, str]) -> dict[str, str]:
        return dict(value)


class EvidenceRecord(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    evidence_id: str = Field(min_length=1)
    evidence_class: EvidenceClass
    produced_for_revision_id: str = Field(min_length=1)
    dependency_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    verdict: Verdict
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    recorded_at: datetime


class InvalidationReport(ContractModel):
    from_revision_id: str
    to_revision_id: str
    changed_artifacts: tuple[ArtifactKind, ...]
    invalidated_evidence: tuple[EvidenceClass, ...]
    retained_evidence: tuple[EvidenceClass, ...]
    reasons: Mapping[str, tuple[str, ...]]

    @field_validator("reasons", mode="after")
    @classmethod
    def freeze_reasons(
        cls, value: Mapping[str, tuple[str, ...]]
    ) -> Mapping[str, tuple[str, ...]]:
        return MappingProxyType(dict(value))

    @field_serializer("reasons")
    def serialize_reasons(
        self, value: Mapping[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        return dict(value)


class MaturityAssessment(ContractModel):
    revision_id: str
    maturity: DesignMaturity
    valid_evidence: tuple[EvidenceClass, ...]
    stale_evidence: tuple[EvidenceClass, ...]
    blockers: tuple[str, ...]


EVIDENCE_DEPENDENCIES: Mapping[EvidenceClass, frozenset[ArtifactKind]] = {
    EvidenceClass.ANALYSIS: frozenset(
        {
            ArtifactKind.SYSTEM_SPEC,
            ArtifactKind.MECHANICAL,
            ArtifactKind.INTERFACE,
        }
    ),
    EvidenceClass.COST: frozenset(
        {
            ArtifactKind.BOM,
            ArtifactKind.BUDGET_POLICY,
        }
    ),
    EvidenceClass.ELECTRICAL_RULES: frozenset(
        {
            ArtifactKind.SYSTEM_SPEC,
            ArtifactKind.ELECTRICAL,
            ArtifactKind.INTERFACE,
            ArtifactKind.DEVICE_PROFILE,
        }
    ),
    EvidenceClass.SOFTWARE_BUILD: frozenset(
        {
            ArtifactKind.SYSTEM_SPEC,
            ArtifactKind.ELECTRICAL,
            ArtifactKind.INTERFACE,
            ArtifactKind.SOFTWARE,
            ArtifactKind.DEVICE_PROFILE,
            ArtifactKind.TOOLCHAIN,
        }
    ),
    EvidenceClass.SIL: frozenset(
        {
            ArtifactKind.SYSTEM_SPEC,
            ArtifactKind.INTERFACE,
            ArtifactKind.SOFTWARE,
            ArtifactKind.TEST_PLAN,
            ArtifactKind.DEVICE_PROFILE,
            ArtifactKind.TOOLCHAIN,
        }
    ),
    EvidenceClass.HIL: frozenset(
        {
            ArtifactKind.SYSTEM_SPEC,
            ArtifactKind.MECHANICAL,
            ArtifactKind.ELECTRICAL,
            ArtifactKind.INTERFACE,
            ArtifactKind.BOM,
            ArtifactKind.SOFTWARE,
            ArtifactKind.TEST_PLAN,
            ArtifactKind.DEVICE_PROFILE,
            ArtifactKind.TOOLCHAIN,
        }
    ),
    EvidenceClass.COMMISSIONING: frozenset(ArtifactKind),
}


MATURITY_REQUIREMENTS: tuple[tuple[DesignMaturity, frozenset[EvidenceClass]], ...] = (
    (DesignMaturity.ANALYZED, frozenset({EvidenceClass.ANALYSIS})),
    (
        DesignMaturity.BUILDABLE,
        frozenset(
            {
                EvidenceClass.COST,
                EvidenceClass.ELECTRICAL_RULES,
                EvidenceClass.SOFTWARE_BUILD,
            }
        ),
    ),
    (
        DesignMaturity.BENCH_VERIFIED,
        frozenset({EvidenceClass.SIL, EvidenceClass.HIL}),
    ),
    (DesignMaturity.COMMISSIONED, frozenset({EvidenceClass.COMMISSIONING})),
)


def revision_hash(revision: SystemDesignRevision) -> str:
    return canonical_sha256(revision)


def dependency_hash(
    revision: SystemDesignRevision, evidence_class: EvidenceClass
) -> str:
    artifacts = revision.artifact_map()
    selected = {
        kind.value: artifacts[kind].content_hash
        for kind in sorted(EVIDENCE_DEPENDENCIES[evidence_class], key=str)
        if kind in artifacts
    }
    snapshot = EvidenceDependencySnapshot(
        evidence_class=evidence_class,
        artifacts=selected,
    )
    return canonical_sha256(snapshot)


def compare_revisions(
    previous: SystemDesignRevision,
    current: SystemDesignRevision,
) -> InvalidationReport:
    if previous.project_id != current.project_id:
        raise ValueError("cannot compare revisions from different projects")
    if current.parent_revision_id != previous.revision_id:
        raise ValueError("current revision must directly reference previous revision")
    if current.revision_number != previous.revision_number + 1:
        raise ValueError("revision numbers must increase by one")

    before = previous.artifact_map()
    after = current.artifact_map()
    changed = tuple(
        kind for kind in ArtifactKind if before.get(kind) != after.get(kind)
    )
    invalidated = tuple(
        evidence_class
        for evidence_class in EvidenceClass
        if EVIDENCE_DEPENDENCIES[evidence_class].intersection(changed)
    )
    retained = tuple(
        evidence_class
        for evidence_class in EvidenceClass
        if evidence_class not in invalidated
    )
    reasons = {
        evidence_class.value: tuple(
            sorted(
                kind.value
                for kind in EVIDENCE_DEPENDENCIES[evidence_class].intersection(changed)
            )
        )
        for evidence_class in invalidated
    }
    return InvalidationReport(
        from_revision_id=previous.revision_id,
        to_revision_id=current.revision_id,
        changed_artifacts=changed,
        invalidated_evidence=invalidated,
        retained_evidence=retained,
        reasons=reasons,
    )


def assess_maturity(
    revision: SystemDesignRevision,
    evidence: Iterable[EvidenceRecord],
) -> MaturityAssessment:
    by_class: dict[EvidenceClass, EvidenceRecord] = {}
    for record in evidence:
        if record.evidence_class in by_class:
            raise ValueError("only one current evidence record is allowed per class")
        by_class[record.evidence_class] = record

    valid: set[EvidenceClass] = set()
    stale: set[EvidenceClass] = set()
    for evidence_class, record in by_class.items():
        if record.dependency_hash == dependency_hash(revision, evidence_class):
            valid.add(evidence_class)
        else:
            stale.add(evidence_class)

    maturity = DesignMaturity.SPECIFIED
    blockers: list[str] = []
    accumulated: set[EvidenceClass] = set()
    for candidate, requirements in MATURITY_REQUIREMENTS:
        accumulated.update(requirements)
        missing = accumulated.difference(valid)
        failed = {
            evidence_class
            for evidence_class in accumulated.intersection(valid)
            if by_class[evidence_class].verdict is not Verdict.PASS
        }
        if missing or failed:
            blockers.extend(
                f"missing:{item.value}" for item in sorted(missing, key=str)
            )
            blockers.extend(
                f"not_pass:{item.value}" for item in sorted(failed, key=str)
            )
            break
        maturity = candidate

    return MaturityAssessment(
        revision_id=revision.revision_id,
        maturity=maturity,
        valid_evidence=tuple(sorted(valid, key=str)),
        stale_evidence=tuple(sorted(stale, key=str)),
        blockers=tuple(blockers),
    )
