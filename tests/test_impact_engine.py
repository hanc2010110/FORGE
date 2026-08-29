from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from forge_core.change_management import (
    ArtifactDomain,
    ConnectorSnapshot,
    EvidenceTier,
    ExternalArtifactRef,
    SourceSystem,
)
from forge_core.constraints import InterfaceContract, InterfaceSignal
from forge_core.impact_engine import (
    DEFAULT_CHANGE_IMPACT_POLICY,
    ArtifactChange,
    ArtifactChangeType,
    ChangeFacet,
    ChangeImpactPolicy,
    NormalizedInterfaceProjection,
    RetestRule,
    analyze_change,
    detect_artifact_changes,
    interface_projection_hash,
    validate_interface_compatibility,
)
from forge_core.models import Quantity, SourceRef

NOW = datetime(2026, 8, 29, tzinfo=UTC)


def sha(seed: int) -> str:
    return f"sha256:{seed:064x}"


def quantity(value: float, unit: str, dimension: str) -> Quantity:
    return Quantity(
        value=value,
        unit=unit,
        dimension=dimension,
        source=SourceRef(kind="user", identifier="test:interface"),
    )


def interface_signal(**updates: object) -> InterfaceSignal:
    values: dict[str, object] = {
        "name": "fan_enable",
        "pin": "J3-4",
        "direction": "output",
        "voltage_min": quantity(0, "V", "voltage"),
        "voltage_max": quantity(3.3, "V", "voltage"),
        "command_min": quantity(0, "%", "ratio"),
        "command_max": quantity(100, "%", "ratio"),
        "safe_value": quantity(0, "%", "ratio"),
    }
    values.update(updates)
    return InterfaceSignal.model_validate(values)


def interface_contract(
    contract_id: str,
    *,
    protocol_seed: int = 90,
    signal: InterfaceSignal | None = None,
) -> InterfaceContract:
    return InterfaceContract(
        contract_id=contract_id,
        protocol_schema_hash=sha(protocol_seed),
        signals=(signal or interface_signal(),),
    )


def artifact(
    artifact_id: str,
    domain: ArtifactDomain,
    source_system: SourceSystem,
    revision: str,
    seed: int,
) -> ExternalArtifactRef:
    return ExternalArtifactRef(
        artifact_id=artifact_id,
        domain=domain,
        source_system=source_system,
        source_revision=revision,
        content_hash=sha(seed),
        captured_at=NOW,
    )


def snapshot(
    snapshot_id: str,
    revision_id: str,
    artifacts: tuple[ExternalArtifactRef, ...],
    *,
    captured_at: datetime = NOW,
) -> ConnectorSnapshot:
    return ConnectorSnapshot(
        snapshot_id=snapshot_id,
        project_id="project-1",
        hardware_revision_id=revision_id,
        captured_at=captured_at,
        artifacts=artifacts,
    )


def projection(
    snapshot_id: str,
    source_ref: ExternalArtifactRef,
    contract: InterfaceContract,
) -> NormalizedInterfaceProjection:
    normalizer_version = "1.0.0"
    return NormalizedInterfaceProjection(
        snapshot_id=snapshot_id,
        source_ref=source_ref,
        normalizer_version=normalizer_version,
        contract=contract,
        projection_hash=interface_projection_hash(
            snapshot_id,
            source_ref,
            normalizer_version,
            contract,
        ),
    )


class ChangeImpactEngineTests(unittest.TestCase):
    def test_artifact_add_remove_and_invalid_snapshot_order(self) -> None:
        kept = artifact(
            "controller-board", ArtifactDomain.HARDWARE, SourceSystem.PLM, "11", 1
        )
        removed = artifact(
            "pinout", ArtifactDomain.DOCUMENTATION, SourceSystem.GIT, "a1", 2
        )
        added = artifact("firmware", ArtifactDomain.FIRMWARE, SourceSystem.GIT, "f1", 3)
        previous = snapshot("snapshot-11", "HW-11", (kept, removed))
        current = snapshot("snapshot-12", "HW-11", (kept, added))

        changes = detect_artifact_changes(previous, current)

        self.assertEqual(
            {item.change_type for item in changes},
            {ArtifactChangeType.ADDED, ArtifactChangeType.REMOVED},
        )
        self.assertEqual(
            {item.facets[0] for item in changes},
            {ChangeFacet.ARTIFACT_ADDED, ChangeFacet.ARTIFACT_REMOVED},
        )
        with self.assertRaises(ValueError):
            detect_artifact_changes(previous, previous)
        with self.assertRaises(ValueError):
            detect_artifact_changes(
                previous,
                current.model_copy(update={"project_id": "other-project"}),
            )
        with self.assertRaises(ValueError):
            detect_artifact_changes(
                previous,
                current.model_copy(update={"captured_at": NOW - timedelta(seconds=1)}),
            )

    def test_artifact_change_shape_is_fail_closed(self) -> None:
        item = artifact(
            "controller-board", ArtifactDomain.HARDWARE, SourceSystem.PLM, "11", 1
        )
        common = {
            "change_id": "change-1",
            "domain": ArtifactDomain.HARDWARE,
            "facets": (ChangeFacet.OPAQUE_CONTENT,),
            "changed_paths": ("content",),
        }
        invalid = (
            {
                **common,
                "change_type": ArtifactChangeType.ADDED,
                "before": item,
                "after": item,
            },
            {
                **common,
                "change_type": ArtifactChangeType.REMOVED,
                "before": None,
            },
            {
                **common,
                "change_type": ArtifactChangeType.MODIFIED,
                "before": item,
                "after": None,
            },
            {
                **common,
                "change_type": ArtifactChangeType.MODIFIED,
                "before": item,
                "after": item.model_copy(update={"artifact_id": "other"}),
            },
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                ArtifactChange.model_validate(values)

    def test_identical_content_with_new_capture_time_has_no_impact(self) -> None:
        hardware = artifact(
            "controller-board", ArtifactDomain.HARDWARE, SourceSystem.PLM, "11", 1
        )
        previous = snapshot("snapshot-11", "HW-11", (hardware,))
        current = snapshot(
            "snapshot-12",
            "HW-11",
            (hardware,),
            captured_at=NOW + timedelta(minutes=1),
        )

        result = analyze_change(previous, current)

        self.assertEqual(result.artifact_changes, ())
        self.assertIsNone(result.impact)
        self.assertEqual(result.findings, ())
        self.assertEqual(result.required_retests, ())

    def test_hardware_revision_identifier_change_is_conservative(self) -> None:
        hardware = artifact(
            "controller-board", ArtifactDomain.HARDWARE, SourceSystem.PLM, "11", 1
        )

        result = analyze_change(
            snapshot("snapshot-11", "HW-11", (hardware,)),
            snapshot("snapshot-12", "HW-12", (hardware,)),
        )

        assert result.impact is not None
        self.assertEqual(result.impact.changed_domains, (ArtifactDomain.HARDWARE,))
        self.assertIn(
            ChangeFacet.REVISION_IDENTIFIER, result.artifact_changes[0].facets
        )
        self.assertIn(
            EvidenceTier.HIL,
            {item.required_tier for item in result.required_retests},
        )

    def test_hardware_source_revision_only_change_still_requires_runtime_retests(
        self,
    ) -> None:
        old = artifact(
            "controller-board", ArtifactDomain.HARDWARE, SourceSystem.PLM, "11", 1
        )
        new = old.model_copy(update={"source_revision": "12"})

        result = analyze_change(
            snapshot("snapshot-11", "HW-11", (old,)),
            snapshot("snapshot-12", "HW-12", (new,)),
        )

        self.assertIn(ChangeFacet.SOURCE_REVISION, result.artifact_changes[0].facets)
        self.assertTrue(
            {EvidenceTier.HIL, EvidenceTier.PHYSICAL_DEVICE}.issubset(
                {item.required_tier for item in result.required_retests}
            )
        )

    def test_opaque_hardware_change_fails_closed_with_full_retests(self) -> None:
        old = artifact(
            "controller-board", ArtifactDomain.HARDWARE, SourceSystem.PLM, "11", 1
        )
        new = artifact(
            "controller-board", ArtifactDomain.HARDWARE, SourceSystem.PLM, "12", 2
        )

        result = analyze_change(
            snapshot("snapshot-11", "HW-11", (old,)),
            snapshot("snapshot-12", "HW-12", (new,)),
        )

        assert result.impact is not None
        self.assertEqual(set(result.impact.affected_domains), set(ArtifactDomain))
        self.assertIn(ChangeFacet.OPAQUE_CONTENT, result.artifact_changes[0].facets)
        self.assertEqual(
            {item.required_tier for item in result.required_retests},
            {
                EvidenceTier.STATIC,
                EvidenceTier.BENCH,
                EvidenceTier.HIL,
                EvidenceTier.PHYSICAL_DEVICE,
            },
        )
        self.assertEqual(
            {item.rule_id for item in result.findings},
            {"interface_contract_missing"},
        )
        self.assertIn(
            "finding:interface_contract_missing:protocol",
            {item.finding_id for item in result.findings},
        )

    def test_structured_pin_change_assigns_specific_retests(self) -> None:
        old_hw = artifact(
            "controller-board", ArtifactDomain.HARDWARE, SourceSystem.PLM, "11", 1
        )
        new_hw = artifact(
            "controller-board", ArtifactDomain.HARDWARE, SourceSystem.PLM, "12", 2
        )
        old_fw = artifact(
            "firmware-pinmap", ArtifactDomain.FIRMWARE, SourceSystem.GIT, "a1", 3
        )
        new_fw = artifact(
            "firmware-pinmap", ArtifactDomain.FIRMWARE, SourceSystem.GIT, "a2", 4
        )
        protocol_ref = artifact(
            "protocol-schema", ArtifactDomain.PROTOCOL, SourceSystem.GIT, "p1", 5
        )
        previous = snapshot("snapshot-11", "HW-11", (old_hw, old_fw, protocol_ref))
        current = snapshot("snapshot-12", "HW-12", (new_hw, new_fw, protocol_ref))
        old_pin = interface_contract("old", signal=interface_signal(pin="J3-4"))
        new_hardware = interface_contract(
            "hardware-current", signal=interface_signal(pin="J3-7")
        )
        new_firmware = interface_contract(
            "firmware-current", signal=interface_signal(pin="J3-7")
        )

        result = analyze_change(
            previous,
            current,
            interface_projections=(
                projection("snapshot-11", old_hw, old_pin),
                projection("snapshot-11", old_fw, old_pin),
                projection("snapshot-12", new_hw, new_hardware),
                projection("snapshot-12", new_fw, new_firmware),
                projection("snapshot-12", protocol_ref, new_hardware),
            ),
        )

        self.assertEqual(result.findings, ())
        self.assertTrue(
            all(
                ChangeFacet.PIN_ASSIGNMENT in change.facets
                for change in result.artifact_changes
            )
        )
        test_ids = {item.test_id for item in result.required_retests}
        self.assertTrue(
            {
                "firmware-build",
                "interface-contract-validation",
                "bench-electrical",
                "hil-regression",
                "physical-device-smoke",
                "documentation-review",
            }.issubset(test_ids)
        )

    def test_stale_firmware_pin_is_a_source_bound_blocker(self) -> None:
        old_hw = artifact(
            "controller-board", ArtifactDomain.HARDWARE, SourceSystem.PLM, "11", 1
        )
        new_hw = artifact(
            "controller-board", ArtifactDomain.HARDWARE, SourceSystem.PLM, "12", 2
        )
        firmware = artifact(
            "firmware-pinmap", ArtifactDomain.FIRMWARE, SourceSystem.GIT, "a1", 3
        )
        previous = snapshot("snapshot-11", "HW-11", (old_hw, firmware))
        current = snapshot("snapshot-12", "HW-12", (new_hw, firmware))
        old_contract = interface_contract(
            "firmware", signal=interface_signal(pin="J3-4")
        )
        new_contract = interface_contract(
            "hardware", signal=interface_signal(pin="J3-7")
        )

        result = analyze_change(
            previous,
            current,
            interface_projections=(
                projection("snapshot-11", old_hw, old_contract),
                projection("snapshot-12", new_hw, new_contract),
                projection("snapshot-12", firmware, old_contract),
            ),
        )

        finding = next(
            item for item in result.findings if item.rule_id == "pin_mismatch"
        )
        self.assertEqual(len(finding.evidence_refs), 2)
        self.assertTrue(all("sha256:" in item for item in finding.evidence_refs))
        bench = next(
            item
            for item in result.required_retests
            if item.test_id == "bench-electrical"
        )
        self.assertIn("finding:pin_mismatch", bench.reason_codes)

    def test_cross_contract_voltage_unit_command_and_schema_mismatches(self) -> None:
        hardware_ref = artifact(
            "controller-board", ArtifactDomain.HARDWARE, SourceSystem.PLM, "12", 2
        )
        firmware_ref = artifact(
            "firmware-pinmap", ArtifactDomain.FIRMWARE, SourceSystem.GIT, "a2", 4
        )
        previous = snapshot("snapshot-11", "HW-11", (hardware_ref, firmware_ref))
        changed_hardware = hardware_ref.model_copy(
            update={"source_revision": "13", "content_hash": sha(5)}
        )
        current = snapshot("snapshot-12", "HW-12", (changed_hardware, firmware_ref))
        hardware_contract = interface_contract("hardware", protocol_seed=90)
        firmware_contract = interface_contract(
            "firmware",
            protocol_seed=91,
            signal=interface_signal(
                voltage_min=quantity(0, "mV", "voltage"),
                voltage_max=quantity(3300, "mV", "voltage"),
                command_min=quantity(0, "rpm", "angular_speed"),
                command_max=quantity(100, "rpm", "angular_speed"),
                safe_value=quantity(0, "rpm", "angular_speed"),
            ),
        )

        result = analyze_change(
            previous,
            current,
            interface_projections=(
                projection("snapshot-12", changed_hardware, hardware_contract),
                projection("snapshot-12", firmware_ref, firmware_contract),
            ),
        )

        rules = {item.rule_id for item in result.findings}
        self.assertTrue(
            {
                "protocol_schema_mismatch",
                "voltage_unit_mismatch",
                "command_unit_mismatch",
                "safe_value_mismatch",
            }.issubset(rules)
        )

    def test_cross_contract_signal_range_direction_and_presence_findings(self) -> None:
        expected = interface_contract("hardware")
        mismatched = interface_contract(
            "firmware",
            signal=interface_signal(
                name="other_signal",
                direction="input",
                voltage_max=quantity(5, "V", "voltage"),
                command_max=quantity(80, "%", "ratio"),
                safe_value=quantity(10, "%", "ratio"),
            ),
        )

        missing_rules = {
            item.rule_id
            for item in validate_interface_compatibility(expected, mismatched)
        }
        self.assertEqual(
            missing_rules,
            {"signal_missing_in_firmware", "signal_missing_in_hardware"},
        )

        same_name = mismatched.model_copy(
            update={
                "signals": (
                    mismatched.signals[0].model_copy(update={"name": "fan_enable"}),
                )
            }
        )
        mismatch_rules = {
            item.rule_id
            for item in validate_interface_compatibility(expected, same_name)
        }
        self.assertTrue(
            {
                "signal_direction_mismatch",
                "voltage_range_mismatch",
                "command_range_mismatch",
                "safe_value_mismatch",
            }.issubset(mismatch_rules)
        )

    def test_structured_change_facets_cover_voltage_command_safe_and_schema(
        self,
    ) -> None:
        old_hw = artifact(
            "controller-board", ArtifactDomain.HARDWARE, SourceSystem.PLM, "11", 1
        )
        new_hw = artifact(
            "controller-board", ArtifactDomain.HARDWARE, SourceSystem.PLM, "12", 2
        )
        firmware = artifact(
            "firmware", ArtifactDomain.FIRMWARE, SourceSystem.GIT, "f1", 3
        )
        previous = snapshot("snapshot-11", "HW-11", (old_hw, firmware))
        current = snapshot("snapshot-12", "HW-12", (new_hw, firmware))
        old_contract = interface_contract("old", protocol_seed=1)
        new_contract = interface_contract(
            "new",
            protocol_seed=2,
            signal=interface_signal(
                voltage_max=quantity(5, "V", "voltage"),
                command_max=quantity(80, "%", "ratio"),
                safe_value=quantity(10, "%", "ratio"),
            ),
        )

        result = analyze_change(
            previous,
            current,
            interface_projections=(
                projection("snapshot-11", old_hw, old_contract),
                projection("snapshot-12", new_hw, new_contract),
                projection("snapshot-12", firmware, new_contract),
            ),
        )

        hardware_change = next(
            item
            for item in result.artifact_changes
            if item.domain is ArtifactDomain.HARDWARE
        )
        self.assertTrue(
            {
                ChangeFacet.VOLTAGE_RANGE,
                ChangeFacet.COMMAND_RANGE,
                ChangeFacet.SAFE_VALUE,
                ChangeFacet.PROTOCOL_SCHEMA,
            }.issubset(hardware_change.facets)
        )
        self.assertIn(ChangeFacet.OPAQUE_CONTENT, hardware_change.facets)

    def test_documentation_only_change_does_not_trigger_runtime_retests(self) -> None:
        old = artifact(
            "pinout", ArtifactDomain.DOCUMENTATION, SourceSystem.GIT, "a1", 1
        )
        new = artifact(
            "pinout", ArtifactDomain.DOCUMENTATION, SourceSystem.GIT, "a2", 2
        )

        result = analyze_change(
            snapshot("snapshot-11", "HW-11", (old,)),
            snapshot("snapshot-12", "HW-11", (new,)),
        )

        assert result.impact is not None
        self.assertEqual(
            result.impact.affected_domains, (ArtifactDomain.DOCUMENTATION,)
        )
        self.assertEqual(
            tuple(item.test_id for item in result.required_retests),
            ("documentation-review",),
        )
        self.assertEqual(result.findings, ())

    def test_same_revision_content_change_is_integrity_blocker(self) -> None:
        old = artifact(
            "controller-board", ArtifactDomain.HARDWARE, SourceSystem.PLM, "a1", 1
        )
        new = artifact(
            "controller-board", ArtifactDomain.HARDWARE, SourceSystem.PLM, "a2", 2
        )

        result = analyze_change(
            snapshot("snapshot-11", "HW-11", (old,)),
            snapshot("snapshot-12", "HW-11", (new,)),
        )

        self.assertIn(
            "hardware_revision_reused", {item.rule_id for item in result.findings}
        )

    def test_projection_must_bind_exact_source_and_payload(self) -> None:
        hardware_ref = artifact(
            "controller-board", ArtifactDomain.HARDWARE, SourceSystem.PLM, "12", 2
        )
        contract = interface_contract("hardware")
        valid = projection("snapshot-12", hardware_ref, contract)

        with self.assertRaises(ValidationError):
            NormalizedInterfaceProjection.model_validate(
                valid.model_dump(mode="python") | {"normalizer_version": "2.0.0"}
            )

        unrelated = snapshot("snapshot-12", "HW-12", (hardware_ref,))
        with self.assertRaises(ValueError):
            analyze_change(
                snapshot("snapshot-11", "HW-11", (hardware_ref,)),
                unrelated,
                interface_projections=(
                    projection("unknown-snapshot", hardware_ref, contract),
                ),
            )

        other_ref = artifact(
            "other-board", ArtifactDomain.HARDWARE, SourceSystem.PLM, "12", 8
        )
        with self.assertRaises(ValueError):
            analyze_change(
                snapshot("snapshot-11", "HW-11", (hardware_ref,)),
                unrelated,
                interface_projections=(projection("snapshot-12", other_ref, contract),),
            )

        with self.assertRaises(ValueError):
            analyze_change(
                snapshot("snapshot-11", "HW-11", (hardware_ref,)),
                unrelated,
                interface_projections=(valid, valid),
            )

    def test_policy_rejects_missing_domains_duplicate_rules_and_empty_trigger(
        self,
    ) -> None:
        complete: dict[str, tuple[ArtifactDomain, ...]] = {
            domain.value: (domain,) for domain in ArtifactDomain
        }
        complete[ArtifactDomain.HARDWARE.value] = tuple(ArtifactDomain)
        with self.assertRaises(ValidationError):
            ChangeImpactPolicy(
                policy_id="bad",
                policy_version="1",
                domain_impacts={"hardware": (ArtifactDomain.HARDWARE,)},
                retest_rules=(),
            )
        rule = RetestRule(
            rule_id="duplicate",
            test_id="test",
            required_tier=EvidenceTier.STATIC,
            trigger_domains=(ArtifactDomain.TEST,),
            reason_code="required",
        )
        with self.assertRaises(ValidationError):
            ChangeImpactPolicy(
                policy_id="bad",
                policy_version="1",
                domain_impacts=complete,
                retest_rules=(rule, rule),
            )
        with self.assertRaises(ValidationError):
            RetestRule(
                rule_id="empty",
                test_id="test",
                required_tier=EvidenceTier.STATIC,
                reason_code="required",
            )
        with self.assertRaises(ValidationError):
            ChangeImpactPolicy(
                policy_id="incomplete",
                policy_version="1",
                domain_impacts=complete,
                retest_rules=(
                    RetestRule(
                        rule_id="domains-only",
                        test_id="test",
                        required_tier=EvidenceTier.STATIC,
                        trigger_domains=tuple(ArtifactDomain),
                        reason_code="required",
                    ),
                ),
            )
        with self.assertRaisesRegex(
            ValidationError, "hardware facet .* is missing minimum retests"
        ):
            ChangeImpactPolicy(
                policy_id="unsafe-all-covered",
                policy_version="1",
                domain_impacts=complete,
                retest_rules=(
                    RetestRule(
                        rule_id="documentation-only",
                        test_id="documentation-review",
                        required_tier=EvidenceTier.STATIC,
                        trigger_domains=tuple(ArtifactDomain),
                        trigger_facets=tuple(ChangeFacet),
                        reason_code="documentation-only-is-not-safe",
                    ),
                ),
            )

    def test_policy_and_snapshot_content_are_bound_into_analysis_hash(self) -> None:
        old = artifact(
            "pinout", ArtifactDomain.DOCUMENTATION, SourceSystem.GIT, "a1", 1
        )
        new = artifact(
            "pinout", ArtifactDomain.DOCUMENTATION, SourceSystem.GIT, "a2", 2
        )
        first = analyze_change(
            snapshot("snapshot-11", "HW-11", (old,)),
            snapshot("snapshot-12", "HW-11", (new,)),
        )
        changed_rules = tuple(
            rule.model_copy(
                update={"reason_code": "changed-policy-reason"}
                if rule.rule_id == "retest-documentation"
                else {}
            )
            for rule in DEFAULT_CHANGE_IMPACT_POLICY.retest_rules
        )
        changed_policy = ChangeImpactPolicy(
            **(
                DEFAULT_CHANGE_IMPACT_POLICY.model_dump(mode="python")
                | {"retest_rules": changed_rules}
            )
        )
        second = analyze_change(
            snapshot("snapshot-11", "HW-11", (old,)),
            snapshot("snapshot-12", "HW-11", (new,)),
            policy=changed_policy,
        )

        self.assertNotEqual(first.policy_hash, second.policy_hash)
        self.assertNotEqual(first.analysis_hash, second.analysis_hash)
        self.assertNotEqual(first.from_snapshot_hash, first.to_snapshot_hash)

    def test_protocol_projection_findings_name_protocol_domain(self) -> None:
        hardware = interface_contract("hardware")
        protocol = interface_contract(
            "protocol",
            signal=interface_signal(name="protocol_only"),
        )

        findings = validate_interface_compatibility(
            hardware,
            protocol,
            candidate_domain=ArtifactDomain.PROTOCOL,
        )

        rules = {item.rule_id for item in findings}
        self.assertIn("signal_missing_in_protocol", rules)
        self.assertTrue(
            all(
                ArtifactDomain.FIRMWARE not in item.affected_domains
                for item in findings
            )
        )

    def test_input_order_does_not_change_output_or_analysis_hash(self) -> None:
        old_hw = artifact(
            "controller-board", ArtifactDomain.HARDWARE, SourceSystem.PLM, "11", 1
        )
        new_hw = artifact(
            "controller-board", ArtifactDomain.HARDWARE, SourceSystem.PLM, "12", 2
        )
        old_doc = artifact(
            "pinout", ArtifactDomain.DOCUMENTATION, SourceSystem.GIT, "a1", 3
        )
        new_doc = artifact(
            "pinout", ArtifactDomain.DOCUMENTATION, SourceSystem.GIT, "a2", 4
        )
        firmware = artifact(
            "firmware-pinmap", ArtifactDomain.FIRMWARE, SourceSystem.GIT, "f1", 5
        )
        old_contract = interface_contract("old")
        new_contract = interface_contract("new")
        previous_a = snapshot("snapshot-11", "HW-11", (old_hw, old_doc, firmware))
        current_a = snapshot("snapshot-12", "HW-12", (new_hw, new_doc, firmware))
        previous_b = snapshot("snapshot-11", "HW-11", (firmware, old_doc, old_hw))
        current_b = snapshot("snapshot-12", "HW-12", (firmware, new_doc, new_hw))
        projections = (
            projection("snapshot-11", old_hw, old_contract),
            projection("snapshot-12", new_hw, new_contract),
            projection("snapshot-12", firmware, new_contract),
        )

        first = analyze_change(previous_a, current_a, interface_projections=projections)
        second = analyze_change(
            previous_b,
            current_b,
            interface_projections=tuple(reversed(projections)),
        )

        self.assertEqual(first, second)
        self.assertEqual(first.analysis_hash, second.analysis_hash)

    def test_duplicate_test_ids_preserve_each_exact_evidence_tier(self) -> None:
        old = artifact(
            "pinout", ArtifactDomain.DOCUMENTATION, SourceSystem.GIT, "a1", 1
        )
        new = artifact(
            "pinout", ArtifactDomain.DOCUMENTATION, SourceSystem.GIT, "a2", 2
        )
        policy = ChangeImpactPolicy(
            policy_id="merge-test",
            policy_version="1",
            domain_impacts=DEFAULT_CHANGE_IMPACT_POLICY.domain_impacts,
            retest_rules=DEFAULT_CHANGE_IMPACT_POLICY.retest_rules
            + (
                RetestRule(
                    rule_id="hil-doc",
                    test_id="documentation-review",
                    required_tier=EvidenceTier.HIL,
                    trigger_domains=(ArtifactDomain.DOCUMENTATION,),
                    reason_code="hil-required",
                ),
            ),
        )

        result = analyze_change(
            snapshot("snapshot-11", "HW-11", (old,)),
            snapshot("snapshot-12", "HW-11", (new,)),
            policy=policy,
        )

        self.assertEqual(len(result.required_retests), 2)
        self.assertEqual(
            {item.required_tier for item in result.required_retests},
            {EvidenceTier.STATIC, EvidenceTier.HIL},
        )

    def test_analysis_hash_rejects_result_tampering(self) -> None:
        old = artifact(
            "pinout", ArtifactDomain.DOCUMENTATION, SourceSystem.GIT, "a1", 1
        )
        new = artifact(
            "pinout", ArtifactDomain.DOCUMENTATION, SourceSystem.GIT, "a2", 2
        )
        result = analyze_change(
            snapshot("snapshot-11", "HW-11", (old,)),
            snapshot("snapshot-12", "HW-11", (new,)),
        )

        with self.assertRaises(ValidationError):
            result.model_validate(
                result.model_dump(mode="python") | {"policy_version": "tampered"}
            )


if __name__ == "__main__":
    unittest.main()
