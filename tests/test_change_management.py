from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from forge_core.change_management import (
    ArtifactDomain,
    ChangeImpact,
    ConnectorSnapshot,
    ConsistencyFinding,
    EvidenceTier,
    ExternalArtifactRef,
    FindingSeverity,
    ReleaseEvidence,
    ReleaseEvidenceKind,
    ReleaseReadinessReport,
    ReleaseStatus,
    RetestRequirement,
    RetestStatus,
    SourceSystem,
)
from forge_core.models import Verdict

NOW = datetime(2026, 8, 29, tzinfo=UTC)


def artifact(**updates: object) -> ExternalArtifactRef:
    values: dict[str, object] = {
        "artifact_id": "board-main",
        "domain": ArtifactDomain.HARDWARE,
        "source_system": SourceSystem.PLM,
        "source_revision": "HW-12",
        "content_hash": "sha256:" + "1" * 64,
        "captured_at": NOW,
    }
    values.update(updates)
    return ExternalArtifactRef.model_validate(values)


def evidence(**updates: object) -> ReleaseEvidence:
    values: dict[str, object] = {
        "evidence_id": "evidence-hil-1",
        "evidence_kind": ReleaseEvidenceKind.TEST_RESULT,
        "project_id": "project-1",
        "hardware_revision_id": "HW-12",
        "snapshot_hash": "sha256:" + "3" * 64,
        "change_analysis_hash": "sha256:" + "4" * 64,
        "test_id": "protocol-pinmap-hil",
        "tier": EvidenceTier.HIL,
        "verdict": Verdict.PASS,
        "source_system": SourceSystem.CI,
        "source_revision": "build-42",
        "source_hash": "sha256:" + "2" * 64,
        "result_ref": "ci://runs/42/tests/hil",
        "recorded_at": NOW,
        "fixture_id": "fixture-fan-1",
    }
    values.update(updates)
    return ReleaseEvidence.model_validate(values)


class ChangeManagementContractTests(unittest.TestCase):
    def test_connector_snapshot_is_read_only_and_source_owned(self) -> None:
        snapshot = ConnectorSnapshot(
            snapshot_id="snapshot-1",
            project_id="project-1",
            hardware_revision_id="HW-12",
            captured_at=NOW,
            artifacts=(
                artifact(),
                artifact(
                    artifact_id="firmware",
                    domain=ArtifactDomain.FIRMWARE,
                    source_system=SourceSystem.GIT,
                    source_revision="commit-abc",
                ),
            ),
        )
        self.assertTrue(snapshot.read_only)
        self.assertEqual(snapshot.artifacts[1].source_system, SourceSystem.GIT)
        with self.assertRaises(ValidationError):
            ConnectorSnapshot.model_validate(
                snapshot.model_dump(mode="python") | {"read_only": False}
            )
        with self.assertRaises(ValidationError):
            ConnectorSnapshot(
                snapshot_id="duplicate",
                project_id="project-1",
                hardware_revision_id="HW-12",
                captured_at=NOW,
                artifacts=(artifact(), artifact()),
            )
        with self.assertRaises(ValidationError):
            ConnectorSnapshot(
                snapshot_id="future",
                project_id="project-1",
                hardware_revision_id="HW-12",
                captured_at=NOW,
                artifacts=(artifact(captured_at=NOW + timedelta(seconds=1)),),
            )

    def test_change_impact_requires_reasons_for_every_affected_domain(self) -> None:
        impact = ChangeImpact(
            from_revision_id="HW-11",
            to_revision_id="HW-12",
            changed_domains=(ArtifactDomain.HARDWARE,),
            affected_domains=(
                ArtifactDomain.HARDWARE,
                ArtifactDomain.FIRMWARE,
                ArtifactDomain.TEST,
                ArtifactDomain.DOCUMENTATION,
            ),
            reasons={
                "hardware": ("pin_assignment_changed",),
                "firmware": ("gpio_mapping_may_be_stale",),
                "test": ("pin_contract_requires_retest",),
                "documentation": ("pinout_document_may_be_stale",),
            },
        )
        self.assertEqual(impact.to_revision_id, "HW-12")
        with self.assertRaises(ValidationError):
            ChangeImpact.model_validate(
                impact.model_dump(mode="python")
                | {"reasons": {"hardware": ("changed",)}}
            )

    def test_evidence_tiers_cannot_hide_physical_context(self) -> None:
        self.assertEqual(evidence().tier, EvidenceTier.HIL)
        with self.assertRaises(ValidationError):
            evidence(tier=EvidenceTier.SIMULATION, fixture_id="fixture-1")
        with self.assertRaises(ValidationError):
            evidence(
                tier=EvidenceTier.PHYSICAL_DEVICE,
                fixture_id=None,
                device_instance_id=None,
            )
        device = evidence(
            tier=EvidenceTier.PHYSICAL_DEVICE,
            fixture_id=None,
            device_instance_id="device-serial-1",
        )
        self.assertEqual(device.device_instance_id, "device-serial-1")

    def test_release_ready_requires_exact_tier_pass_evidence(self) -> None:
        hil = evidence()
        retest = RetestRequirement(
            retest_id="retest-1",
            test_id="protocol-pinmap-hil",
            required_tier=EvidenceTier.HIL,
            status=RetestStatus.PASSED,
            triggered_by=("hardware:pinout",),
            reason_codes=("pin_assignment_changed",),
            satisfied_by_evidence_id=hil.evidence_id,
        )
        report = ReleaseReadinessReport(
            report_id="report-1",
            project_id="project-1",
            hardware_revision_id="HW-12",
            snapshot_hash="sha256:" + "3" * 64,
            change_analysis_hash="sha256:" + "4" * 64,
            evaluated_at=NOW,
            status=ReleaseStatus.READY,
            findings=(),
            required_retests=(retest,),
            evidence=(hil,),
            blocker_codes=(),
        )
        self.assertEqual(report.status, ReleaseStatus.READY)

        simulation = evidence(
            evidence_id="simulation-1",
            tier=EvidenceTier.SIMULATION,
            fixture_id=None,
        )
        with self.assertRaises(ValidationError):
            ReleaseReadinessReport(
                report_id="wrong-tier",
                project_id="project-1",
                hardware_revision_id="HW-12",
                snapshot_hash="sha256:" + "3" * 64,
                change_analysis_hash="sha256:" + "4" * 64,
                evaluated_at=NOW,
                status=ReleaseStatus.READY,
                findings=(),
                required_retests=(
                    retest.model_copy(
                        update={"satisfied_by_evidence_id": simulation.evidence_id}
                    ),
                ),
                evidence=(simulation,),
                blocker_codes=(),
            )

        cost = evidence(
            evidence_id="cost-current",
            evidence_kind=ReleaseEvidenceKind.BOM_COST,
            test_id="bom-provenance-validation",
            tier=EvidenceTier.STATIC,
            source_system=SourceSystem.SUPPLIER,
            fixture_id=None,
        )
        build_retest = retest.model_copy(
            update={
                "test_id": "firmware-build",
                "required_tier": EvidenceTier.STATIC,
                "satisfied_by_evidence_id": cost.evidence_id,
            }
        )
        with self.assertRaisesRegex(ValidationError, "test ID must match exactly"):
            ReleaseReadinessReport(
                report_id="wrong-test",
                project_id="project-1",
                hardware_revision_id="HW-12",
                snapshot_hash="sha256:" + "3" * 64,
                change_analysis_hash="sha256:" + "4" * 64,
                evaluated_at=NOW,
                status=ReleaseStatus.READY,
                findings=(),
                required_retests=(build_retest,),
                evidence=(cost,),
                blocker_codes=(),
            )

    def test_blocked_release_has_complete_machine_readable_reasons(self) -> None:
        finding = ConsistencyFinding(
            finding_id="finding-1",
            rule_id="protocol.schema.compatibility",
            severity=FindingSeverity.BLOCKER,
            summary="Firmware protocol schema is older than the hardware revision.",
            affected_domains=(
                ArtifactDomain.HARDWARE,
                ArtifactDomain.FIRMWARE,
                ArtifactDomain.PROTOCOL,
            ),
            evidence_refs=("plm:HW-12", "git:commit-abc"),
        )
        retest = RetestRequirement(
            retest_id="retest-1",
            test_id="protocol-compatibility-hil",
            required_tier=EvidenceTier.HIL,
            status=RetestStatus.MISSING,
            triggered_by=("protocol:schema",),
            reason_codes=("protocol_schema_changed",),
        )
        blockers = (
            "finding:protocol.schema.compatibility",
            "retest:protocol-compatibility-hil:hil:missing",
        )
        report = ReleaseReadinessReport(
            report_id="report-blocked",
            project_id="project-1",
            hardware_revision_id="HW-12",
            snapshot_hash="sha256:" + "3" * 64,
            change_analysis_hash="sha256:" + "4" * 64,
            evaluated_at=NOW,
            status=ReleaseStatus.BLOCKED,
            findings=(finding,),
            required_retests=(retest,),
            evidence=(),
            blocker_codes=blockers,
        )
        self.assertEqual(set(report.blocker_codes), set(blockers))
        with self.assertRaises(ValidationError):
            ReleaseReadinessReport.model_validate(
                report.model_dump(mode="python") | {"status": ReleaseStatus.READY}
            )


if __name__ == "__main__":
    unittest.main()
