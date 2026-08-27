from __future__ import annotations

import unittest
from datetime import UTC, datetime

from pydantic import ValidationError

from forge_core.design import (
    ArtifactKind,
    DesignMaturity,
    EvidenceClass,
    EvidenceRecord,
    RevisionArtifact,
    SystemDesignRevision,
    assess_maturity,
    compare_revisions,
    dependency_hash,
)
from forge_core.models import Verdict


def content_hash(seed: int) -> str:
    return f"sha256:{seed:064x}"


def artifacts(**overrides: int) -> tuple[RevisionArtifact, ...]:
    seeds = {
        ArtifactKind.SYSTEM_SPEC: 1,
        ArtifactKind.MECHANICAL: 2,
        ArtifactKind.ELECTRICAL: 3,
        ArtifactKind.INTERFACE: 4,
        ArtifactKind.BOM: 5,
        ArtifactKind.BUDGET_POLICY: 6,
        ArtifactKind.SOFTWARE: 7,
        ArtifactKind.TEST_PLAN: 8,
        ArtifactKind.DOCUMENTATION: 9,
        ArtifactKind.DEVICE_PROFILE: 10,
        ArtifactKind.TOOLCHAIN: 11,
    }
    for name, seed in overrides.items():
        seeds[ArtifactKind(name)] = seed
    return tuple(
        RevisionArtifact(
            artifact_id=f"artifact:{kind.value}",
            kind=kind,
            version="1.0.0",
            content_hash=content_hash(seed),
        )
        for kind, seed in seeds.items()
    )


def revision(
    number: int = 1,
    *,
    project_id: str = "project-1",
    **overrides: int,
) -> SystemDesignRevision:
    return SystemDesignRevision(
        project_id=project_id,
        revision_id=f"revision-{number}",
        revision_number=number,
        parent_revision_id=f"revision-{number - 1}" if number > 1 else None,
        approved_at=datetime(2026, 8, 27, tzinfo=UTC),
        artifacts=artifacts(**overrides),
    )


def evidence(
    current: SystemDesignRevision,
    evidence_class: EvidenceClass,
    *,
    verdict: Verdict = Verdict.PASS,
    dependency_override: str | None = None,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=f"evidence:{evidence_class.value}",
        evidence_class=evidence_class,
        produced_for_revision_id=current.revision_id,
        dependency_hash=dependency_override or dependency_hash(current, evidence_class),
        verdict=verdict,
        evidence_refs=(f"artifact:{evidence_class.value}",),
        recorded_at=datetime(2026, 8, 27, tzinfo=UTC),
    )


class DesignRevisionTests(unittest.TestCase):
    def test_revision_requires_baseline_artifacts_and_parent(self) -> None:
        baseline = tuple(
            item for item in artifacts() if item.kind is not ArtifactKind.INTERFACE
        )
        with self.assertRaises(ValidationError):
            SystemDesignRevision(
                project_id="project-1",
                revision_id="revision-1",
                revision_number=1,
                approved_at=datetime(2026, 8, 27, tzinfo=UTC),
                artifacts=baseline,
            )

        with self.assertRaises(ValidationError):
            SystemDesignRevision(
                project_id="project-1",
                revision_id="revision-2",
                revision_number=2,
                approved_at=datetime(2026, 8, 27, tzinfo=UTC),
                artifacts=artifacts(),
            )

    def test_bom_change_selectively_invalidates_dependent_evidence(self) -> None:
        report = compare_revisions(revision(), revision(2, bom=50))

        self.assertEqual(report.changed_artifacts, (ArtifactKind.BOM,))
        self.assertIn(EvidenceClass.COST, report.invalidated_evidence)
        self.assertIn(EvidenceClass.HIL, report.invalidated_evidence)
        self.assertIn(EvidenceClass.COMMISSIONING, report.invalidated_evidence)
        self.assertIn(EvidenceClass.ANALYSIS, report.retained_evidence)
        self.assertIn(EvidenceClass.ELECTRICAL_RULES, report.retained_evidence)

    def test_software_change_keeps_analysis_cost_and_electrical_evidence(self) -> None:
        report = compare_revisions(revision(), revision(2, software=70))

        self.assertIn(EvidenceClass.SOFTWARE_BUILD, report.invalidated_evidence)
        self.assertIn(EvidenceClass.SIL, report.invalidated_evidence)
        self.assertIn(EvidenceClass.HIL, report.invalidated_evidence)
        self.assertIn(EvidenceClass.ANALYSIS, report.retained_evidence)
        self.assertIn(EvidenceClass.COST, report.retained_evidence)

    def test_revision_comparison_rejects_invalid_lineage(self) -> None:
        with self.assertRaises(ValueError):
            compare_revisions(revision(), revision(2, project_id="other"))

        wrong_parent = revision(2).model_copy(update={"parent_revision_id": "other"})
        with self.assertRaises(ValueError):
            compare_revisions(revision(), wrong_parent)

    def test_full_evidence_reaches_commissioned_sequentially(self) -> None:
        current = revision()
        records = tuple(evidence(current, item) for item in EvidenceClass)

        assessment = assess_maturity(current, records)

        self.assertEqual(assessment.maturity, DesignMaturity.COMMISSIONED)
        self.assertEqual(assessment.blockers, ())

    def test_simulation_cannot_replace_hil(self) -> None:
        current = revision()
        records = tuple(
            evidence(current, item)
            for item in EvidenceClass
            if item is not EvidenceClass.HIL
        )

        assessment = assess_maturity(current, records)

        self.assertEqual(assessment.maturity, DesignMaturity.BUILDABLE)
        self.assertIn("missing:hil", assessment.blockers)

    def test_hil_cannot_replace_missing_sil(self) -> None:
        current = revision()
        records = tuple(
            evidence(current, item)
            for item in EvidenceClass
            if item is not EvidenceClass.SIL
        )

        assessment = assess_maturity(current, records)

        self.assertEqual(assessment.maturity, DesignMaturity.BUILDABLE)
        self.assertIn("missing:sil", assessment.blockers)

    def test_stale_or_failed_evidence_blocks_maturity(self) -> None:
        current = revision()
        stale_analysis = evidence(
            current,
            EvidenceClass.ANALYSIS,
            dependency_override=content_hash(999),
        )
        stale = assess_maturity(current, (stale_analysis,))
        self.assertEqual(stale.maturity, DesignMaturity.SPECIFIED)
        self.assertEqual(stale.stale_evidence, (EvidenceClass.ANALYSIS,))

        failed = assess_maturity(
            current,
            (evidence(current, EvidenceClass.ANALYSIS, verdict=Verdict.FAIL),),
        )
        self.assertEqual(failed.maturity, DesignMaturity.SPECIFIED)
        self.assertIn("not_pass:analysis", failed.blockers)

    def test_duplicate_current_evidence_is_rejected(self) -> None:
        current = revision()
        record = evidence(current, EvidenceClass.ANALYSIS)
        with self.assertRaises(ValueError):
            assess_maturity(current, (record, record))


if __name__ == "__main__":
    unittest.main()
