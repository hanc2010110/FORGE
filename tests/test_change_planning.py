from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from pydantic import ValidationError

from forge_core.change_management import (
    ArtifactDomain,
    ConnectorSnapshot,
    EvidenceTier,
    ExternalArtifactRef,
    RetestRequirement,
    RetestStatus,
    SourceSystem,
)
from forge_core.change_planning import (
    AssetInput,
    AssetInputKind,
    ChangeImpactPreview,
    ChangeScenario,
    ComponentSpecification,
    PlanDeviation,
    PlanDeviationKind,
    PlanIntentKind,
    PlannedChangeAction,
    PlannedComponentChange,
    PlanVerification,
    PreviewFinding,
    PreviewFindingSeverity,
    PreviewRecommendation,
    change_impact_preview_hash,
    change_scenario_hash,
    component_specification_hash,
    plan_verification_hash,
)
from forge_core.constraints import InterfaceContract, InterfaceSignal, QuoteSnapshot
from forge_core.impact_engine import analyze_change
from forge_core.models import Quantity, SourceRef
from forge_core.preview_engine import (
    preview_change_scenario,
    verify_plan_against_actual,
)

NOW = datetime(2026, 8, 29, tzinfo=UTC)


def sha(seed: int) -> str:
    return f"sha256:{seed:064x}"


def quantity(value: float, unit: str, dimension: str) -> Quantity:
    return Quantity(
        value=value,
        unit=unit,
        dimension=dimension,
        source=SourceRef(kind="user", identifier="test:change-planning"),
    )


def source_ref(
    seed: int,
    *,
    artifact_id: str = "controller-board",
    domain: ArtifactDomain = ArtifactDomain.HARDWARE,
) -> ExternalArtifactRef:
    return ExternalArtifactRef(
        artifact_id=artifact_id,
        domain=domain,
        source_system=SourceSystem.PLM,
        source_revision=f"r{seed}",
        content_hash=sha(seed),
        captured_at=NOW,
    )


def quote(part_number: str = "DRV-2A") -> QuoteSnapshot:
    return QuoteSnapshot(
        quote_id=f"quote-{part_number}",
        part_number=part_number,
        supplier="supplier",
        region="KR",
        currency="USD",
        unit_price=Decimal("12.50"),
        minimum_quantity=1,
        observed_at=NOW,
        expires_at=NOW + timedelta(days=30),
        shipping_included=True,
        shipping_cost=None,
        tax_included=True,
        tax_cost=None,
        source_url=f"https://supplier.example/{part_number}",
        source_hash=sha(77),
    )


def signal(pin: str = "J3-4", voltage_max: float = 3.3) -> InterfaceSignal:
    return InterfaceSignal(
        name="motor_pwm",
        pin=pin,
        direction="output",
        voltage_min=quantity(0, "V", "voltage"),
        voltage_max=quantity(voltage_max, "V", "voltage"),
        command_min=quantity(0, "%", "ratio"),
        command_max=quantity(100, "%", "ratio"),
        safe_value=quantity(0, "%", "ratio"),
    )


def interface_contract(seed: int, *, pin: str = "J3-4") -> InterfaceContract:
    return InterfaceContract(
        contract_id=f"motor-iface-{seed}",
        protocol_schema_hash=sha(seed),
        signals=(signal(pin=pin),),
    )


def component(
    component_id: str,
    part_number: str,
    seed: int,
    *,
    domain: ArtifactDomain = ArtifactDomain.HARDWARE,
    pin: str = "J3-4",
) -> ComponentSpecification:
    artifact = source_ref(seed, domain=domain)
    contract = interface_contract(seed, pin=pin)
    values: dict[str, Any] = {
        "component_id": component_id,
        "manufacturer": "Acme Components",
        "part_number": part_number,
        "quantity": 1,
        "source_ref": artifact,
        "interface_contract": contract,
        "quote": quote(part_number),
        "attributes": {
            "logic_voltage": quantity(3.3, "V", "voltage"),
        },
    }
    return ComponentSpecification(
        **values,
        specification_hash=component_specification_hash(**values),
    )


def asset_input() -> AssetInput:
    return AssetInput(
        asset_id="mobile-base-alpha",
        kind=AssetInputKind.PLM_SNAPSHOT,
        source_system=SourceSystem.PLM,
        source_locator="plm://robot-controller/mobile-base-alpha",
        source_revision="HW-12",
        content_hash=sha(12),
        captured_at=NOW,
    )


def planned_change(change_id: str = "change-motor-driver") -> PlannedComponentChange:
    return PlannedComponentChange(
        change_id=change_id,
        action=PlannedChangeAction.REPLACE,
        before=component("motor-driver", "DRV-1A", 1),
        after=component("motor-driver", "DRV-2A", 2, pin="J3-7"),
        affected_domains=(
            ArtifactDomain.BOM,
            ArtifactDomain.DOCUMENTATION,
            ArtifactDomain.FIRMWARE,
            ArtifactDomain.HARDWARE,
            ArtifactDomain.PROTOCOL,
            ArtifactDomain.TEST,
        ),
        required_actions=(
            "bench-electrical",
            "bom-quote-refresh",
            "firmware-pinmap-update",
            "protocol-schema-review",
        ),
        rationale=("pinout-change", "supplier-eol"),
    )


def scenario(change: PlannedComponentChange | None = None) -> ChangeScenario:
    changes = (change or planned_change(),)
    values: dict[str, Any] = {
        "scenario_id": "scenario-1",
        "project_id": "project-1",
        "asset_input": asset_input(),
        "intent_kind": PlanIntentKind.CHANGE_IMPACT,
        "baseline_snapshot_id": "snapshot-11",
        "baseline_snapshot_hash": sha(11),
        "proposed_hardware_revision_id": "HW-12-proposed",
        "created_at": NOW,
        "changes": changes,
    }
    return ChangeScenario(
        **values,
        scenario_hash=change_scenario_hash(**values),
    )


def snapshot(
    snapshot_id: str,
    revision_id: str,
    *artifacts: ExternalArtifactRef,
) -> ConnectorSnapshot:
    return ConnectorSnapshot(
        snapshot_id=snapshot_id,
        project_id="project-1",
        hardware_revision_id=revision_id,
        captured_at=NOW,
        artifacts=tuple(
            sorted(
                artifacts,
                key=lambda item: (item.source_system.value, item.artifact_id),
            )
        ),
    )


def preview(recommendation: PreviewRecommendation) -> ChangeImpactPreview:
    plan = scenario()
    finding = PreviewFinding(
        finding_id="preview-finding-pin",
        rule_id="pin_assignment_change",
        severity=PreviewFindingSeverity.RISK,
        summary="planned component replacement changes motor_pwm pin",
        affected_domains=(ArtifactDomain.FIRMWARE, ArtifactDomain.HARDWARE),
        evidence_refs=(plan.scenario_hash,),
    )
    retest = RetestRequirement(
        retest_id="preview-retest-bench",
        test_id="bench-electrical",
        required_tier=EvidenceTier.BENCH,
        status=RetestStatus.REQUIRED,
        triggered_by=("change-motor-driver",),
        reason_codes=("pinout-change",),
    )
    values: dict[str, Any] = {
        "preview_id": "preview-1",
        "project_id": plan.project_id,
        "scenario_id": plan.scenario_id,
        "scenario_hash": plan.scenario_hash,
        "planner_version": "1.0.0",
        "generated_at": NOW,
        "recommendation": recommendation,
        "predicted_affected_domains": (
            ArtifactDomain.FIRMWARE,
            ArtifactDomain.HARDWARE,
            ArtifactDomain.TEST,
        ),
        "predicted_required_actions": (
            "bench-electrical",
            "firmware-pinmap-update",
        ),
        "predicted_findings": (finding,),
        "predicted_retests": (retest,),
        "assumptions": ("candidate-datasheet-is-current",),
        "missing_information": (),
    }
    return ChangeImpactPreview(
        **values,
        preview_hash=change_impact_preview_hash(**values),
    )


class ChangePlanningContractTests(unittest.TestCase):
    def test_component_specification_is_source_bound_and_exactly_hashed(self) -> None:
        spec = component("motor-driver", "DRV-2A", 2)
        restored = ComponentSpecification.model_validate_json(spec.model_dump_json())

        self.assertEqual(restored, spec)
        self.assertEqual(restored.manufacturer, "Acme Components")
        self.assertEqual(restored.quantity, 1)
        self.assertEqual(restored.specification_hash, spec.specification_hash)
        with self.assertRaises(TypeError):
            cast(Any, spec.attributes)["logic_voltage"] = quantity(5, "V", "voltage")
        with self.assertRaises(ValidationError):
            ComponentSpecification(
                component_id="motor-driver",
                manufacturer="Acme Components",
                part_number="DRV-2A",
                quantity=1,
                source_ref=source_ref(9, domain=ArtifactDomain.DOCUMENTATION),
                interface_contract=None,
                quote=None,
                attributes={},
                specification_hash=sha(9),
            )
        values: dict[str, Any] = {
            "component_id": "motor-driver",
            "manufacturer": "Acme Components",
            "part_number": "DRV-2A",
            "quantity": 1,
            "source_ref": source_ref(2),
            "interface_contract": None,
            "quote": quote("OTHER-PART"),
            "attributes": {},
        }
        with self.assertRaisesRegex(ValidationError, "quote part_number"):
            ComponentSpecification(
                **values,
                specification_hash=component_specification_hash(**values),
            )
        with self.assertRaisesRegex(ValidationError, "hash does not match"):
            ComponentSpecification(
                **{
                    **values,
                    "quote": quote("DRV-2A"),
                    "specification_hash": sha(99),
                }
            )

    def test_planned_component_change_actions_are_validated(self) -> None:
        change = planned_change()

        self.assertEqual(change.action, PlannedChangeAction.REPLACE)
        with self.assertRaisesRegex(ValidationError, "requires only an after"):
            PlannedComponentChange(
                change_id="bad-add",
                action=PlannedChangeAction.ADD,
                before=component("motor-driver", "DRV-1A", 1),
                after=component("motor-driver", "DRV-2A", 2),
                affected_domains=(ArtifactDomain.HARDWARE,),
                required_actions=("bom-update",),
            )
        with self.assertRaisesRegex(ValidationError, "preserve component_id"):
            PlannedComponentChange(
                **{
                    **change.model_dump(mode="python"),
                    "after": component("other-component", "DRV-2A", 2),
                }
            )
        with self.assertRaisesRegex(ValidationError, "canonical order"):
            PlannedComponentChange(
                **{
                    **change.model_dump(mode="python"),
                    "required_actions": ("firmware-pinmap-update", "bench-electrical"),
                }
            )

    def test_change_scenario_requires_utc_unique_canonical_changes_and_exact_hash(
        self,
    ) -> None:
        plan = scenario()

        self.assertEqual(plan.baseline_snapshot_hash, sha(11))
        with self.assertRaises(ValidationError):
            ChangeScenario(
                **{
                    **plan.model_dump(mode="python"),
                    "created_at": datetime(2026, 8, 29),
                }
            )
        duplicate = planned_change("change-motor-driver")
        with self.assertRaisesRegex(ValidationError, "change IDs must be unique"):
            ChangeScenario(
                **{
                    **plan.model_dump(mode="python"),
                    "changes": (plan.changes[0], duplicate),
                }
            )
        later = planned_change("change-z")
        earlier = planned_change("change-a")
        values: dict[str, Any] = {
            **plan.model_dump(mode="python"),
            "changes": (later, earlier),
        }
        values["scenario_hash"] = change_scenario_hash(
            **{
                key: value
                for key, value in values.items()
                if key
                in {
                    "scenario_id",
                    "project_id",
                    "asset_input",
                    "intent_kind",
                    "baseline_snapshot_id",
                    "baseline_snapshot_hash",
                    "proposed_hardware_revision_id",
                    "created_at",
                    "changes",
                }
            }
        )
        with self.assertRaisesRegex(ValidationError, "canonical order"):
            ChangeScenario(**values)
        with self.assertRaisesRegex(ValidationError, "hash does not match"):
            ChangeScenario(
                **{
                    **plan.model_dump(mode="python"),
                    "proposed_hardware_revision_id": "HW-13-proposed",
                }
            )

    def test_preview_is_not_release_status_or_release_evidence(self) -> None:
        forecast = preview(PreviewRecommendation.CHANGES_REQUIRED)

        self.assertEqual(
            forecast.recommendation,
            PreviewRecommendation.CHANGES_REQUIRED,
        )
        with self.assertRaises(ValidationError):
            ChangeImpactPreview(
                **{
                    **forecast.model_dump(mode="python"),
                    "recommendation": "ready",
                }
            )
        with self.assertRaisesRegex(ValidationError, "only be required"):
            ChangeImpactPreview(
                **{
                    **forecast.model_dump(mode="python"),
                    "predicted_retests": (
                        forecast.predicted_retests[0].model_copy(
                            update={
                                "status": RetestStatus.PASSED,
                                "satisfied_by_evidence_id": "evidence-1",
                            }
                        ),
                    ),
                }
            )
        with self.assertRaisesRegex(ValidationError, "indeterminate"):
            ChangeImpactPreview(
                **{
                    **forecast.model_dump(mode="python"),
                    "missing_information": ("thermal-rating",),
                }
            )
        with self.assertRaisesRegex(ValidationError, "hash does not match"):
            ChangeImpactPreview(
                **{
                    **forecast.model_dump(mode="python"),
                    "predicted_required_actions": (
                        "bench-electrical",
                        "firmware-pinmap-update",
                        "hil-regression",
                    ),
                }
            )

    def test_preview_engine_predicts_component_change_work_before_release(
        self,
    ) -> None:
        forecast = preview_change_scenario(scenario(), generated_at=NOW)

        self.assertEqual(
            forecast.recommendation,
            PreviewRecommendation.CHANGES_REQUIRED,
        )
        self.assertIn(ArtifactDomain.FIRMWARE, forecast.predicted_affected_domains)
        self.assertIn("firmware-pinmap-update", forecast.predicted_required_actions)
        self.assertIn("protocol-schema-review", forecast.predicted_required_actions)
        self.assertEqual(
            {item.test_id for item in forecast.predicted_retests},
            {
                "bench-electrical",
                "bom-provenance-validation",
                "documentation-review",
                "firmware-build",
                "hil-regression",
                "interface-contract-validation",
                "physical-device-smoke",
                "protocol-conformance",
            },
        )
        self.assertTrue(
            any(
                item.rule_id == "pin_assignment_change"
                for item in forecast.predicted_findings
            )
        )

    def test_verify_plan_reports_missing_and_unplanned_actual_changes(self) -> None:
        plan = scenario()
        forecast = preview_change_scenario(plan, generated_at=NOW)
        before_hardware = source_ref(11, artifact_id="controller-board")
        after_hardware = ExternalArtifactRef(
            artifact_id="controller-board",
            domain=ArtifactDomain.HARDWARE,
            source_system=SourceSystem.PLM,
            source_revision="r12",
            content_hash=sha(12),
            captured_at=NOW,
        )
        before_doc = source_ref(
            13, artifact_id="manual", domain=ArtifactDomain.DOCUMENTATION
        )
        after_doc = ExternalArtifactRef(
            artifact_id="manual",
            domain=ArtifactDomain.DOCUMENTATION,
            source_system=SourceSystem.PLM,
            source_revision="r14",
            content_hash=sha(14),
            captured_at=NOW,
        )
        before = snapshot("snapshot-11", "HW-11", before_hardware, before_doc)
        after = snapshot("snapshot-12", "HW-12", after_hardware, after_doc)
        actual = analyze_change(
            before,
            after,
            interface_projections=(),
        )
        verification = verify_plan_against_actual(
            forecast,
            plan,
            actual,
            verified_at=NOW,
        )

        self.assertFalse(verification.matches_plan)
        self.assertIn("domain:hardware", verification.observed_planned_refs)
        self.assertTrue(
            any(
                item.kind is PlanDeviationKind.MISSING_PLANNED_CHANGE
                and item.planned_ref == "facet:pin_assignment"
                for item in verification.deviations
            )
        )
        self.assertTrue(
            any(
                item.kind is PlanDeviationKind.UNPLANNED_ACTUAL_CHANGE
                and item.actual_ref is not None
                for item in verification.deviations
            )
        )

    def test_plan_verification_binds_preview_to_actual_change_analysis(self) -> None:
        forecast = preview(PreviewRecommendation.CHANGES_REQUIRED)
        deviation = PlanDeviation(
            deviation_id="dev-firmware-pinmap",
            kind=PlanDeviationKind.MISSING_PLANNED_CHANGE,
            summary="firmware pin map was not updated after hardware pin change",
            affected_domains=(ArtifactDomain.FIRMWARE,),
            planned_ref="firmware-pinmap-update",
            actual_ref=None,
        )
        values: dict[str, Any] = {
            "verification_id": "plan-verification-1",
            "project_id": forecast.project_id,
            "scenario_id": forecast.scenario_id,
            "preview_hash": forecast.preview_hash,
            "actual_change_analysis_hash": sha(200),
            "verified_at": NOW,
            "matches_plan": False,
            "observed_planned_refs": ("bench-electrical",),
            "deviations": (deviation,),
        }
        result = PlanVerification(
            **values,
            verification_hash=plan_verification_hash(**values),
        )

        self.assertFalse(result.matches_plan)
        self.assertEqual(result.observed_planned_refs, ("bench-electrical",))
        with self.assertRaisesRegex(ValidationError, "must exactly follow"):
            PlanVerification(
                **{
                    **values,
                    "matches_plan": True,
                    "verification_hash": plan_verification_hash(
                        **{**values, "matches_plan": True}
                    ),
                }
            )
        with self.assertRaisesRegex(ValidationError, "must reference"):
            PlanDeviation(
                deviation_id="dev-empty",
                kind=PlanDeviationKind.FINDING_GAP,
                summary="missing source binding",
                affected_domains=(ArtifactDomain.HARDWARE,),
            )
        with self.assertRaisesRegex(ValidationError, "cannot include missing"):
            PlanVerification(
                **{
                    **values,
                    "observed_planned_refs": (
                        "bench-electrical",
                        "firmware-pinmap-update",
                    ),
                    "verification_hash": plan_verification_hash(
                        **{
                            **values,
                            "observed_planned_refs": (
                                "bench-electrical",
                                "firmware-pinmap-update",
                            ),
                        }
                    ),
                }
            )
        with self.assertRaisesRegex(ValidationError, "hash does not match"):
            PlanVerification(
                **{
                    **values,
                    "actual_change_analysis_hash": sha(201),
                    "verification_hash": result.verification_hash,
                }
            )


if __name__ == "__main__":
    unittest.main()
