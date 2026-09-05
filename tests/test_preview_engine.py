from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from forge_core.change_management import (
    ArtifactDomain,
    ConnectorSnapshot,
    ExternalArtifactRef,
    SourceSystem,
)
from forge_core.change_planning import (
    AssetInput,
    AssetInputKind,
    ChangeImpactPreview,
    ChangeScenario,
    ComponentSpecification,
    PlannedChangeAction,
    PlannedComponentChange,
    PreviewFindingSeverity,
    PreviewRecommendation,
    change_impact_preview_hash,
    change_scenario_hash,
    component_specification_hash,
)
from forge_core.constraints import InterfaceContract, InterfaceSignal, QuoteSnapshot
from forge_core.impact_engine import (
    NormalizedInterfaceProjection,
    analyze_change,
    connector_snapshot_hash,
    interface_projection_hash,
)
from forge_core.models import Quantity, SourceRef
from forge_core.preview_engine import (
    preview_change_scenario,
    verify_plan_against_actual,
)

NOW = datetime(2026, 8, 29, tzinfo=UTC)


def sha(seed: int) -> str:
    return f"sha256:{seed:064x}"


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


def quantity(value: float, unit: str, dimension: str) -> Quantity:
    return Quantity(
        value=value,
        unit=unit,
        dimension=dimension,
        source=SourceRef(kind="user", identifier="test:preview-engine"),
    )


def source_ref(
    seed: int,
    *,
    artifact_id: str = "controller-board",
    domain: ArtifactDomain = ArtifactDomain.HARDWARE,
    source_system: SourceSystem = SourceSystem.PLM,
) -> ExternalArtifactRef:
    return ExternalArtifactRef(
        artifact_id=artifact_id,
        domain=domain,
        source_system=source_system,
        source_revision=f"r{seed}",
        content_hash=sha(seed),
        captured_at=NOW,
    )


def quote(
    part_number: str,
    *,
    seed: int,
    unit_price: str = "12.50",
    observed_offset_days: int = 0,
) -> QuoteSnapshot:
    observed = NOW + timedelta(days=observed_offset_days)
    return QuoteSnapshot(
        quote_id=f"quote-{part_number}-{seed}",
        part_number=part_number,
        supplier="supplier",
        region="US",
        currency="USD",
        unit_price=Decimal(unit_price),
        minimum_quantity=1,
        observed_at=observed,
        expires_at=observed + timedelta(days=30),
        shipping_included=True,
        shipping_cost=None,
        tax_included=True,
        tax_cost=None,
        source_url=f"https://supplier.example/{part_number}/{seed}",
        source_hash=sha(1000 + seed),
    )


def signal(
    *,
    pin: str = "J3-4",
    voltage_unit: str = "V",
    voltage_max: float = 5.0,
    command_unit: str = "%",
    command_dimension: str = "ratio",
    command_max: float = 100,
    safe_value: float = 0,
) -> InterfaceSignal:
    return InterfaceSignal(
        name="motor_pwm",
        pin=pin,
        direction="output",
        voltage_min=quantity(0, voltage_unit, "voltage"),
        voltage_max=quantity(voltage_max, voltage_unit, "voltage"),
        command_min=quantity(0, command_unit, command_dimension),
        command_max=quantity(command_max, command_unit, command_dimension),
        safe_value=quantity(safe_value, command_unit, command_dimension),
    )


def interface_contract(
    seed: int,
    *,
    pin: str = "J3-4",
    voltage_unit: str = "V",
    voltage_max: float = 5.0,
    command_unit: str = "%",
    command_dimension: str = "ratio",
    command_max: float = 100,
    safe_value: float = 0,
) -> InterfaceContract:
    return InterfaceContract(
        contract_id=f"motor-iface-{seed}",
        protocol_schema_hash=sha(seed),
        signals=(
            signal(
                pin=pin,
                voltage_unit=voltage_unit,
                voltage_max=voltage_max,
                command_unit=command_unit,
                command_dimension=command_dimension,
                command_max=command_max,
                safe_value=safe_value,
            ),
        ),
    )


def component(
    component_id: str,
    part_number: str,
    seed: int,
    *,
    contract: InterfaceContract | None,
    quote_snapshot: QuoteSnapshot | None,
    attributes: dict[str, Quantity] | None = None,
) -> ComponentSpecification:
    values: dict[str, Any] = {
        "component_id": component_id,
        "manufacturer": "Acme Components",
        "part_number": part_number,
        "quantity": 1,
        "source_ref": source_ref(seed),
        "interface_contract": contract,
        "quote": quote_snapshot,
        "attributes": attributes
        or {
            "logic_voltage": quantity(5, "V", "voltage"),
            "package_width": quantity(12.5, "mm", "length"),
        },
    }
    return ComponentSpecification(
        **values,
        specification_hash=component_specification_hash(**values),
    )


def planned_change(
    *,
    change_id: str = "change-motor-driver",
    before: ComponentSpecification | None = None,
    after: ComponentSpecification | None = None,
    affected_domains: tuple[ArtifactDomain, ...] | None = None,
    required_actions: tuple[str, ...] = ("firmware-pinmap-update",),
) -> PlannedComponentChange:
    return PlannedComponentChange(
        change_id=change_id,
        action=PlannedChangeAction.REPLACE,
        before=before
        or component(
            "motor-driver",
            "DRV-5V",
            1,
            contract=interface_contract(1),
            quote_snapshot=quote("DRV-5V", seed=1),
        ),
        after=after
        or component(
            "motor-driver",
            "DRV-33V",
            2,
            contract=interface_contract(
                2,
                pin="J3-7",
                voltage_max=3.3,
                command_unit="rpm",
                command_dimension="angular_speed",
                command_max=5000,
            ),
            quote_snapshot=quote("DRV-33V", seed=2, unit_price="9.10"),
        ),
        affected_domains=affected_domains
        or (
            ArtifactDomain.FIRMWARE,
            ArtifactDomain.HARDWARE,
        ),
        required_actions=required_actions,
        rationale=("candidate-substitution",),
    )


def scenario(
    changes: tuple[PlannedComponentChange, ...] | None = None,
) -> ChangeScenario:
    values: dict[str, Any] = {
        "scenario_id": "scenario-1",
        "project_id": "project-1",
        "asset_input": asset_input(),
        "baseline_snapshot_id": "snapshot-11",
        "baseline_snapshot_hash": sha(11),
        "proposed_hardware_revision_id": "HW-12-proposed",
        "created_at": NOW,
        "changes": changes or (planned_change(),),
    }
    return ChangeScenario(**values, scenario_hash=change_scenario_hash(**values))


def snapshot(
    snapshot_id: str,
    hardware_revision_id: str,
    artifacts: tuple[ExternalArtifactRef, ...],
) -> ConnectorSnapshot:
    return ConnectorSnapshot(
        snapshot_id=snapshot_id,
        project_id="project-1",
        hardware_revision_id=hardware_revision_id,
        captured_at=NOW,
        artifacts=artifacts,
    )


def projection(
    snapshot_id: str,
    source: ExternalArtifactRef,
    contract: InterfaceContract,
) -> NormalizedInterfaceProjection:
    normalizer_version = "1.0.0"
    return NormalizedInterfaceProjection(
        snapshot_id=snapshot_id,
        source_ref=source,
        normalizer_version=normalizer_version,
        contract=contract,
        projection_hash=interface_projection_hash(
            snapshot_id,
            source,
            normalizer_version,
            contract,
        ),
    )


class PreviewEngineTests(unittest.TestCase):
    def test_preview_forecasts_component_swap_without_binding_release_evidence(
        self,
    ) -> None:
        plan = scenario()

        preview = preview_change_scenario(plan, generated_at=NOW)

        self.assertEqual(preview.recommendation, PreviewRecommendation.INCOMPATIBLE)
        self.assertEqual(preview.missing_information, ())
        self.assertTrue(
            set(ArtifactDomain).issubset(set(preview.predicted_affected_domains))
        )
        self.assertTrue(
            {
                "bench-electrical",
                "bom-quote-refresh",
                "firmware-pinmap-update",
                "protocol-schema-review",
            }.issubset(set(preview.predicted_required_actions))
        )
        self.assertTrue(
            {
                "pin_assignment_change",
                "protocol_schema_change",
                "quote_provenance_or_price_change",
                "voltage_range_change",
            }.issubset({finding.rule_id for finding in preview.predicted_findings})
        )
        self.assertIn(
            PreviewFindingSeverity.INCOMPATIBILITY,
            {finding.severity for finding in preview.predicted_findings},
        )
        self.assertTrue(preview.predicted_retests)
        self.assertTrue(
            all(
                retest.status.value == "required"
                and retest.satisfied_by_evidence_id is None
                for retest in preview.predicted_retests
            )
        )
        self.assertIn(
            "physics-simulation-results-must-arrive-as-external-evidence",
            preview.assumptions,
        )

    def test_missing_source_bound_interface_or_quote_is_indeterminate(self) -> None:
        incomplete_after = component(
            "motor-driver",
            "DRV-33V",
            2,
            contract=None,
            quote_snapshot=None,
        )
        plan = scenario(changes=(planned_change(after=incomplete_after),))

        preview = preview_change_scenario(plan, generated_at=NOW)

        self.assertEqual(preview.recommendation, PreviewRecommendation.INDETERMINATE)
        self.assertIn(
            "change-motor-driver:after:interface_contract",
            preview.missing_information,
        )
        self.assertIn("change-motor-driver:after:quote", preview.missing_information)
        self.assertIn(
            "interface_contract_missing",
            {finding.rule_id for finding in preview.predicted_findings},
        )

    def test_same_dimension_unit_change_requires_work_not_incompatibility(self) -> None:
        before = component(
            "motor-driver",
            "DRV-5V",
            1,
            contract=interface_contract(1),
            quote_snapshot=quote("DRV-5V", seed=1),
        )
        after = component(
            "motor-driver",
            "DRV-5V-MV",
            2,
            contract=InterfaceContract(
                contract_id="motor-iface-mv",
                protocol_schema_hash=before.interface_contract.protocol_schema_hash
                if before.interface_contract is not None
                else sha(1),
                signals=(signal(voltage_unit="mV", voltage_max=5000),),
            ),
            quote_snapshot=quote("DRV-5V-MV", seed=2),
        )
        plan = scenario(changes=(planned_change(before=before, after=after),))

        preview = preview_change_scenario(plan, generated_at=NOW)

        self.assertEqual(
            preview.recommendation,
            PreviewRecommendation.CHANGES_REQUIRED,
        )
        unit_findings = [
            finding
            for finding in preview.predicted_findings
            if finding.rule_id == "voltage_unit_change"
        ]
        self.assertEqual(
            {finding.severity for finding in unit_findings},
            {PreviewFindingSeverity.RISK},
        )

    def test_preview_is_deterministic_and_scenarios_reject_reordered_changes(
        self,
    ) -> None:
        first = planned_change(change_id="change-a")
        second = planned_change(
            change_id="change-b",
            before=component(
                "brake-driver",
                "BRK-5V",
                3,
                contract=interface_contract(3),
                quote_snapshot=quote("BRK-5V", seed=3),
            ),
            after=component(
                "brake-driver",
                "BRK-33V",
                4,
                contract=interface_contract(4, pin="J4-1", voltage_max=3.3),
                quote_snapshot=quote("BRK-33V", seed=4),
                attributes={
                    "package_width": quantity(12.5, "mm", "length"),
                    "logic_voltage": quantity(3.3, "V", "voltage"),
                },
            ),
        )
        plan = scenario(changes=(first, second))

        one = preview_change_scenario(plan, generated_at=NOW)
        two = preview_change_scenario(plan, generated_at=NOW)

        self.assertEqual(one.preview_hash, two.preview_hash)
        self.assertEqual(one.predicted_findings, two.predicted_findings)
        with self.assertRaisesRegex(ValidationError, "canonical order"):
            scenario(changes=(second, first))

    def test_verify_reports_missing_planned_and_unplanned_actual_refs(self) -> None:
        plan = scenario()
        preview = preview_change_scenario(plan, generated_at=NOW)
        before_contract = interface_contract(1)
        after_contract = interface_contract(
            2,
            pin="J3-7",
            voltage_max=3.3,
            command_unit="rpm",
            command_dimension="angular_speed",
            command_max=5000,
        )
        hardware_before = source_ref(1, artifact_id="controller-board")
        hardware_after = source_ref(2, artifact_id="controller-board")
        firmware = source_ref(
            10,
            artifact_id="motor-fw",
            domain=ArtifactDomain.FIRMWARE,
            source_system=SourceSystem.GIT,
        )
        protocol = source_ref(
            20,
            artifact_id="motor-protocol",
            domain=ArtifactDomain.PROTOCOL,
            source_system=SourceSystem.GIT,
        )
        previous = snapshot(
            "snapshot-11",
            "HW-11",
            (hardware_before, firmware, protocol),
        )
        current = snapshot("snapshot-12", "HW-12", (hardware_after, firmware, protocol))
        actual = analyze_change(
            previous,
            current,
            interface_projections=(
                projection(previous.snapshot_id, hardware_before, before_contract),
                projection(current.snapshot_id, hardware_after, after_contract),
                projection(
                    current.snapshot_id,
                    firmware,
                    interface_contract(11),
                ),
                projection(
                    current.snapshot_id,
                    protocol,
                    interface_contract(21),
                ),
            ),
        )

        verification = verify_plan_against_actual(
            preview,
            plan,
            actual,
            verified_at=NOW + timedelta(hours=1),
        )

        self.assertFalse(verification.matches_plan)
        self.assertEqual(verification.actual_change_analysis_hash, actual.analysis_hash)
        self.assertIn("domain:hardware", verification.observed_planned_refs)
        self.assertIn("retest:hil-regression:hil", verification.observed_planned_refs)
        self.assertTrue(
            {
                "finding:pin_mismatch",
                "finding:protocol_schema_mismatch",
            }.issubset(
                {
                    deviation.actual_ref
                    for deviation in verification.deviations
                    if deviation.actual_ref is not None
                }
            )
        )
        self.assertTrue(
            {
                "finding:pin_assignment_change",
                "finding:quote_provenance_or_price_change",
            }.issubset(
                {
                    deviation.planned_ref
                    for deviation in verification.deviations
                    if deviation.planned_ref is not None
                }
            )
        )
        self.assertEqual(
            connector_snapshot_hash(previous),
            actual.from_snapshot_hash,
        )

    def test_verify_rejects_preview_from_another_project_or_scenario(self) -> None:
        plan = scenario()
        preview = preview_change_scenario(plan, generated_at=NOW)
        actual_snapshot = snapshot(
            "snapshot-12",
            "HW-12-proposed",
            (source_ref(2, artifact_id="controller-board"),),
        )
        actual = analyze_change(
            snapshot(
                "snapshot-11",
                "HW-11",
                (source_ref(1, artifact_id="controller-board"),),
            ),
            actual_snapshot,
        )

        def rebind(**updates: str) -> ChangeImpactPreview:
            values = preview.model_dump(mode="python") | updates
            values.pop("preview_hash")
            hash_values = {
                key: value for key, value in values.items() if key != "schema_version"
            }
            values["preview_hash"] = change_impact_preview_hash(**hash_values)
            return ChangeImpactPreview.model_validate(values)

        with self.assertRaisesRegex(ValueError, "different project"):
            verify_plan_against_actual(
                rebind(project_id="other-project"),
                plan,
                actual,
                verified_at=NOW + timedelta(hours=1),
            )
        with self.assertRaisesRegex(ValueError, "does not belong to scenario"):
            verify_plan_against_actual(
                rebind(scenario_id="other-scenario"),
                plan,
                actual,
                verified_at=NOW + timedelta(hours=1),
            )


if __name__ == "__main__":
    unittest.main()
