from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import ValidationError

from forge_core.change_management import (
    ArtifactDomain,
    ConnectorSnapshot,
    EvidenceTier,
    ExternalArtifactRef,
    ReleaseStatus,
    SourceSystem,
)
from forge_core.constraints import (
    BOMLine,
    InterfaceContract,
    InterfaceSignal,
    QuoteSnapshot,
    evaluate_cost,
)
from forge_core.hashing import canonical_sha256
from forge_core.impact_engine import (
    ChangeImpactAssessment,
    NormalizedInterfaceProjection,
    analyze_change,
    interface_projection_hash,
)
from forge_core.models import Quantity, SourceRef, Verdict
from forge_core.persistence import StoredCostEvaluation
from forge_core.release_readiness import (
    DEFAULT_RELEASE_READINESS_POLICY,
    EvidenceRejectionReason,
    FirmwareBuildEvidence,
    ReleaseReadinessDecision,
    ReleaseReadinessPolicy,
    TestExecutionEvidence,
    evaluate_release_readiness,
)

NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)
HASH = "sha256:" + "a" * 64
BUILD_HASH = "sha256:" + "b" * 64


class ReleaseReadinessPolicyTests(unittest.TestCase):
    def test_cost_currency_requires_three_uppercase_ascii_letters(self) -> None:
        payload = DEFAULT_RELEASE_READINESS_POLICY.model_dump(mode="python")
        for currency in ("U", "USDD", "usd", "U1D"):
            with self.subTest(currency=currency), self.assertRaises(ValidationError):
                ReleaseReadinessPolicy.model_validate(
                    payload | {"cost_currency": currency}
                )


def artifact(
    artifact_id: str,
    domain: ArtifactDomain,
    source_system: SourceSystem,
    revision: str,
    digit: int,
    *,
    captured_at: datetime,
) -> ExternalArtifactRef:
    return ExternalArtifactRef(
        artifact_id=artifact_id,
        domain=domain,
        source_system=source_system,
        source_revision=revision,
        content_hash="sha256:" + str(digit) * 64,
        captured_at=captured_at,
    )


def interface_contract(contract_id: str = "controller") -> InterfaceContract:
    source = SourceRef(kind="user", identifier="release-test")
    return InterfaceContract(
        contract_id=contract_id,
        protocol_schema_hash="sha256:" + "9" * 64,
        signals=(
            InterfaceSignal(
                name="fan_enable",
                pin="J3-4",
                direction="output",
                voltage_min=Quantity(
                    value=0, unit="V", dimension="voltage", source=source
                ),
                voltage_max=Quantity(
                    value=3.3, unit="V", dimension="voltage", source=source
                ),
                command_min=Quantity(
                    value=0, unit="%", dimension="duty", source=source
                ),
                command_max=Quantity(
                    value=100, unit="%", dimension="duty", source=source
                ),
                safe_value=Quantity(value=0, unit="%", dimension="duty", source=source),
            ),
        ),
    )


def projection(
    snapshot_id: str, source: ExternalArtifactRef, contract: InterfaceContract
) -> NormalizedInterfaceProjection:
    return NormalizedInterfaceProjection(
        snapshot_id=snapshot_id,
        source_ref=source,
        normalizer_version="1.0.0",
        contract=contract,
        projection_hash=interface_projection_hash(
            snapshot_id, source, "1.0.0", contract
        ),
    )


def release_context(
    *, hardware_change: bool = False, bind_protocol: bool = True
) -> tuple[ChangeImpactAssessment, ConnectorSnapshot]:
    previous_at = NOW - timedelta(hours=2)
    current_at = NOW - timedelta(hours=1)
    old_hw = artifact(
        "controller-board",
        ArtifactDomain.HARDWARE,
        SourceSystem.PLM,
        "11",
        1,
        captured_at=previous_at,
    )
    new_hw = (
        artifact(
            "controller-board",
            ArtifactDomain.HARDWARE,
            SourceSystem.PLM,
            "12",
            2,
            captured_at=current_at,
        )
        if hardware_change
        else old_hw
    )
    firmware = artifact(
        "controller-firmware",
        ArtifactDomain.FIRMWARE,
        SourceSystem.GIT,
        "commit-42",
        3,
        captured_at=previous_at,
    )
    bom = artifact(
        "controller-bom",
        ArtifactDomain.BOM,
        SourceSystem.PLM,
        "bom-12",
        4,
        captured_at=previous_at,
    )
    protocol = artifact(
        "controller-protocol",
        ArtifactDomain.PROTOCOL,
        SourceSystem.GIT,
        "protocol-3",
        5,
        captured_at=previous_at,
    )
    old_doc = artifact(
        "release-notes",
        ArtifactDomain.DOCUMENTATION,
        SourceSystem.GIT,
        "doc-1",
        6,
        captured_at=previous_at,
    )
    new_doc = (
        old_doc
        if hardware_change
        else artifact(
            "release-notes",
            ArtifactDomain.DOCUMENTATION,
            SourceSystem.GIT,
            "doc-2",
            7,
            captured_at=current_at,
        )
    )
    previous = ConnectorSnapshot(
        snapshot_id="snapshot-11",
        project_id="project-1",
        hardware_revision_id="HW-11" if hardware_change else "HW-12",
        captured_at=previous_at,
        artifacts=(old_hw, firmware, bom, protocol, old_doc),
    )
    current = ConnectorSnapshot(
        snapshot_id="snapshot-12",
        project_id="project-1",
        hardware_revision_id="HW-12",
        captured_at=current_at,
        artifacts=(new_hw, firmware, bom, protocol, new_doc),
    )
    contract = interface_contract()
    projections: tuple[NormalizedInterfaceProjection, ...] = (
        (projection(current.snapshot_id, protocol, contract),) if bind_protocol else ()
    )
    if hardware_change:
        projections = (
            projection(previous.snapshot_id, old_hw, contract),
            projection(current.snapshot_id, new_hw, contract),
            projection(current.snapshot_id, firmware, contract),
            projection(current.snapshot_id, protocol, contract),
        )
    return analyze_change(previous, current, interface_projections=projections), current


def cost_record(
    snapshot: ConnectorSnapshot,
    *,
    expires_at: datetime = NOW + timedelta(days=1),
    supplier: str = "Supplier A",
    source_url: str = "https://supplier.example/FAN-120",
) -> StoredCostEvaluation:
    bom = (BOMLine(part_number="FAN-120", quantity=2),)
    quote = QuoteSnapshot(
        quote_id="quote-1",
        part_number="FAN-120",
        supplier=supplier,
        region="KR",
        currency="USD",
        unit_price=Decimal("10.00"),
        minimum_quantity=1,
        observed_at=NOW - timedelta(hours=2),
        expires_at=expires_at,
        shipping_included=True,
        shipping_cost=None,
        tax_included=True,
        tax_cost=None,
        source_url=source_url,
        source_hash="sha256:" + "c" * 64,
    )
    evaluated_at = NOW - timedelta(hours=1)
    evaluation = evaluate_cost(
        bom,
        (quote,),
        currency="USD",
        budget_limit=Decimal("30.00"),
        reserve_rate=Decimal("0.10"),
        evaluated_at=evaluated_at,
    )
    bom_ref = next(
        item for item in snapshot.artifacts if item.domain is ArtifactDomain.BOM
    )
    return StoredCostEvaluation(
        project_id=snapshot.project_id,
        evidence_id="cost-current",
        revision_id=snapshot.hardware_revision_id,
        bom_artifact_hash=bom_ref.content_hash,
        dependency_hash="sha256:" + "d" * 64,
        bom=bom,
        quotes=(quote,),
        evaluated_at=evaluated_at,
        evaluation=evaluation,
    )


def build_evidence(
    assessment: ChangeImpactAssessment,
    snapshot: ConnectorSnapshot,
    **updates: object,
) -> FirmwareBuildEvidence:
    firmware = next(
        item for item in snapshot.artifacts if item.domain is ArtifactDomain.FIRMWARE
    )
    values: dict[str, object] = {
        "evidence_id": "build-current",
        "project_id": snapshot.project_id,
        "hardware_revision_id": snapshot.hardware_revision_id,
        "snapshot_hash": assessment.to_snapshot_hash,
        "change_analysis_hash": assessment.analysis_hash,
        "build_id": "ci-build-42",
        "firmware_artifact_id": firmware.artifact_id,
        "firmware_source_revision": firmware.source_revision,
        "firmware_source_hash": firmware.content_hash,
        "toolchain_id": "arm-none-eabi-gcc",
        "toolchain_version": "15.1",
        "toolchain_hash": "sha256:" + "e" * 64,
        "protocol_schema_hash": assessment.target_protocol_schema_hash,
        "firmware_build_artifact_hash": BUILD_HASH,
        "verdict": Verdict.PASS,
        "result_ref": "ci://builds/42",
        "started_at": NOW - timedelta(minutes=10),
        "completed_at": NOW - timedelta(minutes=5),
    }
    values.update(updates)
    return FirmwareBuildEvidence.model_validate(values)


def test_evidence(
    assessment: ChangeImpactAssessment,
    snapshot: ConnectorSnapshot,
    test_id: str,
    tier: EvidenceTier,
    **updates: object,
) -> TestExecutionEvidence:
    values: dict[str, object] = {
        "evidence_id": f"test-{test_id}-{tier.value}",
        "project_id": snapshot.project_id,
        "hardware_revision_id": snapshot.hardware_revision_id,
        "snapshot_hash": assessment.to_snapshot_hash,
        "change_analysis_hash": assessment.analysis_hash,
        "test_id": test_id,
        "tier": tier,
        "verdict": Verdict.PASS,
        "source_system": SourceSystem.CI,
        "source_revision": "ci-run-100",
        "source_hash": "sha256:" + "f" * 64,
        "result_ref": f"ci://tests/{test_id}",
        "recorded_at": NOW - timedelta(minutes=1),
        "firmware_build_artifact_hash": (
            BUILD_HASH
            if tier
            in {
                EvidenceTier.SIMULATION,
                EvidenceTier.HIL,
                EvidenceTier.PHYSICAL_DEVICE,
            }
            or test_id == "protocol-conformance"
            else None
        ),
        "fixture_id": (
            "fixture-1" if tier in {EvidenceTier.BENCH, EvidenceTier.HIL} else None
        ),
        "device_instance_id": (
            "device-1" if tier is EvidenceTier.PHYSICAL_DEVICE else None
        ),
    }
    values.update(updates)
    return TestExecutionEvidence.model_validate(values)


def required_test_evidence(
    assessment: ChangeImpactAssessment, snapshot: ConnectorSnapshot
) -> tuple[TestExecutionEvidence, ...]:
    return tuple(
        test_evidence(
            assessment, snapshot, requirement.test_id, requirement.required_tier
        )
        for requirement in assessment.required_retests
        if requirement.test_id not in {"firmware-build", "bom-provenance-validation"}
    )


class ReleaseReadinessServiceTests(unittest.TestCase):
    def test_ready_is_fully_bound_reproducible_and_human_readable(self) -> None:
        assessment, snapshot = release_context()
        cost = cost_record(snapshot)
        build = build_evidence(assessment, snapshot)
        results = required_test_evidence(assessment, snapshot)

        first = evaluate_release_readiness(
            assessment,
            snapshot,
            cost_evaluation=cost,
            firmware_builds=(build,),
            test_results=results,
            evaluated_at=NOW,
        )
        second = evaluate_release_readiness(
            assessment,
            snapshot,
            cost_evaluation=cost,
            firmware_builds=(build,),
            test_results=tuple(reversed(results)),
            evaluated_at=NOW,
        )

        self.assertEqual(first, second)
        self.assertEqual(first.report.status, ReleaseStatus.READY)
        self.assertEqual(first.report.blocker_codes, ())
        self.assertTrue(
            all(item.status.value == "passed" for item in first.report.required_retests)
        )
        self.assertIn(cost.quotes[0].source_hash, first.human_readable_report)
        self.assertIn(first.report.change_analysis_hash, first.human_readable_report)
        self.assertIn(build.toolchain_hash, first.human_readable_report)
        self.assertIn(build.firmware_build_artifact_hash, first.human_readable_report)
        self.assertIn("not regulatory certification", first.human_readable_report)

    def test_missing_baselines_or_required_test_always_blocks(self) -> None:
        assessment, snapshot = release_context()
        cost = cost_record(snapshot)
        build = build_evidence(assessment, snapshot)
        results = required_test_evidence(assessment, snapshot)
        cases = (
            (None, (build,), results, "bom-provenance-validation"),
            (cost, (), results, "firmware-build"),
            (cost, (build,), (), "documentation-review"),
        )
        for case_cost, builds, tests, missing_test_id in cases:
            with self.subTest(missing=missing_test_id):
                decision = evaluate_release_readiness(
                    assessment,
                    snapshot,
                    cost_evaluation=case_cost,
                    firmware_builds=builds,
                    test_results=tests,
                    evaluated_at=NOW,
                )
                self.assertEqual(decision.report.status, ReleaseStatus.BLOCKED)
                self.assertTrue(
                    any(
                        code.startswith(f"retest:{missing_test_id}:")
                        for code in decision.report.blocker_codes
                    )
                )

    def test_wrong_target_tier_future_and_stale_evidence_are_rejected(self) -> None:
        assessment, snapshot = release_context()
        cost = cost_record(snapshot)
        build = build_evidence(assessment, snapshot)
        base = required_test_evidence(assessment, snapshot)[0]
        wrong_revision = base.model_copy(
            update={
                "evidence_id": "wrong-revision",
                "hardware_revision_id": "HW-11",
            }
        )
        wrong_tier = base.model_copy(
            update={
                "evidence_id": "wrong-tier",
                "tier": EvidenceTier.SIMULATION,
                "firmware_build_artifact_hash": BUILD_HASH,
            }
        )
        future = base.model_copy(
            update={
                "evidence_id": "future",
                "recorded_at": NOW + timedelta(seconds=1),
            }
        )
        stale = base.model_copy(
            update={
                "evidence_id": "stale",
                "recorded_at": NOW - timedelta(days=31),
            }
        )
        decision = evaluate_release_readiness(
            assessment,
            snapshot,
            cost_evaluation=cost,
            firmware_builds=(build,),
            test_results=(wrong_revision, wrong_tier, future, stale),
            evaluated_at=NOW,
        )
        reasons = {item.evidence_id: item.reason for item in decision.rejected_evidence}
        self.assertEqual(
            reasons["wrong-revision"],
            EvidenceRejectionReason.WRONG_HARDWARE_REVISION,
        )
        self.assertEqual(reasons["wrong-tier"], EvidenceRejectionReason.NOT_REQUIRED)
        self.assertEqual(reasons["future"], EvidenceRejectionReason.FUTURE_TIMESTAMP)
        self.assertEqual(
            reasons["stale"], EvidenceRejectionReason.BEFORE_TARGET_SNAPSHOT
        )
        self.assertEqual(decision.report.status, ReleaseStatus.BLOCKED)

    def test_cost_is_recomputed_at_release_time_and_future_quotes_fail_closed(
        self,
    ) -> None:
        assessment, snapshot = release_context()
        expired = cost_record(snapshot, expires_at=NOW)
        decision = evaluate_release_readiness(
            assessment,
            snapshot,
            cost_evaluation=expired,
            firmware_builds=(build_evidence(assessment, snapshot),),
            test_results=required_test_evidence(assessment, snapshot),
            evaluated_at=NOW,
        )
        self.assertIn("finding:bom_cost_indeterminate", decision.report.blocker_codes)
        self.assertIn("expired_quote:FAN-120", decision.human_readable_report)

        quote = expired.quotes[0].model_copy(
            update={
                "observed_at": NOW + timedelta(minutes=1),
                "expires_at": NOW + timedelta(days=1),
            }
        )
        future = evaluate_cost(
            expired.bom,
            (quote,),
            currency="USD",
            budget_limit=Decimal("30"),
            reserve_rate=Decimal("0.1"),
            evaluated_at=NOW,
        )
        self.assertEqual(future.verdict, Verdict.INDETERMINATE)
        self.assertEqual(future.reasons, ("future_quote:FAN-120",))

    def test_cost_evidence_cannot_choose_its_release_budget_policy(self) -> None:
        assessment, snapshot = release_context()
        cost = cost_record(snapshot)
        inflated = evaluate_cost(
            cost.bom,
            cost.quotes,
            currency="USD",
            budget_limit=Decimal("999999"),
            reserve_rate=Decimal("0"),
            evaluated_at=cost.evaluated_at,
        )
        malicious = cost.model_copy(
            update={
                "dependency_hash": "sha256:" + "0" * 64,
                "evaluation": inflated,
            }
        )
        decision = evaluate_release_readiness(
            assessment,
            snapshot,
            cost_evaluation=malicious,
            firmware_builds=(build_evidence(assessment, snapshot),),
            test_results=required_test_evidence(assessment, snapshot),
            evaluated_at=NOW,
        )
        self.assertEqual(decision.report.status, ReleaseStatus.BLOCKED)
        self.assertIn("finding:bom_cost_policy_mismatch", decision.report.blocker_codes)
        self.assertIn(
            EvidenceRejectionReason.COST_POLICY_MISMATCH,
            {item.reason for item in decision.rejected_evidence},
        )

        scale_changed = cost.model_copy(
            update={
                "evaluation": evaluate_cost(
                    cost.bom,
                    cost.quotes,
                    currency="USD",
                    budget_limit=Decimal("30"),
                    reserve_rate=Decimal("0.10"),
                    evaluated_at=cost.evaluated_at,
                )
            }
        )
        scale_decision = evaluate_release_readiness(
            assessment,
            snapshot,
            cost_evaluation=scale_changed,
            firmware_builds=(build_evidence(assessment, snapshot),),
            test_results=required_test_evidence(assessment, snapshot),
            evaluated_at=NOW,
        )
        self.assertEqual(scale_decision.report.status, ReleaseStatus.BLOCKED)
        self.assertIn(
            "finding:bom_cost_policy_mismatch",
            scale_decision.report.blocker_codes,
        )

    def test_runtime_results_require_the_selected_firmware_artifact(self) -> None:
        assessment, snapshot = release_context(hardware_change=True)
        cost = cost_record(snapshot)
        build = build_evidence(assessment, snapshot)
        results = list(required_test_evidence(assessment, snapshot))
        ready = evaluate_release_readiness(
            assessment,
            snapshot,
            cost_evaluation=cost,
            firmware_builds=(build,),
            test_results=results,
            evaluated_at=NOW,
        )
        self.assertEqual(ready.report.status, ReleaseStatus.READY)

        hil_index = next(
            index for index, item in enumerate(results) if item.tier is EvidenceTier.HIL
        )
        results[hil_index] = results[hil_index].model_copy(
            update={"firmware_build_artifact_hash": HASH}
        )
        blocked = evaluate_release_readiness(
            assessment,
            snapshot,
            cost_evaluation=cost,
            firmware_builds=(build,),
            test_results=results,
            evaluated_at=NOW,
        )
        self.assertEqual(blocked.report.status, ReleaseStatus.BLOCKED)
        self.assertEqual(
            next(
                item.reason
                for item in blocked.rejected_evidence
                if item.evidence_id == results[hil_index].evidence_id
            ),
            EvidenceRejectionReason.FIRMWARE_ARTIFACT_MISMATCH,
        )

    def test_firmware_build_requires_the_target_protocol_schema(self) -> None:
        assessment, snapshot = release_context(hardware_change=True)
        self.assertEqual(
            assessment.target_protocol_schema_hash,
            interface_contract().protocol_schema_hash,
        )
        wrong_build = build_evidence(
            assessment,
            snapshot,
            protocol_schema_hash="sha256:" + "0" * 64,
        )
        decision = evaluate_release_readiness(
            assessment,
            snapshot,
            cost_evaluation=cost_record(snapshot),
            firmware_builds=(wrong_build,),
            test_results=required_test_evidence(assessment, snapshot),
            evaluated_at=NOW,
        )

        self.assertEqual(decision.report.status, ReleaseStatus.BLOCKED)
        self.assertEqual(
            next(
                item.reason
                for item in decision.rejected_evidence
                if item.evidence_id == wrong_build.evidence_id
            ),
            EvidenceRejectionReason.PROTOCOL_SCHEMA_MISMATCH,
        )

    def test_missing_target_protocol_projection_fails_closed(self) -> None:
        assessment, snapshot = release_context(bind_protocol=False)
        build = build_evidence(
            assessment,
            snapshot,
            protocol_schema_hash=interface_contract().protocol_schema_hash,
        )
        decision = evaluate_release_readiness(
            assessment,
            snapshot,
            cost_evaluation=cost_record(snapshot),
            firmware_builds=(build,),
            test_results=required_test_evidence(assessment, snapshot),
            evaluated_at=NOW,
        )

        self.assertEqual(decision.report.status, ReleaseStatus.BLOCKED)
        self.assertIn(
            "finding:protocol_schema_evidence_missing",
            decision.report.blocker_codes,
        )
        self.assertEqual(
            next(
                item.reason
                for item in decision.rejected_evidence
                if item.evidence_id == build.evidence_id
            ),
            EvidenceRejectionReason.PROTOCOL_SCHEMA_TARGET_MISSING,
        )

    def test_latest_failure_wins_and_equal_time_is_ambiguous(self) -> None:
        assessment, snapshot = release_context()
        cost = cost_record(snapshot)
        build = build_evidence(assessment, snapshot)
        original = required_test_evidence(assessment, snapshot)[0]
        older = original.model_copy(
            update={
                "evidence_id": "doc-old-pass",
                "recorded_at": NOW - timedelta(minutes=2),
            }
        )
        latest_fail = original.model_copy(
            update={"evidence_id": "doc-new-fail", "verdict": Verdict.FAIL}
        )
        failed = evaluate_release_readiness(
            assessment,
            snapshot,
            cost_evaluation=cost,
            firmware_builds=(build,),
            test_results=(older, latest_fail),
            evaluated_at=NOW,
        )
        self.assertIn(
            "retest:documentation-review:static:failed",
            failed.report.blocker_codes,
        )
        self.assertIn(
            EvidenceRejectionReason.SUPERSEDED,
            {item.reason for item in failed.rejected_evidence},
        )

        same_time = latest_fail.model_copy(
            update={"evidence_id": "doc-same-time-pass", "verdict": Verdict.PASS}
        )
        ambiguous = evaluate_release_readiness(
            assessment,
            snapshot,
            cost_evaluation=cost,
            firmware_builds=(build,),
            test_results=(latest_fail, same_time),
            evaluated_at=NOW,
        )
        self.assertIn(
            "retest:documentation-review:static:missing",
            ambiguous.report.blocker_codes,
        )
        self.assertEqual(
            {item.reason for item in ambiguous.rejected_evidence},
            {EvidenceRejectionReason.AMBIGUOUS_LATEST_TIMESTAMP},
        )

    def test_decision_tampering_and_hostile_report_text_are_rejected_or_escaped(
        self,
    ) -> None:
        assessment, snapshot = release_context()
        cost = cost_record(
            snapshot,
            supplier="<script>alert(1)</script>",
            source_url="javascript:<script>alert(2)</script>",
        )
        decision = evaluate_release_readiness(
            assessment,
            snapshot,
            cost_evaluation=cost,
            firmware_builds=(build_evidence(assessment, snapshot),),
            test_results=required_test_evidence(assessment, snapshot),
            evaluated_at=NOW,
        )
        self.assertNotIn("<script>", decision.human_readable_report)
        self.assertIn("&lt;script&gt;", decision.human_readable_report)
        with self.assertRaises(ValidationError):
            ReleaseReadinessDecision.model_validate(
                decision.model_dump(mode="python")
                | {"decision_hash": "sha256:" + "0" * 64}
            )
        with self.assertRaises(ValidationError):
            ReleaseReadinessDecision.model_validate(
                decision.model_dump(mode="python") | {"human_readable_report": "READY"}
            )
        assert decision.selected_firmware_build is not None
        tampered_build = decision.selected_firmware_build.model_copy(
            update={"protocol_schema_hash": "sha256:" + "0" * 64}
        )
        with self.assertRaises(ValidationError) as protocol_error:
            ReleaseReadinessDecision.model_validate(
                decision.model_dump(mode="python")
                | {"selected_firmware_build": tampered_build}
            )
        self.assertIn("protocol schema is incorrect", str(protocol_error.exception))

        assert decision.cost_evaluation is not None
        tampered_cost = decision.cost_evaluation.model_copy(
            update={"bom_artifact_hash": "sha256:" + "0" * 64}
        )
        with self.assertRaises(ValidationError) as bom_error:
            ReleaseReadinessDecision.model_validate(
                decision.model_dump(mode="python")
                | {
                    "cost_evaluation": tampered_cost,
                    "cost_evaluation_hash": canonical_sha256(tampered_cost),
                }
            )
        self.assertIn("BOM is not in target snapshot", str(bom_error.exception))


if __name__ == "__main__":
    unittest.main()
