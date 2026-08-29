from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from pydantic import Field, field_serializer, field_validator, model_validator

from forge_core.change_management import (
    ArtifactDomain,
    ConnectorSnapshot,
    ConsistencyFinding,
    EvidenceTier,
    FindingSeverity,
    ReleaseEvidence,
    ReleaseEvidenceKind,
    ReleaseReadinessReport,
    ReleaseStatus,
    RetestRequirement,
    RetestStatus,
    SourceSystem,
)
from forge_core.constraints import evaluate_cost
from forge_core.hashing import canonical_sha256
from forge_core.impact_engine import (
    ChangeImpactAssessment,
    connector_snapshot_hash,
)
from forge_core.models import ContractModel, Verdict
from forge_core.persistence import StoredCostEvaluation


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


class EvidenceRejectionReason(StrEnum):
    WRONG_PROJECT = "wrong_project"
    WRONG_HARDWARE_REVISION = "wrong_hardware_revision"
    WRONG_SNAPSHOT = "wrong_snapshot"
    WRONG_CHANGE_ANALYSIS = "wrong_change_analysis"
    FUTURE_TIMESTAMP = "future_timestamp"
    BEFORE_TARGET_SNAPSHOT = "before_target_snapshot"
    STALE = "stale"
    FIRMWARE_SOURCE_MISMATCH = "firmware_source_mismatch"
    PROTOCOL_SCHEMA_TARGET_MISSING = "protocol_schema_target_missing"
    PROTOCOL_SCHEMA_MISMATCH = "protocol_schema_mismatch"
    FIRMWARE_BUILD_NOT_PASSED = "firmware_build_not_passed"
    FIRMWARE_ARTIFACT_MISMATCH = "firmware_artifact_mismatch"
    NOT_REQUIRED = "not_required"
    SUPERSEDED = "superseded"
    AMBIGUOUS_LATEST_TIMESTAMP = "ambiguous_latest_timestamp"
    COST_POLICY_MISMATCH = "cost_policy_mismatch"


class BaselineRetestRule(ContractModel):
    test_id: str = Field(min_length=1)
    required_tier: EvidenceTier
    reason_code: str = Field(min_length=1)


class ReleaseReadinessPolicy(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    policy_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    baseline_retests: tuple[BaselineRetestRule, ...]
    max_age_seconds: Mapping[str, int]
    runtime_test_ids_requiring_build: tuple[str, ...]
    cost_currency: str = Field(pattern=r"^[A-Z]{3}$")
    cost_budget_limit: Decimal = Field(ge=0)
    cost_reserve_rate: Decimal = Field(ge=0)
    cost_dependency_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("max_age_seconds", mode="after")
    @classmethod
    def validate_and_freeze_max_age(cls, value: Mapping[str, int]) -> Mapping[str, int]:
        if set(value) != {tier.value for tier in EvidenceTier}:
            raise ValueError(
                "release policy must define max age for every evidence tier"
            )
        copied = dict(value)
        if any(seconds <= 0 for seconds in copied.values()):
            raise ValueError("release evidence max age must be positive")
        return MappingProxyType(copied)

    @field_serializer("max_age_seconds")
    def serialize_max_age(self, value: Mapping[str, int]) -> dict[str, int]:
        return dict(value)

    @model_validator(mode="after")
    def baseline_and_runtime_rules_must_be_safe(self) -> ReleaseReadinessPolicy:
        if (
            not self.cost_budget_limit.is_finite()
            or not self.cost_reserve_rate.is_finite()
        ):
            raise ValueError("release cost policy values must be finite")
        baseline_keys = [
            (item.test_id, item.required_tier) for item in self.baseline_retests
        ]
        if len(baseline_keys) != len(set(baseline_keys)):
            raise ValueError("baseline release retests must be unique")
        required = {
            ("bom-provenance-validation", EvidenceTier.STATIC),
            ("firmware-build", EvidenceTier.STATIC),
        }
        if not required.issubset(baseline_keys):
            raise ValueError("release policy cannot remove BOM or firmware baselines")
        if len(self.runtime_test_ids_requiring_build) != len(
            set(self.runtime_test_ids_requiring_build)
        ):
            raise ValueError("runtime test IDs must be unique")
        minimum_runtime_tests = {
            "hil-regression",
            "physical-device-smoke",
            "protocol-conformance",
        }
        if not minimum_runtime_tests.issubset(self.runtime_test_ids_requiring_build):
            raise ValueError("release policy cannot remove runtime build bindings")
        return self

    def max_age(self, tier: EvidenceTier) -> timedelta:
        return timedelta(seconds=self.max_age_seconds[tier.value])


DEFAULT_RELEASE_READINESS_POLICY = ReleaseReadinessPolicy(
    policy_id="forge-default-release-readiness",
    policy_version="1.0.0",
    baseline_retests=(
        BaselineRetestRule(
            test_id="bom-provenance-validation",
            required_tier=EvidenceTier.STATIC,
            reason_code="release_requires_current_bom_cost",
        ),
        BaselineRetestRule(
            test_id="firmware-build",
            required_tier=EvidenceTier.STATIC,
            reason_code="release_requires_current_firmware_build",
        ),
    ),
    max_age_seconds={
        EvidenceTier.STATIC.value: 30 * 24 * 60 * 60,
        EvidenceTier.SIMULATION.value: 7 * 24 * 60 * 60,
        EvidenceTier.BENCH.value: 7 * 24 * 60 * 60,
        EvidenceTier.HIL.value: 7 * 24 * 60 * 60,
        EvidenceTier.PHYSICAL_DEVICE.value: 24 * 60 * 60,
    },
    runtime_test_ids_requiring_build=(
        "hil-regression",
        "physical-device-smoke",
        "protocol-conformance",
    ),
    cost_currency="USD",
    cost_budget_limit=Decimal("30.00"),
    cost_reserve_rate=Decimal("0.10"),
    cost_dependency_hash="sha256:" + "d" * 64,
)


def cost_evidence_matches_policy(
    evidence: StoredCostEvaluation, policy: ReleaseReadinessPolicy
) -> bool:
    return (
        evidence.dependency_hash == policy.cost_dependency_hash
        and evidence.evaluation.currency == policy.cost_currency
        and str(evidence.evaluation.budget_limit) == str(policy.cost_budget_limit)
        and str(evidence.evaluation.reserve_rate) == str(policy.cost_reserve_rate)
    )


class FirmwareBuildEvidence(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    evidence_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    hardware_revision_id: str = Field(min_length=1)
    snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    change_analysis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    build_id: str = Field(min_length=1)
    firmware_artifact_id: str = Field(min_length=1)
    firmware_source_revision: str = Field(min_length=1)
    firmware_source_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    toolchain_id: str = Field(min_length=1)
    toolchain_version: str = Field(min_length=1)
    toolchain_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    protocol_schema_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    firmware_build_artifact_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    verdict: Verdict
    source_system: Literal[SourceSystem.CI] = SourceSystem.CI
    result_ref: str = Field(min_length=1)
    started_at: datetime
    completed_at: datetime

    @field_validator("started_at", "completed_at")
    @classmethod
    def timestamps_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "firmware build timestamp")

    @model_validator(mode="after")
    def completion_must_follow_start(self) -> FirmwareBuildEvidence:
        if self.completed_at < self.started_at:
            raise ValueError("firmware build completion cannot predate its start")
        return self


class TestExecutionEvidence(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    evidence_id: str = Field(min_length=1)
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
    firmware_build_artifact_hash: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    fixture_id: str | None = Field(default=None, min_length=1)
    device_instance_id: str | None = Field(default=None, min_length=1)

    @field_validator("recorded_at")
    @classmethod
    def timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "test evidence recorded_at")

    @model_validator(mode="after")
    def source_and_physical_context_must_be_explicit(self) -> TestExecutionEvidence:
        if self.test_id in {"firmware-build", "bom-provenance-validation"}:
            raise ValueError("build and BOM evidence use their dedicated contracts")
        if self.source_system not in {SourceSystem.CI, SourceSystem.MANUAL}:
            raise ValueError("test evidence must be collected from CI or a manual run")
        if self.tier is EvidenceTier.PHYSICAL_DEVICE:
            if self.device_instance_id is None:
                raise ValueError("physical-device test evidence requires a device")
        elif self.device_instance_id is not None:
            raise ValueError("only physical-device test evidence may name a device")
        if self.tier in {EvidenceTier.BENCH, EvidenceTier.HIL}:
            if self.fixture_id is None:
                raise ValueError("bench and HIL test evidence require a fixture")
        elif self.fixture_id is not None:
            raise ValueError("only bench and HIL test evidence may name a fixture")
        return self


class EvidenceRejection(ContractModel):
    evidence_id: str = Field(min_length=1)
    reason: EvidenceRejectionReason


class _DecisionPayload(ContractModel):
    policy: ReleaseReadinessPolicy
    policy_hash: str
    change_assessment: ChangeImpactAssessment
    target_snapshot: ConnectorSnapshot
    report: ReleaseReadinessReport
    cost_evaluation_hash: str | None
    selected_firmware_build: FirmwareBuildEvidence | None
    selected_test_results: tuple[TestExecutionEvidence, ...]
    selected_evidence_ids: tuple[str, ...]
    rejected_evidence: tuple[EvidenceRejection, ...]


class ReleaseReadinessDecision(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    policy: ReleaseReadinessPolicy
    policy_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    change_assessment: ChangeImpactAssessment
    target_snapshot: ConnectorSnapshot
    report: ReleaseReadinessReport
    cost_evaluation: StoredCostEvaluation | None
    cost_evaluation_hash: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    selected_firmware_build: FirmwareBuildEvidence | None
    selected_test_results: tuple[TestExecutionEvidence, ...]
    selected_evidence_ids: tuple[str, ...]
    rejected_evidence: tuple[EvidenceRejection, ...]
    human_readable_report: str = Field(min_length=1)
    decision_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def hashes_and_human_report_must_reproduce(self) -> ReleaseReadinessDecision:
        if self.policy_hash != canonical_sha256(self.policy):
            raise ValueError("release policy hash does not match policy")
        if (
            self.change_assessment.analysis_hash != self.report.change_analysis_hash
            or self.change_assessment.project_id != self.report.project_id
            or self.change_assessment.to_hardware_revision_id
            != self.report.hardware_revision_id
            or self.change_assessment.to_snapshot_hash != self.report.snapshot_hash
        ):
            raise ValueError("change assessment does not reproduce the release report")
        report_findings_by_id = {item.finding_id: item for item in self.report.findings}
        if any(
            report_findings_by_id.get(item.finding_id) != item
            for item in self.change_assessment.findings
        ):
            raise ValueError("release report omits or changes an impact finding")
        expected_requirements = {
            (item.test_id, item.required_tier): item
            for item in _with_baselines(self.change_assessment, self.policy)
        }
        actual_requirements = {
            (item.test_id, item.required_tier): item
            for item in self.report.required_retests
        }
        if len(actual_requirements) != len(self.report.required_retests) or set(
            actual_requirements
        ) != set(expected_requirements):
            raise ValueError("release report retest set does not match its assessment")
        for key, expected in expected_requirements.items():
            actual = actual_requirements[key]
            if (
                actual.retest_id != expected.retest_id
                or actual.triggered_by != expected.triggered_by
                or actual.reason_codes != expected.reason_codes
            ):
                raise ValueError(
                    "release report retest provenance does not match its assessment"
                )
        if (
            connector_snapshot_hash(self.target_snapshot) != self.report.snapshot_hash
            or self.target_snapshot.project_id != self.report.project_id
            or self.target_snapshot.hardware_revision_id
            != self.report.hardware_revision_id
        ):
            raise ValueError("target snapshot does not reproduce the release report")
        if self.target_snapshot.captured_at > self.report.evaluated_at:
            raise ValueError("release evaluation predates its target snapshot")
        expected_cost_hash = (
            canonical_sha256(self.cost_evaluation)
            if self.cost_evaluation is not None
            else None
        )
        if self.cost_evaluation_hash != expected_cost_hash:
            raise ValueError("release cost hash does not match cost provenance")
        if self.cost_evaluation is not None and (
            self.cost_evaluation.project_id != self.report.project_id
            or self.cost_evaluation.revision_id != self.report.hardware_revision_id
        ):
            raise ValueError("release cost provenance must target the report revision")
        target_bom_hashes = {
            item.content_hash
            for item in self.target_snapshot.artifacts
            if item.domain is ArtifactDomain.BOM
        }
        if (
            self.cost_evaluation is not None
            and self.cost_evaluation.bom_artifact_hash not in target_bom_hashes
        ):
            raise ValueError("release cost provenance BOM is not in target snapshot")
        freshness_cutoff = self.report.evaluated_at - self.policy.max_age(
            EvidenceTier.STATIC
        )
        if self.cost_evaluation is not None:
            if not cost_evidence_matches_policy(self.cost_evaluation, self.policy):
                raise ValueError(
                    "selected BOM cost does not bind release budget policy"
                )
            required_parts = {
                item.part_number for item in self.cost_evaluation.bom if item.required
            }
            if (
                self.cost_evaluation.evaluated_at > self.report.evaluated_at
                or self.cost_evaluation.evaluated_at < freshness_cutoff
                or self.cost_evaluation.evaluated_at < self.target_snapshot.captured_at
                or any(
                    quote.part_number in required_parts
                    and quote.observed_at < freshness_cutoff
                    for quote in self.cost_evaluation.quotes
                )
            ):
                raise ValueError("selected BOM cost provenance is stale")
        report_ids = tuple(sorted(item.evidence_id for item in self.report.evidence))
        if self.selected_evidence_ids != report_ids:
            raise ValueError("selected evidence IDs must exactly match report evidence")
        rejected_ids = [item.evidence_id for item in self.rejected_evidence]
        if len(rejected_ids) != len(set(rejected_ids)):
            raise ValueError("rejected evidence IDs must be unique")
        if set(rejected_ids).intersection(self.selected_evidence_ids):
            raise ValueError("evidence cannot be both selected and rejected")
        build_ids = {
            item.evidence_id
            for item in self.report.evidence
            if item.evidence_kind is ReleaseEvidenceKind.FIRMWARE_BUILD
        }
        expected_build_ids = (
            {self.selected_firmware_build.evidence_id}
            if self.selected_firmware_build is not None
            else set()
        )
        if build_ids != expected_build_ids:
            raise ValueError("selected firmware build must match report evidence")
        if self.selected_firmware_build is not None:
            if self.selected_firmware_build.completed_at > self.report.evaluated_at:
                raise ValueError("selected firmware build is from the future")
            if (
                self.selected_firmware_build.completed_at
                < self.target_snapshot.captured_at
            ):
                raise ValueError("selected firmware build predates target snapshot")
            if self.selected_firmware_build.completed_at < (
                self.report.evaluated_at - self.policy.max_age(EvidenceTier.STATIC)
            ):
                raise ValueError("selected firmware build is stale")
            firmware_refs = {
                (item.artifact_id, item.source_revision, item.content_hash)
                for item in self.target_snapshot.artifacts
                if item.domain is ArtifactDomain.FIRMWARE
                and item.source_system is SourceSystem.GIT
            }
            selected_source = (
                self.selected_firmware_build.firmware_artifact_id,
                self.selected_firmware_build.firmware_source_revision,
                self.selected_firmware_build.firmware_source_hash,
            )
            if selected_source not in firmware_refs:
                raise ValueError("selected firmware build source is not in snapshot")
            expected_protocol_schema_hash = (
                self.change_assessment.target_protocol_schema_hash
            )
            if expected_protocol_schema_hash is None:
                raise ValueError("selected build requires a target protocol schema")
            if (
                self.selected_firmware_build.protocol_schema_hash
                != expected_protocol_schema_hash
            ):
                raise ValueError("selected firmware build protocol schema is incorrect")
            normalized_build = _normalize_build(self.selected_firmware_build)
            report_build = next(
                item
                for item in self.report.evidence
                if item.evidence_id == self.selected_firmware_build.evidence_id
            )
            if normalized_build != report_build:
                raise ValueError(
                    "selected firmware build does not reproduce report evidence"
                )
        report_test_ids = {
            item.evidence_id
            for item in self.report.evidence
            if item.evidence_kind is ReleaseEvidenceKind.TEST_RESULT
        }
        selected_test_ids = [item.evidence_id for item in self.selected_test_results]
        if (
            report_test_ids != set(selected_test_ids)
            or len(selected_test_ids) != len(set(selected_test_ids))
            or self.selected_test_results
            != tuple(
                sorted(self.selected_test_results, key=lambda item: item.evidence_id)
            )
        ):
            raise ValueError("selected test results must match report evidence")
        report_evidence_by_id = {
            item.evidence_id: item for item in self.report.evidence
        }
        if any(
            _normalize_test(item) != report_evidence_by_id[item.evidence_id]
            for item in self.selected_test_results
        ):
            raise ValueError("selected test result does not reproduce report evidence")
        if any(
            item.recorded_at > self.report.evaluated_at
            for item in self.selected_test_results
        ):
            raise ValueError("selected test result is from the future")
        if any(
            item.recorded_at < self.target_snapshot.captured_at
            for item in self.selected_test_results
        ):
            raise ValueError("selected test result predates target snapshot")
        if any(
            item.recorded_at < self.report.evaluated_at - self.policy.max_age(item.tier)
            for item in self.selected_test_results
        ):
            raise ValueError("selected test result is stale")
        selected_build_artifact_hash = (
            self.selected_firmware_build.firmware_build_artifact_hash
            if self.selected_firmware_build is not None
            and self.selected_firmware_build.verdict is Verdict.PASS
            else None
        )
        for result in self.selected_test_results:
            requires_build = (
                result.test_id in self.policy.runtime_test_ids_requiring_build
                or result.tier
                in {
                    EvidenceTier.SIMULATION,
                    EvidenceTier.HIL,
                    EvidenceTier.PHYSICAL_DEVICE,
                }
            )
            if requires_build and (
                selected_build_artifact_hash is None
                or result.firmware_build_artifact_hash != selected_build_artifact_hash
            ):
                raise ValueError(
                    "selected runtime test does not bind the selected firmware build"
                )
        cost_report_evidence = tuple(
            item
            for item in self.report.evidence
            if item.evidence_kind is ReleaseEvidenceKind.BOM_COST
        )
        if self.cost_evaluation is None:
            if cost_report_evidence:
                raise ValueError(
                    "report cannot contain cost evidence without provenance"
                )
        else:
            if len(cost_report_evidence) != 1:
                raise ValueError(
                    "cost provenance requires one normalized report evidence"
                )
            current_cost = evaluate_cost(
                self.cost_evaluation.bom,
                self.cost_evaluation.quotes,
                currency=self.policy.cost_currency,
                budget_limit=self.policy.cost_budget_limit,
                reserve_rate=self.policy.cost_reserve_rate,
                evaluated_at=self.report.evaluated_at,
            )
            expected_cost = _normalize_cost_for_target(
                self.cost_evaluation,
                self.report.project_id,
                self.report.hardware_revision_id,
                self.report.snapshot_hash,
                self.report.change_analysis_hash,
                current_cost.verdict,
            )
            if cost_report_evidence[0] != expected_cost:
                raise ValueError("cost provenance does not reproduce report evidence")
        if self.rejected_evidence != tuple(
            sorted(self.rejected_evidence, key=lambda item: item.evidence_id)
        ):
            raise ValueError("rejected evidence must be canonically ordered")
        expected_human = render_release_report(
            self.report,
            self.cost_evaluation,
            self.rejected_evidence,
            self.selected_firmware_build,
            self.selected_test_results,
            target_protocol_schema_hash=(
                self.change_assessment.target_protocol_schema_hash
            ),
        )
        if self.human_readable_report != expected_human:
            raise ValueError("human report is not a reproducible view of the decision")
        payload = _DecisionPayload(
            policy=self.policy,
            policy_hash=self.policy_hash,
            change_assessment=self.change_assessment,
            target_snapshot=self.target_snapshot,
            report=self.report,
            cost_evaluation_hash=self.cost_evaluation_hash,
            selected_firmware_build=self.selected_firmware_build,
            selected_test_results=self.selected_test_results,
            selected_evidence_ids=self.selected_evidence_ids,
            rejected_evidence=self.rejected_evidence,
        )
        if self.decision_hash != canonical_sha256(payload):
            raise ValueError("release decision hash does not match decision payload")
        return self


class _ReportIdentity(ContractModel):
    project_id: str
    hardware_revision_id: str
    snapshot_hash: str
    change_analysis_hash: str
    evaluated_at: datetime
    findings: tuple[ConsistencyFinding, ...]
    required_retests: tuple[RetestRequirement, ...]
    evidence: tuple[ReleaseEvidence, ...]


def _target_rejection_reason(
    item: FirmwareBuildEvidence | TestExecutionEvidence,
    assessment: ChangeImpactAssessment,
) -> EvidenceRejectionReason | None:
    if item.project_id != assessment.project_id:
        return EvidenceRejectionReason.WRONG_PROJECT
    if item.hardware_revision_id != assessment.to_hardware_revision_id:
        return EvidenceRejectionReason.WRONG_HARDWARE_REVISION
    if item.snapshot_hash != assessment.to_snapshot_hash:
        return EvidenceRejectionReason.WRONG_SNAPSHOT
    if item.change_analysis_hash != assessment.analysis_hash:
        return EvidenceRejectionReason.WRONG_CHANGE_ANALYSIS
    return None


def _cost_finding(rule_id: str, summary: str, evidence_ref: str) -> ConsistencyFinding:
    return ConsistencyFinding(
        finding_id=f"release:{rule_id}",
        rule_id=rule_id,
        severity=FindingSeverity.BLOCKER,
        summary=summary,
        affected_domains=(ArtifactDomain.BOM, ArtifactDomain.TEST),
        evidence_refs=(evidence_ref,),
    )


def _protocol_finding(
    rule_id: str, summary: str, evidence_ref: str
) -> ConsistencyFinding:
    return ConsistencyFinding(
        finding_id=f"release:{rule_id}",
        rule_id=rule_id,
        severity=FindingSeverity.BLOCKER,
        summary=summary,
        affected_domains=(
            ArtifactDomain.FIRMWARE,
            ArtifactDomain.PROTOCOL,
            ArtifactDomain.TEST,
        ),
        evidence_refs=(evidence_ref,),
    )


def _with_baselines(
    assessment: ChangeImpactAssessment, policy: ReleaseReadinessPolicy
) -> tuple[RetestRequirement, ...]:
    retests = list(assessment.required_retests)
    keys = [(item.test_id, item.required_tier) for item in retests]
    if len(keys) != len(set(keys)):
        raise ValueError("change assessment has duplicate test/tier requirements")
    existing = set(keys)
    for baseline in policy.baseline_retests:
        key = (baseline.test_id, baseline.required_tier)
        if key in existing:
            continue
        retests.append(
            RetestRequirement(
                retest_id=(
                    f"release-baseline:{policy.policy_id}:{policy.policy_version}:"
                    f"{baseline.test_id}:{baseline.required_tier.value}:"
                    f"{assessment.to_hardware_revision_id}"
                ),
                test_id=baseline.test_id,
                required_tier=baseline.required_tier,
                status=RetestStatus.REQUIRED,
                triggered_by=(f"analysis:{assessment.analysis_hash}",),
                reason_codes=(baseline.reason_code,),
            )
        )
    return tuple(sorted(retests, key=lambda item: (item.test_id, item.required_tier)))


def _normalize_build(item: FirmwareBuildEvidence) -> ReleaseEvidence:
    return ReleaseEvidence(
        evidence_id=item.evidence_id,
        evidence_kind=ReleaseEvidenceKind.FIRMWARE_BUILD,
        project_id=item.project_id,
        hardware_revision_id=item.hardware_revision_id,
        snapshot_hash=item.snapshot_hash,
        change_analysis_hash=item.change_analysis_hash,
        test_id="firmware-build",
        tier=EvidenceTier.STATIC,
        verdict=item.verdict,
        source_system=SourceSystem.CI,
        source_revision=item.firmware_source_revision,
        source_hash=item.firmware_source_hash,
        result_ref=item.result_ref,
        recorded_at=item.completed_at,
    )


def _normalize_test(item: TestExecutionEvidence) -> ReleaseEvidence:
    return ReleaseEvidence(
        evidence_id=item.evidence_id,
        evidence_kind=ReleaseEvidenceKind.TEST_RESULT,
        project_id=item.project_id,
        hardware_revision_id=item.hardware_revision_id,
        snapshot_hash=item.snapshot_hash,
        change_analysis_hash=item.change_analysis_hash,
        test_id=item.test_id,
        tier=item.tier,
        verdict=item.verdict,
        source_system=item.source_system,
        source_revision=item.source_revision,
        source_hash=item.source_hash,
        result_ref=item.result_ref,
        recorded_at=item.recorded_at,
        fixture_id=item.fixture_id,
        device_instance_id=item.device_instance_id,
    )


def _normalize_cost(
    record: StoredCostEvaluation,
    assessment: ChangeImpactAssessment,
    verdict: Verdict,
) -> ReleaseEvidence:
    return _normalize_cost_for_target(
        record,
        assessment.project_id,
        assessment.to_hardware_revision_id,
        assessment.to_snapshot_hash,
        assessment.analysis_hash,
        verdict,
    )


def _normalize_cost_for_target(
    record: StoredCostEvaluation,
    project_id: str,
    hardware_revision_id: str,
    snapshot_hash: str,
    change_analysis_hash: str,
    verdict: Verdict,
) -> ReleaseEvidence:
    return ReleaseEvidence(
        evidence_id=record.evidence_id,
        evidence_kind=ReleaseEvidenceKind.BOM_COST,
        project_id=project_id,
        hardware_revision_id=hardware_revision_id,
        snapshot_hash=snapshot_hash,
        change_analysis_hash=change_analysis_hash,
        test_id="bom-provenance-validation",
        tier=EvidenceTier.STATIC,
        verdict=verdict,
        source_system=SourceSystem.SUPPLIER,
        source_revision=record.revision_id,
        source_hash=canonical_sha256(record),
        result_ref=f"forge://cost-evaluations/{record.evidence_id}",
        recorded_at=record.evaluated_at,
    )


def _choose_latest(
    items: Sequence[ReleaseEvidence], rejections: dict[str, EvidenceRejectionReason]
) -> ReleaseEvidence | None:
    if not items:
        return None
    latest_at = max(item.recorded_at for item in items)
    latest = tuple(item for item in items if item.recorded_at == latest_at)
    older = tuple(item for item in items if item.recorded_at < latest_at)
    for item in older:
        rejections[item.evidence_id] = EvidenceRejectionReason.SUPERSEDED
    if len(latest) != 1:
        for item in latest:
            rejections[item.evidence_id] = (
                EvidenceRejectionReason.AMBIGUOUS_LATEST_TIMESTAMP
            )
        return None
    return latest[0]


def _resolve_retest(
    requirement: RetestRequirement, selected: ReleaseEvidence | None
) -> RetestRequirement:
    if selected is None or selected.verdict is Verdict.INDETERMINATE:
        status = RetestStatus.MISSING
        evidence_id = None
    elif selected.verdict is Verdict.FAIL:
        status = RetestStatus.FAILED
        evidence_id = None
    else:
        status = RetestStatus.PASSED
        evidence_id = selected.evidence_id
    return RetestRequirement(
        retest_id=requirement.retest_id,
        test_id=requirement.test_id,
        required_tier=requirement.required_tier,
        status=status,
        triggered_by=requirement.triggered_by,
        reason_codes=requirement.reason_codes,
        satisfied_by_evidence_id=evidence_id,
    )


def _blocker_codes(
    findings: tuple[ConsistencyFinding, ...],
    retests: tuple[RetestRequirement, ...],
) -> tuple[str, ...]:
    codes = {
        f"finding:{item.rule_id}"
        for item in findings
        if item.severity is FindingSeverity.BLOCKER
    }
    codes.update(
        f"retest:{item.test_id}:{item.required_tier.value}:{item.status.value}"
        for item in retests
        if item.status is not RetestStatus.PASSED
    )
    return tuple(sorted(codes))


def evaluate_release_readiness(
    assessment: ChangeImpactAssessment,
    target_snapshot: ConnectorSnapshot,
    *,
    cost_evaluation: StoredCostEvaluation | None,
    pre_rejected_cost_evidence: Sequence[EvidenceRejection] = (),
    firmware_builds: Sequence[FirmwareBuildEvidence],
    test_results: Sequence[TestExecutionEvidence],
    evaluated_at: datetime,
    policy: ReleaseReadinessPolicy = DEFAULT_RELEASE_READINESS_POLICY,
) -> ReleaseReadinessDecision:
    assessment = ChangeImpactAssessment.model_validate(
        assessment.model_dump(mode="python")
    )
    target_snapshot = ConnectorSnapshot.model_validate(
        target_snapshot.model_dump(mode="python")
    )
    policy = ReleaseReadinessPolicy.model_validate(policy.model_dump(mode="python"))
    if cost_evaluation is not None:
        cost_evaluation = StoredCostEvaluation.model_validate(
            cost_evaluation.model_dump(mode="python")
        )
    pre_rejected_cost_evidence = tuple(
        EvidenceRejection.model_validate(item.model_dump(mode="python"))
        for item in pre_rejected_cost_evidence
    )
    firmware_builds = tuple(
        FirmwareBuildEvidence.model_validate(item.model_dump(mode="python"))
        for item in firmware_builds
    )
    test_results = tuple(
        TestExecutionEvidence.model_validate(item.model_dump(mode="python"))
        for item in test_results
    )
    _require_utc(evaluated_at, "release evaluated_at")
    if target_snapshot.captured_at > evaluated_at:
        raise ValueError("release evaluation cannot predate its target snapshot")
    if connector_snapshot_hash(target_snapshot) != assessment.to_snapshot_hash:
        raise ValueError("target connector snapshot does not match change assessment")
    if (
        target_snapshot.project_id != assessment.project_id
        or target_snapshot.hardware_revision_id != assessment.to_hardware_revision_id
    ):
        raise ValueError("target snapshot identity does not match change assessment")
    all_ids = (
        ([cost_evaluation.evidence_id] if cost_evaluation is not None else [])
        + [item.evidence_id for item in pre_rejected_cost_evidence]
        + [build.evidence_id for build in firmware_builds]
        + [result.evidence_id for result in test_results]
    )
    if len(all_ids) != len(set(all_ids)):
        raise ValueError("collected release evidence IDs must be unique")

    requirements = _with_baselines(assessment, policy)
    requirement_keys = {(item.test_id, item.required_tier) for item in requirements}
    rejections: dict[str, EvidenceRejectionReason] = {
        item.evidence_id: item.reason for item in pre_rejected_cost_evidence
    }
    candidates: dict[tuple[str, EvidenceTier], list[ReleaseEvidence]] = {}
    findings = list(assessment.findings)
    if assessment.target_protocol_schema_hash is None:
        findings.append(
            _protocol_finding(
                "protocol_schema_evidence_missing",
                "No target protocol interface projection is bound to this change "
                "assessment.",
                f"change-analysis:{assessment.analysis_hash}",
            )
        )

    cost_record = cost_evaluation
    if cost_record is None:
        ambiguous_cost_ids = tuple(
            sorted(
                item.evidence_id
                for item in pre_rejected_cost_evidence
                if item.reason is EvidenceRejectionReason.AMBIGUOUS_LATEST_TIMESTAMP
            )
        )
        if ambiguous_cost_ids:
            findings.append(
                _cost_finding(
                    "bom_cost_evidence_ambiguous",
                    "Multiple BOM cost evaluations share the latest timestamp; "
                    "release must fail closed until one current source is selected.",
                    ",".join(ambiguous_cost_ids),
                )
            )
        else:
            findings.append(
                _cost_finding(
                    "bom_cost_evidence_missing",
                    "No source- and timestamp-bound BOM cost evaluation was provided.",
                    "cost-evaluation:missing",
                )
            )
    elif (
        cost_record.project_id != assessment.project_id
        or cost_record.revision_id != assessment.to_hardware_revision_id
    ):
        findings.append(
            _cost_finding(
                "bom_cost_evidence_target_mismatch",
                "BOM cost provenance targets a different project or revision.",
                cost_record.evidence_id,
            )
        )
        cost_record = None
    elif not cost_evidence_matches_policy(cost_record, policy):
        findings.append(
            _cost_finding(
                "bom_cost_policy_mismatch",
                "BOM cost evidence does not bind the approved release budget policy.",
                cost_record.evidence_id,
            )
        )
        rejections[cost_record.evidence_id] = (
            EvidenceRejectionReason.COST_POLICY_MISMATCH
        )
        cost_record = None
    elif cost_record.evaluated_at > evaluated_at:
        findings.append(
            _cost_finding(
                "bom_cost_evidence_from_future",
                "BOM cost evaluation was recorded after the release evaluation.",
                cost_record.evidence_id,
            )
        )
        cost_record = None
    elif cost_record.evaluated_at < evaluated_at - policy.max_age(EvidenceTier.STATIC):
        findings.append(
            _cost_finding(
                "bom_cost_evidence_stale",
                "BOM cost evaluation exceeds the release freshness policy.",
                cost_record.evidence_id,
            )
        )
        cost_record = None
    elif cost_record.evaluated_at < target_snapshot.captured_at:
        findings.append(
            _cost_finding(
                "bom_cost_evidence_before_snapshot",
                "BOM cost evaluation predates the target connector snapshot.",
                cost_record.evidence_id,
            )
        )
        cost_record = None
    else:
        current_bom_hashes = {
            item.content_hash
            for item in target_snapshot.artifacts
            if item.domain is ArtifactDomain.BOM
        }
        if cost_record.bom_artifact_hash not in current_bom_hashes:
            findings.append(
                _cost_finding(
                    "bom_cost_artifact_mismatch",
                    "BOM cost provenance does not match the current BOM artifact.",
                    cost_record.evidence_id,
                )
            )
            cost_record = None
        else:
            required_parts = {
                item.part_number for item in cost_record.bom if item.required
            }
            stale_quote_parts = tuple(
                sorted(
                    quote.part_number
                    for quote in cost_record.quotes
                    if quote.part_number in required_parts
                    and quote.observed_at
                    < evaluated_at - policy.max_age(EvidenceTier.STATIC)
                )
            )
            if stale_quote_parts:
                findings.append(
                    _cost_finding(
                        "bom_quote_observation_stale",
                        "Required BOM quote observations exceed the release freshness "
                        "policy: " + ", ".join(stale_quote_parts),
                        cost_record.evidence_id,
                    )
                )
                cost_record = None
        if cost_record is not None:
            current_cost = evaluate_cost(
                cost_record.bom,
                cost_record.quotes,
                currency=policy.cost_currency,
                budget_limit=policy.cost_budget_limit,
                reserve_rate=policy.cost_reserve_rate,
                evaluated_at=evaluated_at,
            )
            cost_evidence = _normalize_cost(
                cost_record, assessment, current_cost.verdict
            )
            candidates.setdefault(
                (cost_evidence.test_id, cost_evidence.tier), []
            ).append(cost_evidence)
            if current_cost.verdict is Verdict.FAIL:
                findings.append(
                    _cost_finding(
                        "bom_cost_budget_failed",
                        "Current BOM cost exceeds the release budget.",
                        cost_record.evidence_id,
                    )
                )
            elif current_cost.verdict is Verdict.INDETERMINATE:
                findings.append(
                    _cost_finding(
                        "bom_cost_indeterminate",
                        "Current BOM cost is indeterminate: "
                        + ", ".join(current_cost.reasons),
                        cost_record.evidence_id,
                    )
                )

    firmware_refs = {
        (item.artifact_id, item.source_revision, item.content_hash)
        for item in target_snapshot.artifacts
        if item.domain is ArtifactDomain.FIRMWARE
        and item.source_system is SourceSystem.GIT
    }
    valid_builds: list[ReleaseEvidence] = []
    build_by_evidence_id: dict[str, FirmwareBuildEvidence] = {}
    test_by_evidence_id: dict[str, TestExecutionEvidence] = {}
    for item in firmware_builds:
        reason = _target_rejection_reason(item, assessment)
        if reason is None and item.completed_at > evaluated_at:
            reason = EvidenceRejectionReason.FUTURE_TIMESTAMP
        if reason is None and item.completed_at < target_snapshot.captured_at:
            reason = EvidenceRejectionReason.BEFORE_TARGET_SNAPSHOT
        if reason is None and item.completed_at < evaluated_at - policy.max_age(
            EvidenceTier.STATIC
        ):
            reason = EvidenceRejectionReason.STALE
        source_key = (
            item.firmware_artifact_id,
            item.firmware_source_revision,
            item.firmware_source_hash,
        )
        if reason is None and source_key not in firmware_refs:
            reason = EvidenceRejectionReason.FIRMWARE_SOURCE_MISMATCH
        if reason is None and assessment.target_protocol_schema_hash is None:
            reason = EvidenceRejectionReason.PROTOCOL_SCHEMA_TARGET_MISSING
        if (
            reason is None
            and item.protocol_schema_hash != assessment.target_protocol_schema_hash
        ):
            reason = EvidenceRejectionReason.PROTOCOL_SCHEMA_MISMATCH
        if reason is not None:
            rejections[item.evidence_id] = reason
            continue
        normalized = _normalize_build(item)
        valid_builds.append(normalized)
        build_by_evidence_id[item.evidence_id] = item

    selected_build = _choose_latest(valid_builds, rejections)
    selected_build_artifact_hash: str | None = None
    if selected_build is not None:
        candidates.setdefault(("firmware-build", EvidenceTier.STATIC), []).append(
            selected_build
        )
        if selected_build.verdict is Verdict.PASS:
            selected_build_artifact_hash = build_by_evidence_id[
                selected_build.evidence_id
            ].firmware_build_artifact_hash

    for test_result in test_results:
        reason = _target_rejection_reason(test_result, assessment)
        if reason is None and test_result.recorded_at > evaluated_at:
            reason = EvidenceRejectionReason.FUTURE_TIMESTAMP
        if reason is None and test_result.recorded_at < target_snapshot.captured_at:
            reason = EvidenceRejectionReason.BEFORE_TARGET_SNAPSHOT
        if reason is None and test_result.recorded_at < evaluated_at - policy.max_age(
            test_result.tier
        ):
            reason = EvidenceRejectionReason.STALE
        key = (test_result.test_id, test_result.tier)
        if reason is None and key not in requirement_keys:
            reason = EvidenceRejectionReason.NOT_REQUIRED
        requires_build = (
            test_result.test_id in policy.runtime_test_ids_requiring_build
            or test_result.tier
            in {
                EvidenceTier.SIMULATION,
                EvidenceTier.HIL,
                EvidenceTier.PHYSICAL_DEVICE,
            }
        )
        if reason is None and requires_build and selected_build_artifact_hash is None:
            reason = EvidenceRejectionReason.FIRMWARE_BUILD_NOT_PASSED
        if (
            reason is None
            and requires_build
            and test_result.firmware_build_artifact_hash != selected_build_artifact_hash
        ):
            reason = EvidenceRejectionReason.FIRMWARE_ARTIFACT_MISMATCH
        if reason is not None:
            rejections[test_result.evidence_id] = reason
            continue
        candidates.setdefault(key, []).append(_normalize_test(test_result))
        test_by_evidence_id[test_result.evidence_id] = test_result

    selected_by_key: dict[tuple[str, EvidenceTier], ReleaseEvidence] = {}
    for key in sorted(requirement_keys, key=lambda item: (item[0], item[1].value)):
        selected = _choose_latest(candidates.get(key, ()), rejections)
        if selected is not None:
            selected_by_key[key] = selected

    resolved = tuple(
        _resolve_retest(item, selected_by_key.get((item.test_id, item.required_tier)))
        for item in requirements
    )
    selected_evidence = tuple(
        sorted(selected_by_key.values(), key=lambda item: item.evidence_id)
    )
    selected_build_record = (
        build_by_evidence_id.get(selected_build.evidence_id)
        if selected_build is not None
        and selected_build.evidence_id
        in {item.evidence_id for item in selected_evidence}
        else None
    )
    selected_test_records = tuple(
        sorted(
            (
                test_by_evidence_id[item.evidence_id]
                for item in selected_evidence
                if item.evidence_kind is ReleaseEvidenceKind.TEST_RESULT
            ),
            key=lambda item: item.evidence_id,
        )
    )
    ordered_findings = tuple(sorted(findings, key=lambda item: item.finding_id))
    blockers = _blocker_codes(ordered_findings, resolved)
    status = ReleaseStatus.BLOCKED if blockers else ReleaseStatus.READY
    report_identity = _ReportIdentity(
        project_id=assessment.project_id,
        hardware_revision_id=assessment.to_hardware_revision_id,
        snapshot_hash=assessment.to_snapshot_hash,
        change_analysis_hash=assessment.analysis_hash,
        evaluated_at=evaluated_at,
        findings=ordered_findings,
        required_retests=resolved,
        evidence=selected_evidence,
    )
    report = ReleaseReadinessReport(
        report_id=f"release:{canonical_sha256(report_identity)[7:31]}",
        project_id=assessment.project_id,
        hardware_revision_id=assessment.to_hardware_revision_id,
        snapshot_hash=assessment.to_snapshot_hash,
        change_analysis_hash=assessment.analysis_hash,
        evaluated_at=evaluated_at,
        status=status,
        findings=ordered_findings,
        required_retests=resolved,
        evidence=selected_evidence,
        blocker_codes=blockers,
    )
    rejected = tuple(
        EvidenceRejection(evidence_id=evidence_id, reason=reason)
        for evidence_id, reason in sorted(rejections.items())
    )
    policy_hash = canonical_sha256(policy)
    selected_ids = tuple(item.evidence_id for item in selected_evidence)
    cost_hash = canonical_sha256(cost_record) if cost_record is not None else None
    human = render_release_report(
        report,
        cost_record,
        rejected,
        selected_build_record,
        selected_test_records,
        target_protocol_schema_hash=assessment.target_protocol_schema_hash,
    )
    payload = _DecisionPayload(
        policy=policy,
        policy_hash=policy_hash,
        change_assessment=assessment,
        target_snapshot=target_snapshot,
        report=report,
        cost_evaluation_hash=cost_hash,
        selected_firmware_build=selected_build_record,
        selected_test_results=selected_test_records,
        selected_evidence_ids=selected_ids,
        rejected_evidence=rejected,
    )
    return ReleaseReadinessDecision(
        policy=policy,
        policy_hash=policy_hash,
        change_assessment=assessment,
        target_snapshot=target_snapshot,
        report=report,
        cost_evaluation=cost_record,
        cost_evaluation_hash=cost_hash,
        selected_firmware_build=selected_build_record,
        selected_test_results=selected_test_records,
        selected_evidence_ids=selected_ids,
        rejected_evidence=rejected,
        human_readable_report=human,
        decision_hash=canonical_sha256(payload),
    )


def _safe_text(value: object) -> str:
    cleaned = "".join(
        character if character >= " " or character in "\n\t" else "�"
        for character in str(value)
    )
    return html.escape(cleaned, quote=True).replace("|", "&#124;")


def render_release_report(
    report: ReleaseReadinessReport,
    cost_evaluation: StoredCostEvaluation | None,
    rejected_evidence: Sequence[EvidenceRejection],
    selected_firmware_build: FirmwareBuildEvidence | None = None,
    selected_test_results: Sequence[TestExecutionEvidence] = (),
    *,
    target_protocol_schema_hash: str | None = None,
) -> str:
    lines = [
        "# FORGE Release Readiness",
        "",
        f"- Decision: `{report.status.value.upper()}`",
        f"- Project: `{_safe_text(report.project_id)}`",
        f"- Hardware revision: `{_safe_text(report.hardware_revision_id)}`",
        f"- Snapshot hash: `{report.snapshot_hash}`",
        f"- Change analysis hash: `{report.change_analysis_hash}`",
        f"- Target protocol schema: `{target_protocol_schema_hash or 'not-bound'}`",
        f"- Evaluated at: `{report.evaluated_at.isoformat()}`",
        "",
        "## Blockers",
        "",
    ]
    lines.extend(
        (f"- `{_safe_text(code)}`" for code in report.blocker_codes),
    )
    if not report.blocker_codes:
        lines.append("- None")
    lines.extend(("", "## Consistency findings", ""))
    for finding in report.findings:
        lines.append(
            f"- `{_safe_text(finding.rule_id)}` / `{finding.severity.value}`: "
            f"{_safe_text(finding.summary)}"
        )
    if not report.findings:
        lines.append("- None")
    lines.extend(("", "## Required retests", ""))
    for retest in report.required_retests:
        evidence_id = retest.satisfied_by_evidence_id or "none"
        lines.append(
            f"- `{_safe_text(retest.test_id)}` / `{retest.required_tier.value}`: "
            f"`{retest.status.value}` (evidence `{_safe_text(evidence_id)}`)"
        )
    lines.extend(("", "## Firmware build and test evidence", ""))
    for report_evidence in report.evidence:
        lines.append(
            f"- `{_safe_text(report_evidence.evidence_id)}` | "
            f"`{report_evidence.evidence_kind.value}` | "
            f"`{_safe_text(report_evidence.test_id)}` | "
            f"`{report_evidence.tier.value}` | `{report_evidence.verdict.value}` | "
            f"`{report_evidence.source_system.value}:"
            f"{_safe_text(report_evidence.source_revision)}` | "
            f"`{report_evidence.recorded_at.isoformat()}` | "
            f"`{report_evidence.source_hash}` | "
            f"`{_safe_text(report_evidence.result_ref)}`"
        )
    if not report.evidence:
        lines.append("- None")
    if selected_firmware_build is not None:
        build = selected_firmware_build
        lines.extend(
            (
                "",
                "### Selected firmware build binding",
                "",
                f"- Build `{_safe_text(build.build_id)}`; firmware artifact "
                f"`{_safe_text(build.firmware_artifact_id)}` at Git revision "
                f"`{_safe_text(build.firmware_source_revision)}` / "
                f"`{build.firmware_source_hash}`",
                f"- Toolchain `{_safe_text(build.toolchain_id)}` "
                f"`{_safe_text(build.toolchain_version)}` / `{build.toolchain_hash}`",
                f"- Produced firmware `{build.firmware_build_artifact_hash}`; "
                f"protocol `{build.protocol_schema_hash}`; completed "
                f"`{build.completed_at.isoformat()}`",
            )
        )
    if selected_test_results:
        lines.extend(("", "### Selected test execution bindings", ""))
        for result in selected_test_results:
            build_hash = result.firmware_build_artifact_hash or "not-applicable"
            lines.append(
                f"- `{_safe_text(result.evidence_id)}` / "
                f"`{_safe_text(result.test_id)}`: execution "
                f"`{_safe_text(result.source_revision)}` / `{result.source_hash}`; "
                f"firmware `{build_hash}`"
            )
    lines.extend(("", "## BOM price provenance", ""))
    if cost_evaluation is None:
        lines.append("- No valid current BOM cost provenance.")
    else:
        lines.append(
            f"- Stored evaluation `{_safe_text(cost_evaluation.evidence_id)}` at "
            f"`{cost_evaluation.evaluated_at.isoformat()}`; dependency "
            f"`{cost_evaluation.dependency_hash}`; BOM "
            f"`{cost_evaluation.bom_artifact_hash}`"
        )
        for quote in sorted(
            cost_evaluation.quotes, key=lambda item: (item.part_number, item.quote_id)
        ):
            lines.append(
                f"- `{_safe_text(quote.part_number)}` | "
                f"supplier `{_safe_text(quote.supplier)}` | "
                f"{quote.unit_price} {quote.currency} | MOQ {quote.minimum_quantity} | "
                f"observed `{quote.observed_at.isoformat()}` | "
                f"expires `{quote.expires_at.isoformat()}` | "
                f"source `{_safe_text(quote.source_url)}` | `{quote.source_hash}`"
            )
    lines.extend(("", "## Rejected or superseded evidence", ""))
    for rejection in rejected_evidence:
        lines.append(
            f"- `{_safe_text(rejection.evidence_id)}`: `{rejection.reason.value}`"
        )
    if not rejected_evidence:
        lines.append("- None")
    lines.extend(
        (
            "",
            "> READY is an evidence-based engineering release gate; it is not "
            "regulatory certification or production approval.",
            "",
        )
    )
    return "\n".join(lines)


__all__ = [
    "BaselineRetestRule",
    "DEFAULT_RELEASE_READINESS_POLICY",
    "EvidenceRejection",
    "EvidenceRejectionReason",
    "FirmwareBuildEvidence",
    "ReleaseReadinessDecision",
    "ReleaseReadinessPolicy",
    "TestExecutionEvidence",
    "cost_evidence_matches_policy",
    "evaluate_release_readiness",
    "render_release_report",
]
