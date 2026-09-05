from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from forge_core.change_planning import (
    ChangeScenario,
    PlanVerification,
    change_scenario_hash,
    plan_verification_hash,
)
from forge_core.persistence import (
    CorruptRecordError,
    IntegrityConflictError,
    RecordNotFoundError,
)
from forge_core.planning_persistence import (
    StoredChangeImpactPreview,
    StoredPlanVerification,
)
from forge_core.preview_engine import (
    preview_change_scenario,
    verify_plan_against_actual,
)
from forge_core.release_service import (
    AnalyzeChangeCommand,
    CreateProjectCommand,
    MutationContext,
    ReleaseIntegrationService,
)
from forge_core.sqlite_store import SQLiteEvidenceStore
from tests.test_change_planning import asset_input as default_asset_input
from tests.test_change_planning import planned_change, sha
from tests.test_release_readiness import NOW
from tests.test_release_service import (
    capture_command,
    external_evidence_plan_command,
    registry,
)


def context(key: str, version: int | None) -> MutationContext:
    return MutationContext(
        local_installation_id="installation-1",
        idempotency_key=key,
        expected_project_version=version,
    )


def scenario_for_baseline(
    project_id: str,
    snapshot_id: str,
    snapshot_hash: str,
) -> ChangeScenario:
    changes = (planned_change(),)
    scenario_hash = change_scenario_hash(
        scenario_id="scenario-1",
        project_id=project_id,
        asset_input=default_asset_input(),
        baseline_snapshot_id=snapshot_id,
        baseline_snapshot_hash=snapshot_hash,
        proposed_hardware_revision_id="HW-12-proposed",
        created_at=NOW,
        changes=changes,
    )
    return ChangeScenario(
        scenario_id="scenario-1",
        project_id=project_id,
        asset_input=default_asset_input(),
        baseline_snapshot_id=snapshot_id,
        baseline_snapshot_hash=snapshot_hash,
        proposed_hardware_revision_id="HW-12-proposed",
        created_at=NOW,
        changes=changes,
        scenario_hash=scenario_hash,
    )


class PlanningPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SQLiteEvidenceStore(Path(self.temporary.name) / "forge.db")
        self.service = ReleaseIntegrationService(
            self.store,
            registry(),
            clock=lambda: NOW,
        )
        self.service.create_project(
            CreateProjectCommand(project_id="project-1", name="Project One"),
            context("create", None),
        )
        self.service.capture_snapshot(
            "project-1",
            capture_command("before", "snapshot-11", NOW - timedelta(hours=2)),
            context("baseline", 1),
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _store_preview(self, expected_version: int = 2) -> StoredChangeImpactPreview:
        baseline = self.store.get_connector_snapshot("project-1", "snapshot-11")
        scenario = scenario_for_baseline(
            "project-1", baseline.snapshot_id, baseline.snapshot_hash
        )
        preview = preview_change_scenario(scenario, generated_at=NOW)
        record = StoredChangeImpactPreview(
            project_id="project-1",
            preview_hash=preview.preview_hash,
            scenario_id=scenario.scenario_id,
            scenario_hash=scenario.scenario_hash,
            baseline_snapshot_id=scenario.baseline_snapshot_id,
            baseline_snapshot_hash=scenario.baseline_snapshot_hash,
            scenario=scenario,
            preview=preview,
            stored_at=NOW,
        )
        self.store.store_change_preview(
            record,
            expected_project_version=expected_version,
        )
        return record

    def test_change_preview_round_trips_and_exports(self) -> None:
        record = self._store_preview()

        self.assertEqual(self.store.get_change_preview(record.preview_hash), record)
        self.assertEqual(self.store.list_change_previews("project-1"), (record,))
        export = self.store.export_project("project-1").content
        self.assertIn(b"change-previews/", export)
        self.assertIn(record.preview_hash.encode("utf-8"), export)

    def test_change_preview_rejects_wrong_baseline_and_duplicate(self) -> None:
        record = self._store_preview()
        with self.assertRaises(IntegrityConflictError):
            self.store.store_change_preview(record, expected_project_version=3)

        wrong = scenario_for_baseline("project-1", "snapshot-11", sha(99))
        preview = preview_change_scenario(wrong, generated_at=NOW)
        wrong_record = StoredChangeImpactPreview(
            project_id="project-1",
            preview_hash=preview.preview_hash,
            scenario_id=wrong.scenario_id,
            scenario_hash=wrong.scenario_hash,
            baseline_snapshot_id=wrong.baseline_snapshot_id,
            baseline_snapshot_hash=wrong.baseline_snapshot_hash,
            scenario=wrong,
            preview=preview,
            stored_at=NOW,
        )
        with self.assertRaises(IntegrityConflictError):
            self.store.store_change_preview(wrong_record, expected_project_version=3)

    def test_plan_verification_round_trips_and_rejects_unstored_inputs(self) -> None:
        preview_record = self._store_preview()
        self.service.capture_snapshot(
            "project-1",
            capture_command("after", "snapshot-12", NOW - timedelta(hours=1)),
            context("current", 3),
        )
        analyzed = self.service.analyze_change(
            "project-1",
            AnalyzeChangeCommand(
                from_snapshot_id="snapshot-11",
                to_snapshot_id="snapshot-12",
            ),
            context("analysis", 4),
        )
        assessment = self.store.get_change_assessment(
            analyzed.payload["change_assessment"]["analysis_hash"]
        )
        verification = verify_plan_against_actual(
            preview_record.preview,
            preview_record.scenario,
            assessment.assessment,
            verified_at=NOW,
        )
        record = StoredPlanVerification(
            project_id="project-1",
            verification_hash=verification.verification_hash,
            scenario_id=verification.scenario_id,
            preview_hash=verification.preview_hash,
            actual_change_analysis_hash=verification.actual_change_analysis_hash,
            verification=verification,
            stored_at=NOW,
        )
        self.store.store_plan_verification(record, expected_project_version=5)

        self.assertEqual(
            self.store.get_plan_verification(record.verification_hash), record
        )
        self.assertEqual(self.store.list_plan_verifications("project-1"), (record,))
        export = self.store.export_project("project-1").content
        self.assertIn(b"plan-verifications/", export)
        with self.assertRaises(RecordNotFoundError):
            missing_hash = plan_verification_hash(
                verification_id="missing-preview",
                project_id="project-1",
                scenario_id=verification.scenario_id,
                preview_hash=sha(71),
                actual_change_analysis_hash=verification.actual_change_analysis_hash,
                verified_at=NOW,
                matches_plan=True,
                observed_planned_refs=(),
                deviations=(),
            )
            missing_verification = PlanVerification(
                verification_id="missing-preview",
                project_id="project-1",
                scenario_id=verification.scenario_id,
                preview_hash=sha(71),
                actual_change_analysis_hash=(verification.actual_change_analysis_hash),
                verified_at=NOW,
                matches_plan=True,
                observed_planned_refs=(),
                deviations=(),
                verification_hash=missing_hash,
            )
            missing = StoredPlanVerification(
                project_id="project-1",
                verification_hash=missing_verification.verification_hash,
                scenario_id=missing_verification.scenario_id,
                preview_hash=missing_verification.preview_hash,
                actual_change_analysis_hash=(
                    missing_verification.actual_change_analysis_hash
                ),
                verification=missing_verification,
                stored_at=NOW,
            )
            self.store.store_plan_verification(missing, expected_project_version=6)

    def test_external_evidence_plan_round_trips_exports_and_detects_row_tamper(
        self,
    ) -> None:
        created = self.service.create_external_evidence_plan(
            "project-1",
            external_evidence_plan_command(),
            context("external-plan", 2),
        )
        plan_hash = created.payload["external_evidence_plan"]["plan_hash"]
        record = self.store.get_external_evidence_plan(plan_hash)
        self.assertEqual(
            self.store.list_external_evidence_plans("project-1"), (record,)
        )
        export = self.store.export_project("project-1").content
        self.assertIn(b"external-evidence-plans/", export)
        self.assertIn(plan_hash.encode("utf-8"), export)

        with sqlite3.connect(self.store.path) as connection:
            connection.execute(
                "UPDATE external_evidence_plans "
                "SET scenario_id = ? WHERE plan_hash = ?",
                ("tampered-scenario", plan_hash),
            )
        with self.assertRaises(CorruptRecordError):
            self.store.get_external_evidence_plan(plan_hash)


if __name__ == "__main__":
    unittest.main()
