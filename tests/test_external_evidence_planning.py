from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from pydantic import ValidationError

from forge_core.change_management import (
    ArtifactDomain,
    EvidenceTier,
    ExternalArtifactRef,
    SourceSystem,
)
from forge_core.change_planning import AssetInput, AssetInputKind
from forge_core.external_evidence_planning import (
    EXTERNAL_EVIDENCE_TIERS,
    AcceptanceCriterion,
    ExternalEvidenceActionKind,
    ExternalEvidenceCheckResult,
    ExternalEvidenceImportBinding,
    ExternalEvidencePlanVerification,
    ExternalEvidenceRequirementHint,
    OperatingScenario,
    OperatingScenarioKind,
    RequiredExternalEvidence,
    RequiredExternalEvidencePlan,
    build_required_external_evidence_plan,
    external_evidence_plan_verification_hash,
    operating_scenario_hash,
    required_external_evidence_plan_hash,
    verify_external_evidence_plan,
)
from forge_core.hashing import canonical_sha256
from forge_core.models import Verdict
from forge_core.release_readiness import TestExecutionEvidence

NOW = datetime(2026, 8, 31, tzinfo=UTC)


def sha(seed: int) -> str:
    return f"sha256:{seed:064x}"


def source_ref(
    seed: int,
    *,
    artifact_id: str = "controller-board",
    source_system: SourceSystem = SourceSystem.PLM,
    domain: ArtifactDomain = ArtifactDomain.HARDWARE,
    captured_at: datetime = NOW,
) -> ExternalArtifactRef:
    return ExternalArtifactRef(
        artifact_id=artifact_id,
        domain=domain,
        source_system=source_system,
        source_revision=f"rev-{seed}",
        content_hash=sha(seed),
        captured_at=captured_at,
    )


def asset_input(*, captured_at: datetime = NOW) -> AssetInput:
    return AssetInput(
        asset_id="mobile-base-alpha",
        kind=AssetInputKind.PLM_SNAPSHOT,
        source_system=SourceSystem.PLM,
        source_locator="plm://robot-controller/mobile-base-alpha",
        source_revision="HW-12",
        content_hash=sha(12),
        captured_at=captured_at,
    )


def operating_scenario(
    *,
    created_at: datetime = NOW,
    source_refs: tuple[ExternalArtifactRef, ...] | None = None,
) -> OperatingScenario:
    values: dict[str, Any] = {
        "scenario_id": "scenario-physical-slope",
        "project_id": "project-1",
        "kind": OperatingScenarioKind.DYNAMIC_SIMULATION,
        "asset_input": asset_input(),
        "baseline_snapshot_id": "snapshot-11",
        "baseline_snapshot_hash": sha(11),
        "scenario_text": "replace motor driver and simulate 12 degree slope start",
        "source_refs": source_refs
        or (
            source_ref(2, artifact_id="firmware", source_system=SourceSystem.GIT),
            source_ref(1, artifact_id="controller-board"),
        ),
        "created_at": created_at,
    }
    return OperatingScenario(
        **values,
        scenario_hash=operating_scenario_hash(**values),
    )


def operating_scenario_hash_args(
    scenario: OperatingScenario,
    **updates: Any,
) -> dict[str, Any]:
    fields = {
        "scenario_id",
        "project_id",
        "kind",
        "asset_input",
        "baseline_snapshot_id",
        "baseline_snapshot_hash",
        "scenario_text",
        "source_refs",
        "created_at",
    }
    values = {
        key: value
        for key, value in scenario.model_dump(mode="python").items()
        if key in fields
    }
    values.update(updates)
    return values


def criterion(criterion_id: str = "criterion-pinmap") -> AcceptanceCriterion:
    return AcceptanceCriterion(
        criterion_id=criterion_id,
        metric="pin-voltage-protocol-consistency",
        operator="schema_valid",
        expected="external result matches the source-bound operating scenario",
        source_refs=("artifact:controller-board", "scenario:dynamic-simulation"),
    )


def required_evidence(
    evidence_id: str,
    tier: EvidenceTier,
    action: ExternalEvidenceActionKind,
    *,
    test_id: str,
    missing_information: tuple[str, ...] = (),
) -> RequiredExternalEvidence:
    return RequiredExternalEvidence(
        required_evidence_id=evidence_id,
        tier=tier,
        action_kind=action,
        external_adapter_id=f"{tier.value}-adapter",
        external_adapter_route=f"external://{tier.value}/{test_id}",
        test_id=test_id,
        requested_action=f"collect {tier.value} result from external adapter",
        source_refs=(
            source_ref(2, artifact_id="firmware", source_system=SourceSystem.GIT),
            source_ref(1, artifact_id="controller-board"),
        ),
        acceptance_criteria=(criterion(),),
        missing_information=missing_information,
    )


def required_set() -> tuple[RequiredExternalEvidence, ...]:
    return (
        required_evidence(
            "req-bench",
            EvidenceTier.BENCH,
            ExternalEvidenceActionKind.MEASURE,
            test_id="bench-electrical",
        ),
        required_evidence(
            "req-hil",
            EvidenceTier.HIL,
            ExternalEvidenceActionKind.EXERCISE,
            test_id="hil-regression",
        ),
        required_evidence(
            "req-physical-device",
            EvidenceTier.PHYSICAL_DEVICE,
            ExternalEvidenceActionKind.EXERCISE,
            test_id="physical-device-smoke",
        ),
        required_evidence(
            "req-simulation",
            EvidenceTier.SIMULATION,
            ExternalEvidenceActionKind.SIMULATE,
            test_id="dynamics-simulation",
        ),
    )


def plan(
    *,
    required: tuple[RequiredExternalEvidence, ...] | None = None,
    generated_at: datetime = NOW,
    missing_information: tuple[str, ...] = (),
) -> RequiredExternalEvidencePlan:
    values: dict[str, Any] = {
        "plan_id": "external-plan-1",
        "project_id": "project-1",
        "operating_scenario": operating_scenario(),
        "generated_at": generated_at,
        "required_evidence": required or required_set(),
        "assumptions": ("external-adapters-are-read-only",),
        "missing_information": missing_information,
    }
    return RequiredExternalEvidencePlan(
        **values,
        plan_hash=required_external_evidence_plan_hash(**values),
    )


def test_evidence(
    evidence_id: str,
    tier: EvidenceTier,
    *,
    test_id: str,
    verdict: Verdict = Verdict.PASS,
    seed: int = 50,
) -> TestExecutionEvidence:
    return TestExecutionEvidence(
        evidence_id=evidence_id,
        project_id="project-1",
        hardware_revision_id="HW-12-proposed",
        snapshot_hash=sha(21),
        change_analysis_hash=sha(22),
        test_id=test_id,
        tier=tier,
        verdict=verdict,
        source_system=SourceSystem.CI,
        source_revision=f"run-{seed}",
        source_hash=sha(seed),
        result_ref=f"ci://runs/{seed}",
        recorded_at=NOW,
        firmware_build_artifact_hash=sha(30),
        fixture_id="fixture-1"
        if tier in {EvidenceTier.BENCH, EvidenceTier.HIL}
        else None,
        device_instance_id="device-1" if tier is EvidenceTier.PHYSICAL_DEVICE else None,
    )


def binding(
    required_id: str, evidence: TestExecutionEvidence
) -> ExternalEvidenceImportBinding:
    return ExternalEvidenceImportBinding(
        required_evidence_id=required_id,
        evidence=evidence,
        evidence_hash=canonical_sha256(evidence),
    )


def bindings_for_plan(
    evidence_plan: RequiredExternalEvidencePlan,
    *,
    omit: set[str] | None = None,
    fail: set[str] | None = None,
    unexpected: bool = False,
) -> tuple[ExternalEvidenceImportBinding, ...]:
    omit = omit or set()
    fail = fail or set()
    results: list[ExternalEvidenceImportBinding] = []
    for index, item in enumerate(evidence_plan.required_evidence):
        if item.required_evidence_id in omit:
            continue
        results.append(
            binding(
                item.required_evidence_id,
                test_evidence(
                    f"evidence-{item.required_evidence_id}",
                    item.tier,
                    test_id=item.test_id,
                    verdict=(
                        Verdict.FAIL
                        if item.required_evidence_id in fail
                        else Verdict.PASS
                    ),
                    seed=50 + index,
                ),
            )
        )
    if unexpected:
        results.append(
            binding(
                "req-unplanned",
                test_evidence(
                    "evidence-unplanned",
                    EvidenceTier.SIMULATION,
                    test_id="unplanned-simulation",
                    seed=90,
                ),
            )
        )
    return tuple(sorted(results, key=lambda item: item.evidence.evidence_id))


def verification(
    evidence_plan: RequiredExternalEvidencePlan,
    imports: tuple[ExternalEvidenceImportBinding, ...],
) -> ExternalEvidencePlanVerification:
    return verify_external_evidence_plan(
        evidence_plan,
        imports,
        actual_change_analysis_hash=sha(22),
        verified_at=NOW + timedelta(minutes=1),
        verification_id="external-verification-1",
    )


def verification_hash_args(
    result: ExternalEvidencePlanVerification,
    **updates: Any,
) -> dict[str, Any]:
    fields = {
        "verification_id",
        "project_id",
        "plan",
        "stored_plan_hash",
        "actual_change_analysis_hash",
        "verified_at",
        "check_result",
        "imported_evidence",
        "imported_evidence_hashes",
        "missing_required_evidence_ids",
        "unexpected_imported_evidence_ids",
        "failed_required_evidence_ids",
    }
    values = {
        key: value
        for key, value in result.model_dump(mode="python").items()
        if key in fields
    }
    values.update(updates)
    return values


class ExternalEvidencePlanningContractTests(unittest.TestCase):
    def test_operating_scenario_is_first_class_source_bound_and_hashed(self) -> None:
        scenario = operating_scenario()
        restored = OperatingScenario.model_validate_json(scenario.model_dump_json())

        self.assertEqual(restored, scenario)
        with self.assertRaises(ValidationError):
            cast(Any, scenario).project_id = "other-project"
        with self.assertRaisesRegex(ValidationError, "hash does not match"):
            OperatingScenario(
                **{**scenario.model_dump(mode="python"), "scenario_text": "tampered"}
            )
        with self.assertRaisesRegex(ValidationError, "source refs.*canonical"):
            OperatingScenario(
                **{
                    **scenario.model_dump(mode="python"),
                    "source_refs": tuple(reversed(scenario.source_refs)),
                    "scenario_hash": operating_scenario_hash(
                        **operating_scenario_hash_args(
                            scenario,
                            source_refs=tuple(reversed(scenario.source_refs)),
                        )
                    ),
                }
            )
        with self.assertRaisesRegex(ValidationError, "asset input cannot be newer"):
            OperatingScenario(
                **{
                    **scenario.model_dump(mode="python"),
                    "asset_input": asset_input(captured_at=NOW + timedelta(minutes=1)),
                    "scenario_hash": operating_scenario_hash(
                        **operating_scenario_hash_args(
                            scenario,
                            asset_input=asset_input(
                                captured_at=NOW + timedelta(minutes=1)
                            ),
                        )
                    ),
                }
            )

    def test_builder_creates_plan_only_external_adapter_requests(self) -> None:
        scenario = operating_scenario()
        evidence_plan = build_required_external_evidence_plan(
            scenario,
            tuple(sorted(scenario.source_refs, key=lambda item: item.artifact_id)),
            generated_at=NOW,
            plan_id="external-plan-1",
        )

        self.assertEqual(evidence_plan.operating_scenario, scenario)
        self.assertTrue(evidence_plan.planning_only)
        self.assertEqual(evidence_plan.plan_id, "external-plan-1")
        self.assertTrue(evidence_plan.plan_hash.startswith("sha256:"))
        self.assertEqual(
            {item.tier for item in evidence_plan.required_evidence},
            {
                EvidenceTier.SIMULATION,
                EvidenceTier.BENCH,
                EvidenceTier.HIL,
                EvidenceTier.PHYSICAL_DEVICE,
            },
        )
        self.assertEqual(
            {item.external_adapter_route for item in evidence_plan.required_evidence},
            {
                "external://bench/bench-electrical",
                "external://hil/hil-regression",
                "external://physical_device/physical-device-smoke",
                "external://simulation/dynamics-simulation",
            },
        )
        with self.assertRaises(ValidationError):
            RequiredExternalEvidencePlan(
                **{**evidence_plan.model_dump(mode="python"), "status": "ready"}
            )

    def test_builder_reflects_safe_ui_hints_without_dropping_required_tiers(
        self,
    ) -> None:
        scenario = operating_scenario()
        custom_criterion = criterion("criterion-hil-current-limit")
        hint = ExternalEvidenceRequirementHint(
            test_id="custom-hil-regression",
            required_tier=EvidenceTier.HIL,
            acceptance_criteria=(custom_criterion,),
        )
        evidence_plan = build_required_external_evidence_plan(
            scenario,
            scenario.source_refs,
            generated_at=NOW,
            external_tool_ref="lab_ci_1",
            required_evidence_hints=(hint,),
        )

        by_tier = {item.tier: item for item in evidence_plan.required_evidence}
        hil = by_tier[EvidenceTier.HIL]
        self.assertEqual(hil.external_adapter_id, "lab_ci_1:hil")
        self.assertEqual(
            hil.external_adapter_route,
            "external://hil/custom-hil-regression",
        )
        self.assertEqual(hil.test_id, "custom-hil-regression")
        self.assertEqual(hil.acceptance_criteria, (custom_criterion,))
        self.assertEqual(set(by_tier), set(EXTERNAL_EVIDENCE_TIERS))
        with self.assertRaisesRegex(ValueError, "safe adapter"):
            build_required_external_evidence_plan(
                scenario,
                scenario.source_refs,
                generated_at=NOW,
                external_tool_ref="../shell",
            )
        with self.assertRaisesRegex(ValidationError, "tier must be external"):
            ExternalEvidenceRequirementHint(
                test_id="static-review",
                required_tier=EvidenceTier.STATIC,
                acceptance_criteria=(custom_criterion,),
            )
        with self.assertRaisesRegex(ValidationError, "test_id must be opaque"):
            ExternalEvidenceRequirementHint(
                test_id="../../run-anything",
                required_tier=EvidenceTier.SIMULATION,
                acceptance_criteria=(custom_criterion,),
            )

    def test_required_evidence_rejects_static_tier_and_wrong_adapter_route(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValidationError, "tier must be external"):
            required_evidence(
                "req-static",
                EvidenceTier.STATIC,
                ExternalEvidenceActionKind.INSPECT,
                test_id="static-review",
            )
        with self.assertRaisesRegex(ValidationError, "action must match"):
            required_evidence(
                "req-wrong-action",
                EvidenceTier.SIMULATION,
                ExternalEvidenceActionKind.MEASURE,
                test_id="dynamics-simulation",
            )
        request = required_evidence(
            "req-bench",
            EvidenceTier.BENCH,
            ExternalEvidenceActionKind.MEASURE,
            test_id="bench-electrical",
        )
        with self.assertRaisesRegex(ValidationError, "adapter route"):
            RequiredExternalEvidence(
                **{
                    **request.model_dump(mode="python"),
                    "external_adapter_route": "external://simulation/bench-electrical",
                }
            )

    def test_plan_hash_and_fail_closed_missing_information_are_enforced(self) -> None:
        incomplete = required_evidence(
            "req-simulation",
            EvidenceTier.SIMULATION,
            ExternalEvidenceActionKind.SIMULATE,
            test_id="dynamics-simulation",
            missing_information=("model-mass-properties",),
        )
        required = (
            required_set()[0],
            required_set()[1],
            required_set()[2],
            incomplete,
        )
        with self.assertRaisesRegex(ValidationError, "must reference incomplete"):
            plan(required=required)

        evidence_plan = plan(
            required=required,
            missing_information=("required_evidence:req-simulation",),
        )
        with self.assertRaisesRegex(ValidationError, "hash does not match"):
            RequiredExternalEvidencePlan(
                **{
                    **evidence_plan.model_dump(mode="python"),
                    "generated_at": NOW + timedelta(minutes=1),
                }
            )
        with self.assertRaisesRegex(ValidationError, "must cover"):
            plan(required=required_set()[:3])

    def test_import_binding_uses_existing_test_execution_evidence_hash(self) -> None:
        evidence = test_evidence(
            "evidence-req-bench",
            EvidenceTier.BENCH,
            test_id="bench-electrical",
        )
        bound = binding("req-bench", evidence)

        self.assertEqual(bound.evidence, evidence)
        self.assertEqual(bound.evidence_hash, canonical_sha256(evidence))
        with self.assertRaisesRegex(ValidationError, "hash does not match"):
            ExternalEvidenceImportBinding(
                required_evidence_id="req-bench",
                evidence=evidence.model_copy(update={"source_hash": sha(99)}),
                evidence_hash=bound.evidence_hash,
            )
        with self.assertRaisesRegex(ValidationError, "bench and HIL.*fixture"):
            TestExecutionEvidence(
                **{
                    **test_evidence(
                        "evidence-req-bench",
                        EvidenceTier.BENCH,
                        test_id="bench-electrical",
                    ).model_dump(mode="python"),
                    "fixture_id": None,
                }
            )

    def test_verification_binds_stored_plan_to_actual_imported_test_evidence(
        self,
    ) -> None:
        evidence_plan = plan()
        imports = bindings_for_plan(evidence_plan)
        result = verification(evidence_plan, imports)

        self.assertEqual(result.check_result, ExternalEvidenceCheckResult.MATCHES_PLAN)
        self.assertFalse(result.missing_required_evidence_ids)
        self.assertEqual(
            result.imported_evidence_hashes,
            tuple(sorted(item.evidence_hash for item in imports)),
        )
        with self.assertRaisesRegex(ValidationError, "stored plan hash"):
            ExternalEvidencePlanVerification(
                **{
                    **result.model_dump(mode="python"),
                    "stored_plan_hash": sha(99),
                }
            )
        with self.assertRaisesRegex(ValidationError, "hashes must match"):
            ExternalEvidencePlanVerification(
                **{
                    **result.model_dump(mode="python"),
                    "imported_evidence_hashes": (sha(99),),
                    "verification_hash": external_evidence_plan_verification_hash(
                        **verification_hash_args(
                            result,
                            imported_evidence_hashes=(sha(99),),
                        )
                    ),
                }
            )
        with self.assertRaisesRegex(ValidationError, "change analysis must match"):
            ExternalEvidencePlanVerification(
                **{
                    **result.model_dump(mode="python"),
                    "actual_change_analysis_hash": sha(23),
                    "verification_hash": external_evidence_plan_verification_hash(
                        **verification_hash_args(
                            result,
                            actual_change_analysis_hash=sha(23),
                        )
                    ),
                }
            )

    def test_verification_fails_closed_on_missing_failed_or_unexpected_imports(
        self,
    ) -> None:
        evidence_plan = plan()
        result = verification(
            evidence_plan,
            bindings_for_plan(
                evidence_plan,
                omit={"req-hil"},
                fail={"req-bench"},
                unexpected=True,
            ),
        )

        self.assertEqual(
            result.check_result,
            ExternalEvidenceCheckResult.DIFFERS_FROM_PLAN,
        )
        self.assertEqual(result.missing_required_evidence_ids, ("req-hil",))
        self.assertEqual(result.failed_required_evidence_ids, ("req-bench",))
        self.assertEqual(
            result.unexpected_imported_evidence_ids, ("evidence-unplanned",)
        )
        with self.assertRaisesRegex(ValidationError, "check result must follow"):
            ExternalEvidencePlanVerification(
                **{
                    **result.model_dump(mode="python"),
                    "check_result": ExternalEvidenceCheckResult.MATCHES_PLAN,
                    "verification_hash": external_evidence_plan_verification_hash(
                        **verification_hash_args(
                            result,
                            check_result=ExternalEvidenceCheckResult.MATCHES_PLAN,
                        )
                    ),
                }
            )
        with self.assertRaisesRegex(ValidationError, "missing required evidence"):
            ExternalEvidencePlanVerification(
                **{
                    **result.model_dump(mode="python"),
                    "missing_required_evidence_ids": (),
                    "verification_hash": external_evidence_plan_verification_hash(
                        **verification_hash_args(
                            result,
                            missing_required_evidence_ids=(),
                        )
                    ),
                }
            )


if __name__ == "__main__":
    unittest.main()
