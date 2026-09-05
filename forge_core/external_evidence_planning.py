from __future__ import annotations

import re
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from forge_core.change_management import (
    EvidenceTier,
    ExternalArtifactRef,
)
from forge_core.change_planning import AssetInput
from forge_core.hashing import canonical_sha256
from forge_core.models import ContractModel, Verdict
from forge_core.release_readiness import TestExecutionEvidence

EXTERNAL_EVIDENCE_TIERS = (
    EvidenceTier.SIMULATION,
    EvidenceTier.BENCH,
    EvidenceTier.HIL,
    EvidenceTier.PHYSICAL_DEVICE,
)
_SAFE_EXTERNAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SAFE_ADAPTER_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


class OperatingScenarioKind(StrEnum):
    CHANGE_IMPACT_PREVIEW = "change_impact_preview"
    PHYSICAL_SIMULATION = "physical_simulation"
    DYNAMIC_SIMULATION = "dynamic_simulation"
    BENCH_PROCEDURE = "bench_procedure"
    HIL_PROCEDURE = "hil_procedure"
    PHYSICAL_DEVICE_PROCEDURE = "physical_device_procedure"


class ExternalEvidenceActionKind(StrEnum):
    SIMULATE = "simulate"
    MEASURE = "measure"
    EXERCISE = "exercise"
    INSPECT = "inspect"


class ExternalEvidenceCheckResult(StrEnum):
    MATCHES_PLAN = "matches_plan"
    DIFFERS_FROM_PLAN = "differs_from_plan"


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


def _unique_sorted_strings(value: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if len(value) != len(set(value)):
        raise ValueError(f"{field_name} must be unique")
    ordered = tuple(sorted(value))
    if value != ordered:
        raise ValueError(f"{field_name} must be in canonical order")
    return value


def _source_ref_key(item: ExternalArtifactRef) -> tuple[str, str, str, str]:
    return (
        item.source_system.value,
        item.artifact_id,
        item.source_revision,
        item.content_hash,
    )


def _required_evidence_key(item: RequiredExternalEvidence) -> tuple[str, str, str]:
    return item.required_evidence_id, item.tier.value, item.test_id


def _evidence_binding_key(item: ExternalEvidenceImportBinding) -> tuple[str, str]:
    return item.required_evidence_id, item.evidence.evidence_id


def _external_tool_ref(value: str | None) -> str:
    if value is None:
        return "external-adapter"
    if _SAFE_ADAPTER_REF.fullmatch(value) is None:
        raise ValueError("external_tool_ref must be a safe adapter identifier")
    return value


class AcceptanceCriterion(ContractModel):
    criterion_id: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    operator: Literal["<", "<=", "==", ">=", ">", "exists", "schema_valid"]
    expected: str = Field(min_length=1)
    source_refs: tuple[str, ...] = Field(min_length=1)

    @field_validator("source_refs")
    @classmethod
    def source_refs_must_be_unique_and_canonical(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        return _unique_sorted_strings(value, "acceptance criterion source refs")


class ExternalEvidenceRequirementHint(ContractModel):
    test_id: str = Field(min_length=1)
    required_tier: EvidenceTier
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = Field(min_length=1)

    @field_validator("test_id")
    @classmethod
    def test_id_must_be_safe(cls, value: str) -> str:
        if _SAFE_EXTERNAL_ID.fullmatch(value) is None:
            raise ValueError("external evidence test_id must be opaque and safe")
        return value

    @model_validator(mode="after")
    def hint_must_target_external_tier(self) -> ExternalEvidenceRequirementHint:
        if self.required_tier not in EXTERNAL_EVIDENCE_TIERS:
            raise ValueError("requirement hint tier must be external")
        criterion_ids = [item.criterion_id for item in self.acceptance_criteria]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("requirement hint acceptance criteria must be unique")
        if self.acceptance_criteria != tuple(
            sorted(self.acceptance_criteria, key=lambda item: item.criterion_id)
        ):
            raise ValueError(
                "requirement hint acceptance criteria must be in canonical order"
            )
        return self


class OperatingScenario(ContractModel):
    """Source-bound operating or simulation scenario for external evidence planning."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    scenario_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    kind: OperatingScenarioKind
    asset_input: AssetInput
    baseline_snapshot_id: str = Field(min_length=1)
    baseline_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    scenario_text: str = Field(min_length=1)
    source_refs: tuple[ExternalArtifactRef, ...] = Field(default_factory=tuple)
    created_at: datetime
    scenario_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("created_at")
    @classmethod
    def created_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "created_at")

    @model_validator(mode="after")
    def scenario_must_be_source_bound_and_hashed(self) -> OperatingScenario:
        if self.asset_input.captured_at > self.created_at:
            raise ValueError("asset input cannot be newer than operating scenario")
        source_identities = [
            (item.source_system, item.artifact_id) for item in self.source_refs
        ]
        if len(source_identities) != len(set(source_identities)):
            raise ValueError("operating scenario source refs must be unique")
        if self.source_refs != tuple(sorted(self.source_refs, key=_source_ref_key)):
            raise ValueError(
                "operating scenario source refs must be in canonical order"
            )
        if any(item.captured_at > self.created_at for item in self.source_refs):
            raise ValueError("scenario source cannot be newer than operating scenario")
        expected = operating_scenario_hash(
            scenario_id=self.scenario_id,
            project_id=self.project_id,
            kind=self.kind,
            asset_input=self.asset_input,
            baseline_snapshot_id=self.baseline_snapshot_id,
            baseline_snapshot_hash=self.baseline_snapshot_hash,
            scenario_text=self.scenario_text,
            source_refs=self.source_refs,
            created_at=self.created_at,
        )
        if self.scenario_hash != expected:
            raise ValueError("operating scenario hash does not match its payload")
        return self


class RequiredExternalEvidence(ContractModel):
    """Planning-only request for evidence produced outside FORGE."""

    required_evidence_id: str = Field(min_length=1)
    tier: EvidenceTier
    action_kind: ExternalEvidenceActionKind
    external_adapter_id: str = Field(min_length=1)
    external_adapter_route: str = Field(min_length=1)
    test_id: str = Field(min_length=1)
    requested_action: str = Field(min_length=1)
    source_refs: tuple[ExternalArtifactRef, ...] = Field(min_length=1)
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = Field(min_length=1)
    missing_information: tuple[str, ...] = Field(default_factory=tuple)
    planning_only: Literal[True] = True

    @field_validator("required_evidence_id", "external_adapter_id", "test_id")
    @classmethod
    def identifiers_must_be_safe(cls, value: str) -> str:
        if _SAFE_EXTERNAL_ID.fullmatch(value) is None:
            raise ValueError("external evidence identifiers must be opaque and safe")
        return value

    @field_validator("missing_information")
    @classmethod
    def missing_information_must_be_unique_and_canonical(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        return _unique_sorted_strings(value, "required evidence missing information")

    @model_validator(mode="after")
    def evidence_request_must_be_external_canonical_plan(
        self,
    ) -> RequiredExternalEvidence:
        if self.tier not in EXTERNAL_EVIDENCE_TIERS:
            raise ValueError("required evidence tier must be external")
        expected_action = {
            EvidenceTier.SIMULATION: ExternalEvidenceActionKind.SIMULATE,
            EvidenceTier.BENCH: ExternalEvidenceActionKind.MEASURE,
            EvidenceTier.HIL: ExternalEvidenceActionKind.EXERCISE,
            EvidenceTier.PHYSICAL_DEVICE: ExternalEvidenceActionKind.EXERCISE,
        }[self.tier]
        if self.action_kind is not expected_action:
            raise ValueError("required evidence action must match its tier")
        expected_route = f"external://{self.tier.value}/{self.test_id}"
        if self.external_adapter_route != expected_route:
            raise ValueError("external adapter route must match tier and test")
        source_identities = [
            (item.source_system, item.artifact_id) for item in self.source_refs
        ]
        if len(source_identities) != len(set(source_identities)):
            raise ValueError("required evidence source refs must be unique")
        if self.source_refs != tuple(sorted(self.source_refs, key=_source_ref_key)):
            raise ValueError("required evidence source refs must be in canonical order")
        criterion_ids = [item.criterion_id for item in self.acceptance_criteria]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("acceptance criterion IDs must be unique")
        if self.acceptance_criteria != tuple(
            sorted(self.acceptance_criteria, key=lambda item: item.criterion_id)
        ):
            raise ValueError("acceptance criteria must be in canonical order")
        return self


class RequiredExternalEvidencePlan(ContractModel):
    """External evidence adapter plan; never a release gate or readiness decision."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    plan_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    operating_scenario: OperatingScenario
    generated_at: datetime
    required_evidence: tuple[RequiredExternalEvidence, ...] = Field(min_length=1)
    assumptions: tuple[str, ...] = Field(default_factory=tuple)
    missing_information: tuple[str, ...] = Field(default_factory=tuple)
    planning_only: Literal[True] = True
    plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("generated_at")
    @classmethod
    def generated_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "generated_at")

    @field_validator("assumptions", "missing_information")
    @classmethod
    def plan_strings_must_be_unique_and_canonical(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        return _unique_sorted_strings(value, "external evidence plan strings")

    @model_validator(mode="after")
    def evidence_plan_must_be_complete_canonical_and_hashed(
        self,
    ) -> RequiredExternalEvidencePlan:
        if self.project_id != self.operating_scenario.project_id:
            raise ValueError("external evidence plan project_id must match scenario")
        if self.operating_scenario.created_at > self.generated_at:
            raise ValueError("operating scenario cannot be newer than evidence plan")
        evidence_ids = [item.required_evidence_id for item in self.required_evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("required evidence IDs must be unique")
        if self.required_evidence != tuple(
            sorted(self.required_evidence, key=_required_evidence_key)
        ):
            raise ValueError("required evidence must be in canonical order")
        tiers = {item.tier for item in self.required_evidence}
        missing_tiers = [
            tier.value for tier in EXTERNAL_EVIDENCE_TIERS if tier not in tiers
        ]
        if missing_tiers:
            raise ValueError(
                "external evidence plan must cover simulation, bench, HIL, "
                "and physical device tiers"
            )
        if any(
            source.captured_at > self.generated_at
            for item in self.required_evidence
            for source in item.source_refs
        ):
            raise ValueError("required evidence source cannot be newer than plan")
        if any(item.missing_information for item in self.required_evidence):
            missing_refs = {
                f"required_evidence:{item.required_evidence_id}"
                for item in self.required_evidence
                if item.missing_information
            }
            if not missing_refs.issubset(self.missing_information):
                raise ValueError(
                    "plan missing information must reference incomplete evidence"
                )
        expected = required_external_evidence_plan_hash(
            plan_id=self.plan_id,
            project_id=self.project_id,
            operating_scenario=self.operating_scenario,
            generated_at=self.generated_at,
            required_evidence=self.required_evidence,
            assumptions=self.assumptions,
            missing_information=self.missing_information,
        )
        if self.plan_hash != expected:
            raise ValueError("external evidence plan hash does not match its payload")
        return self


class ExternalEvidenceImportBinding(ContractModel):
    """Binds one planned external evidence request to existing test evidence."""

    required_evidence_id: str = Field(min_length=1)
    evidence: TestExecutionEvidence
    evidence_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def evidence_hash_must_bind_exact_test_evidence(
        self,
    ) -> ExternalEvidenceImportBinding:
        expected = canonical_sha256(self.evidence)
        if self.evidence_hash != expected:
            raise ValueError("test evidence hash does not match imported evidence")
        return self


class ExternalEvidencePlanVerification(ContractModel):
    """Compares a stored external evidence plan with imported test evidence."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    verification_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    plan: RequiredExternalEvidencePlan
    stored_plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    actual_change_analysis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    verified_at: datetime
    check_result: ExternalEvidenceCheckResult
    imported_evidence: tuple[ExternalEvidenceImportBinding, ...]
    imported_evidence_hashes: tuple[str, ...] = Field(default_factory=tuple)
    missing_required_evidence_ids: tuple[str, ...] = Field(default_factory=tuple)
    unexpected_imported_evidence_ids: tuple[str, ...] = Field(default_factory=tuple)
    failed_required_evidence_ids: tuple[str, ...] = Field(default_factory=tuple)
    verification_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("verified_at")
    @classmethod
    def verified_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "verified_at")

    @field_validator(
        "imported_evidence_hashes",
        "missing_required_evidence_ids",
        "unexpected_imported_evidence_ids",
        "failed_required_evidence_ids",
    )
    @classmethod
    def verification_ids_must_be_unique_and_canonical(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        return _unique_sorted_strings(value, "external evidence verification IDs")

    @model_validator(mode="after")
    def verification_must_bind_plan_and_imports(
        self,
    ) -> ExternalEvidencePlanVerification:
        if self.project_id != self.plan.project_id:
            raise ValueError("verification project_id must match plan")
        if self.stored_plan_hash != self.plan.plan_hash:
            raise ValueError("stored plan hash must match plan hash")
        if self.plan.generated_at > self.verified_at:
            raise ValueError("external evidence plan cannot be generated after check")
        binding_keys = [
            (item.required_evidence_id, item.evidence.evidence_id)
            for item in self.imported_evidence
        ]
        if len(binding_keys) != len(set(binding_keys)):
            raise ValueError("imported evidence bindings must be unique")
        if self.imported_evidence != tuple(
            sorted(self.imported_evidence, key=_evidence_binding_key)
        ):
            raise ValueError("imported evidence bindings must be in canonical order")
        expected_import_hashes = tuple(
            sorted(item.evidence_hash for item in self.imported_evidence)
        )
        if self.imported_evidence_hashes != expected_import_hashes:
            raise ValueError("imported evidence hashes must match imported records")

        required_by_id = {
            item.required_evidence_id: item for item in self.plan.required_evidence
        }
        unexpected = tuple(
            sorted(
                item.evidence.evidence_id
                for item in self.imported_evidence
                if item.required_evidence_id not in required_by_id
            )
        )
        in_scope = [
            item
            for item in self.imported_evidence
            if item.required_evidence_id in required_by_id
        ]
        imported_by_required = {item.required_evidence_id: item for item in in_scope}
        if len(imported_by_required) != len(in_scope):
            raise ValueError(
                "only one imported result is allowed per required evidence"
            )
        missing = tuple(sorted(set(required_by_id).difference(imported_by_required)))
        failed = tuple(
            sorted(
                item.required_evidence_id
                for item in imported_by_required.values()
                if item.evidence.verdict is not Verdict.PASS
            )
        )
        for binding in imported_by_required.values():
            required = required_by_id[binding.required_evidence_id]
            evidence = binding.evidence
            if evidence.project_id != self.project_id:
                raise ValueError("imported evidence project_id must match plan")
            if evidence.change_analysis_hash != self.actual_change_analysis_hash:
                raise ValueError(
                    "imported evidence change analysis must match verification"
                )
            if (
                evidence.tier is not required.tier
                or evidence.test_id != required.test_id
            ):
                raise ValueError("imported evidence must match required tier and test")
            if evidence.recorded_at > self.verified_at:
                raise ValueError("imported evidence cannot be newer than verification")

        if self.missing_required_evidence_ids != missing:
            raise ValueError(
                "missing required evidence IDs must match imported results"
            )
        if self.unexpected_imported_evidence_ids != unexpected:
            raise ValueError("unexpected imported evidence IDs must match imports")
        if self.failed_required_evidence_ids != failed:
            raise ValueError("failed required evidence IDs must match imported results")
        expected_result = (
            ExternalEvidenceCheckResult.MATCHES_PLAN
            if not missing and not unexpected and not failed
            else ExternalEvidenceCheckResult.DIFFERS_FROM_PLAN
        )
        if self.check_result is not expected_result:
            raise ValueError("check result must follow plan/import comparison")
        expected = external_evidence_plan_verification_hash(
            verification_id=self.verification_id,
            project_id=self.project_id,
            plan=self.plan,
            stored_plan_hash=self.stored_plan_hash,
            actual_change_analysis_hash=self.actual_change_analysis_hash,
            verified_at=self.verified_at,
            check_result=self.check_result,
            imported_evidence=self.imported_evidence,
            imported_evidence_hashes=self.imported_evidence_hashes,
            missing_required_evidence_ids=self.missing_required_evidence_ids,
            unexpected_imported_evidence_ids=self.unexpected_imported_evidence_ids,
            failed_required_evidence_ids=self.failed_required_evidence_ids,
        )
        if self.verification_hash != expected:
            raise ValueError(
                "external evidence plan verification hash does not match its payload"
            )
        return self


class _OperatingScenarioPayload(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    scenario_id: str
    project_id: str
    kind: OperatingScenarioKind
    asset_input: AssetInput
    baseline_snapshot_id: str
    baseline_snapshot_hash: str
    scenario_text: str
    source_refs: tuple[ExternalArtifactRef, ...]
    created_at: datetime


class _RequiredExternalEvidencePlanPayload(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    plan_id: str
    project_id: str
    operating_scenario: OperatingScenario
    generated_at: datetime
    required_evidence: tuple[RequiredExternalEvidence, ...]
    assumptions: tuple[str, ...]
    missing_information: tuple[str, ...]


class _ExternalEvidencePlanVerificationPayload(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    verification_id: str
    project_id: str
    plan: RequiredExternalEvidencePlan
    stored_plan_hash: str
    actual_change_analysis_hash: str
    verified_at: datetime
    check_result: ExternalEvidenceCheckResult
    imported_evidence: tuple[ExternalEvidenceImportBinding, ...]
    imported_evidence_hashes: tuple[str, ...]
    missing_required_evidence_ids: tuple[str, ...]
    unexpected_imported_evidence_ids: tuple[str, ...]
    failed_required_evidence_ids: tuple[str, ...]


def operating_scenario_hash(
    *,
    scenario_id: str,
    project_id: str,
    kind: OperatingScenarioKind,
    asset_input: AssetInput,
    baseline_snapshot_id: str,
    baseline_snapshot_hash: str,
    scenario_text: str,
    source_refs: tuple[ExternalArtifactRef, ...] = (),
    created_at: datetime,
) -> str:
    return canonical_sha256(
        _OperatingScenarioPayload(
            scenario_id=scenario_id,
            project_id=project_id,
            kind=kind,
            asset_input=asset_input,
            baseline_snapshot_id=baseline_snapshot_id,
            baseline_snapshot_hash=baseline_snapshot_hash,
            scenario_text=scenario_text,
            source_refs=source_refs,
            created_at=created_at,
        )
    )


def build_required_external_evidence_plan(
    scenario: OperatingScenario,
    baseline_artifacts: tuple[ExternalArtifactRef, ...],
    *,
    generated_at: datetime,
    plan_id: str | None = None,
    external_tool_ref: str | None = None,
    required_evidence_hints: tuple[ExternalEvidenceRequirementHint, ...] = (),
) -> RequiredExternalEvidencePlan:
    """Build a deterministic external adapter evidence plan without executing it."""

    if not baseline_artifacts:
        raise ValueError("baseline artifacts are required for external evidence plan")
    adapter_ref = _external_tool_ref(external_tool_ref)
    hint_by_tier = {item.required_tier: item for item in required_evidence_hints}
    if len(hint_by_tier) != len(required_evidence_hints):
        raise ValueError("only one external evidence hint is allowed per tier")
    ordered_artifacts = tuple(sorted(baseline_artifacts, key=_source_ref_key))
    criteria = (
        AcceptanceCriterion(
            criterion_id="candidate-interface-within-planned-bounds",
            metric="pin-voltage-unit-range-protocol-schema",
            operator="schema_valid",
            expected="external result matches the source-bound operating scenario",
            source_refs=tuple(
                sorted(
                    (
                        f"operating_scenario:{scenario.scenario_hash}",
                        *(
                            f"artifact:{item.content_hash}"
                            for item in ordered_artifacts
                        ),
                    )
                )
            ),
        ),
    )
    simulation = hint_by_tier.get(EvidenceTier.SIMULATION)
    bench = hint_by_tier.get(EvidenceTier.BENCH)
    hil = hint_by_tier.get(EvidenceTier.HIL)
    physical = hint_by_tier.get(EvidenceTier.PHYSICAL_DEVICE)
    required = (
        RequiredExternalEvidence(
            required_evidence_id="req-bench",
            tier=EvidenceTier.BENCH,
            action_kind=ExternalEvidenceActionKind.MEASURE,
            external_adapter_id=f"{adapter_ref}:bench",
            external_adapter_route=(
                f"external://bench/{bench.test_id if bench else 'bench-electrical'}"
            ),
            test_id=bench.test_id if bench else "bench-electrical",
            requested_action="measure electrical behavior on a bench fixture",
            source_refs=ordered_artifacts,
            acceptance_criteria=bench.acceptance_criteria if bench else criteria,
        ),
        RequiredExternalEvidence(
            required_evidence_id="req-hil",
            tier=EvidenceTier.HIL,
            action_kind=ExternalEvidenceActionKind.EXERCISE,
            external_adapter_id=f"{adapter_ref}:hil",
            external_adapter_route=(
                f"external://hil/{hil.test_id if hil else 'hil-regression'}"
            ),
            test_id=hil.test_id if hil else "hil-regression",
            requested_action="exercise controller behavior on a HIL rig",
            source_refs=ordered_artifacts,
            acceptance_criteria=hil.acceptance_criteria if hil else criteria,
        ),
        RequiredExternalEvidence(
            required_evidence_id="req-physical-device",
            tier=EvidenceTier.PHYSICAL_DEVICE,
            action_kind=ExternalEvidenceActionKind.EXERCISE,
            external_adapter_id=f"{adapter_ref}:physical_device",
            external_adapter_route=(
                "external://physical_device/"
                f"{physical.test_id if physical else 'physical-device-smoke'}"
            ),
            test_id=physical.test_id if physical else "physical-device-smoke",
            requested_action="exercise the changed function on a physical device",
            source_refs=ordered_artifacts,
            acceptance_criteria=physical.acceptance_criteria if physical else criteria,
        ),
        RequiredExternalEvidence(
            required_evidence_id="req-simulation",
            tier=EvidenceTier.SIMULATION,
            action_kind=ExternalEvidenceActionKind.SIMULATE,
            external_adapter_id=f"{adapter_ref}:simulation",
            external_adapter_route=(
                "external://simulation/"
                f"{simulation.test_id if simulation else 'dynamics-simulation'}"
            ),
            test_id=simulation.test_id if simulation else "dynamics-simulation",
            requested_action="simulate the requested operating scenario externally",
            source_refs=ordered_artifacts,
            acceptance_criteria=simulation.acceptance_criteria
            if simulation
            else criteria,
        ),
    )
    resolved_plan_id = plan_id or f"external-plan:{scenario.scenario_hash}"
    assumptions = ("external-adapters-are-read-only",)
    return RequiredExternalEvidencePlan(
        plan_id=resolved_plan_id,
        project_id=scenario.project_id,
        operating_scenario=scenario,
        generated_at=generated_at,
        required_evidence=required,
        assumptions=assumptions,
        missing_information=(),
        plan_hash=required_external_evidence_plan_hash(
            plan_id=resolved_plan_id,
            project_id=scenario.project_id,
            operating_scenario=scenario,
            generated_at=generated_at,
            required_evidence=required,
            assumptions=assumptions,
            missing_information=(),
        ),
    )


def required_external_evidence_plan_hash(
    *,
    plan_id: str,
    project_id: str,
    operating_scenario: OperatingScenario,
    generated_at: datetime,
    required_evidence: tuple[RequiredExternalEvidence, ...],
    assumptions: tuple[str, ...] = (),
    missing_information: tuple[str, ...] = (),
) -> str:
    return canonical_sha256(
        _RequiredExternalEvidencePlanPayload(
            plan_id=plan_id,
            project_id=project_id,
            operating_scenario=operating_scenario,
            generated_at=generated_at,
            required_evidence=required_evidence,
            assumptions=assumptions,
            missing_information=missing_information,
        )
    )


def external_evidence_plan_verification_hash(
    *,
    verification_id: str,
    project_id: str,
    plan: RequiredExternalEvidencePlan,
    stored_plan_hash: str,
    actual_change_analysis_hash: str,
    verified_at: datetime,
    check_result: ExternalEvidenceCheckResult,
    imported_evidence: tuple[ExternalEvidenceImportBinding, ...] = (),
    imported_evidence_hashes: tuple[str, ...] = (),
    missing_required_evidence_ids: tuple[str, ...] = (),
    unexpected_imported_evidence_ids: tuple[str, ...] = (),
    failed_required_evidence_ids: tuple[str, ...] = (),
) -> str:
    return canonical_sha256(
        _ExternalEvidencePlanVerificationPayload(
            verification_id=verification_id,
            project_id=project_id,
            plan=plan,
            stored_plan_hash=stored_plan_hash,
            actual_change_analysis_hash=actual_change_analysis_hash,
            verified_at=verified_at,
            check_result=check_result,
            imported_evidence=imported_evidence,
            imported_evidence_hashes=imported_evidence_hashes,
            missing_required_evidence_ids=missing_required_evidence_ids,
            unexpected_imported_evidence_ids=unexpected_imported_evidence_ids,
            failed_required_evidence_ids=failed_required_evidence_ids,
        )
    )


def verify_external_evidence_plan(
    plan: RequiredExternalEvidencePlan,
    imported_evidence: tuple[ExternalEvidenceImportBinding, ...],
    *,
    actual_change_analysis_hash: str,
    verified_at: datetime,
    verification_id: str | None = None,
) -> ExternalEvidencePlanVerification:
    """Compare imported test evidence with a plan without making release decisions."""

    ordered_imports = tuple(sorted(imported_evidence, key=_evidence_binding_key))
    required_ids = {item.required_evidence_id for item in plan.required_evidence}
    in_scope = {
        item.required_evidence_id: item
        for item in ordered_imports
        if item.required_evidence_id in required_ids
    }
    missing = tuple(sorted(required_ids.difference(in_scope)))
    unexpected = tuple(
        sorted(
            item.evidence.evidence_id
            for item in ordered_imports
            if item.required_evidence_id not in required_ids
        )
    )
    failed = tuple(
        sorted(
            item.required_evidence_id
            for item in in_scope.values()
            if item.evidence.verdict is not Verdict.PASS
        )
    )
    imported_hashes = tuple(sorted(item.evidence_hash for item in ordered_imports))
    check_result = (
        ExternalEvidenceCheckResult.MATCHES_PLAN
        if not missing and not unexpected and not failed
        else ExternalEvidenceCheckResult.DIFFERS_FROM_PLAN
    )
    resolved_id = verification_id or f"external-verification:{plan.plan_hash}"
    verification_hash = external_evidence_plan_verification_hash(
        verification_id=resolved_id,
        project_id=plan.project_id,
        plan=plan,
        stored_plan_hash=plan.plan_hash,
        actual_change_analysis_hash=actual_change_analysis_hash,
        verified_at=verified_at,
        check_result=check_result,
        imported_evidence=ordered_imports,
        imported_evidence_hashes=imported_hashes,
        missing_required_evidence_ids=missing,
        unexpected_imported_evidence_ids=unexpected,
        failed_required_evidence_ids=failed,
    )
    return ExternalEvidencePlanVerification(
        verification_id=resolved_id,
        project_id=plan.project_id,
        plan=plan,
        stored_plan_hash=plan.plan_hash,
        actual_change_analysis_hash=actual_change_analysis_hash,
        verified_at=verified_at,
        check_result=check_result,
        imported_evidence=ordered_imports,
        imported_evidence_hashes=imported_hashes,
        missing_required_evidence_ids=missing,
        unexpected_imported_evidence_ids=unexpected,
        failed_required_evidence_ids=failed,
        verification_hash=verification_hash,
    )


__all__ = [
    "EXTERNAL_EVIDENCE_TIERS",
    "AcceptanceCriterion",
    "ExternalEvidenceActionKind",
    "ExternalEvidenceCheckResult",
    "ExternalEvidenceImportBinding",
    "ExternalEvidencePlanVerification",
    "ExternalEvidenceRequirementHint",
    "OperatingScenario",
    "OperatingScenarioKind",
    "RequiredExternalEvidence",
    "RequiredExternalEvidencePlan",
    "build_required_external_evidence_plan",
    "external_evidence_plan_verification_hash",
    "operating_scenario_hash",
    "required_external_evidence_plan_hash",
    "verify_external_evidence_plan",
]
