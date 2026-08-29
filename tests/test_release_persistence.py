from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from forge_core.change_management import (
    ConnectorSnapshot,
    ReleaseEvidenceKind,
)
from forge_core.hashing import canonical_sha256
from forge_core.impact_engine import analyze_change, connector_snapshot_hash
from forge_core.persistence import (
    CorruptRecordError,
    IntegrityConflictError,
    ProjectRecord,
    RecordNotFoundError,
)
from forge_core.release_persistence import (
    StoredChangeImpactAssessment,
    StoredConnectorSnapshot,
    StoredRawReleaseEvidence,
    StoredReleaseDecision,
    StoredReleasePolicy,
)
from forge_core.release_readiness import (
    DEFAULT_RELEASE_READINESS_POLICY,
    ReleaseReadinessPolicy,
    evaluate_release_readiness,
)
from forge_core.sqlite_store import SQLiteEvidenceStore
from tests.test_release_readiness import (
    NOW,
    build_evidence,
    cost_record,
    required_test_evidence,
)
from tests.test_release_service import capture_command, registry


def release_history() -> tuple[
    StoredConnectorSnapshot,
    StoredConnectorSnapshot,
    StoredChangeImpactAssessment,
]:
    previous_at = NOW - timedelta(hours=2)
    current_at = NOW - timedelta(hours=1)
    connector_registry = registry()

    def capture(
        key: str, snapshot_id: str, captured_at: datetime
    ) -> StoredConnectorSnapshot:
        bundles = tuple(
            connector_registry.capture(request)
            for request in capture_command(key, snapshot_id, captured_at).captures
        )
        snapshot = ConnectorSnapshot(
            snapshot_id=snapshot_id,
            project_id="project-1",
            hardware_revision_id="HW-12",
            captured_at=captured_at,
            artifacts=tuple(
                sorted(
                    (
                        artifact
                        for bundle in bundles
                        for artifact in bundle.snapshot.artifacts
                    ),
                    key=lambda item: (item.source_system.value, item.artifact_id),
                )
            ),
        )
        projections = tuple(
            sorted(
                (projection for bundle in bundles for projection in bundle.projections),
                key=lambda item: item.projection_hash,
            )
        )
        return StoredConnectorSnapshot(
            project_id=snapshot.project_id,
            snapshot_id=snapshot.snapshot_id,
            snapshot_hash=connector_snapshot_hash(snapshot),
            snapshot=snapshot,
            capture_bundles=bundles,
            interface_projections=projections,
            stored_at=NOW + timedelta(minutes=1),
        )

    previous_record = capture("before", "snapshot-11", previous_at)
    current_record = capture("after", "snapshot-12", current_at)
    assessment = analyze_change(
        previous_record.snapshot,
        current_record.snapshot,
        interface_projections=(
            previous_record.interface_projections + current_record.interface_projections
        ),
    )
    stored_at = NOW + timedelta(minutes=1)
    return (
        previous_record,
        current_record,
        StoredChangeImpactAssessment(
            project_id=assessment.project_id,
            analysis_hash=assessment.analysis_hash,
            from_snapshot_id=assessment.from_snapshot_id,
            to_snapshot_id=assessment.to_snapshot_id,
            assessment=assessment,
            stored_at=stored_at,
        ),
    )


class ReleasePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "forge.db"
        self.store = SQLiteEvidenceStore(self.path)
        self.project = ProjectRecord(
            project_id="project-1",
            name="Project One",
            version=1,
            created_at=NOW,
            updated_at=NOW,
        )
        self.store.create_project(self.project)
        policy = StoredReleasePolicy(
            project_id="project-1",
            policy_hash=canonical_sha256(DEFAULT_RELEASE_READINESS_POLICY),
            policy=DEFAULT_RELEASE_READINESS_POLICY,
            stored_at=NOW,
        )
        self.initial_version = self.store.store_release_policy(
            policy, expected_project_version=1
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _store_release_inputs(self) -> tuple[int, StoredReleaseDecision]:
        previous, current, assessment = release_history()
        version = self.store.store_connector_snapshot(
            previous, expected_project_version=self.initial_version
        )
        version = self.store.store_connector_snapshot(
            current, expected_project_version=version
        )
        version = self.store.store_change_assessment(
            assessment, expected_project_version=version
        )
        cost = cost_record(current.snapshot)
        build = build_evidence(assessment.assessment, current.snapshot)
        tests = required_test_evidence(assessment.assessment, current.snapshot)
        raw_values = (
            StoredRawReleaseEvidence(
                project_id="project-1",
                evidence_id=cost.evidence_id,
                evidence_kind=ReleaseEvidenceKind.BOM_COST,
                evidence_hash=canonical_sha256(cost),
                change_analysis_hash=assessment.analysis_hash,
                snapshot_hash=current.snapshot_hash,
                hardware_revision_id=current.snapshot.hardware_revision_id,
                evidence=cost,
                stored_at=NOW + timedelta(minutes=1),
            ),
            StoredRawReleaseEvidence(
                project_id="project-1",
                evidence_id=build.evidence_id,
                evidence_kind=ReleaseEvidenceKind.FIRMWARE_BUILD,
                evidence_hash=canonical_sha256(build),
                change_analysis_hash=assessment.analysis_hash,
                snapshot_hash=current.snapshot_hash,
                hardware_revision_id=current.snapshot.hardware_revision_id,
                evidence=build,
                stored_at=NOW + timedelta(minutes=1),
            ),
            *(
                StoredRawReleaseEvidence(
                    project_id="project-1",
                    evidence_id=item.evidence_id,
                    evidence_kind=ReleaseEvidenceKind.TEST_RESULT,
                    evidence_hash=canonical_sha256(item),
                    change_analysis_hash=assessment.analysis_hash,
                    snapshot_hash=current.snapshot_hash,
                    hardware_revision_id=current.snapshot.hardware_revision_id,
                    evidence=item,
                    stored_at=NOW + timedelta(minutes=1),
                )
                for item in tests
            ),
        )
        for raw in raw_values:
            version = self.store.store_release_evidence(
                raw, expected_project_version=version
            )
        decision = evaluate_release_readiness(
            assessment.assessment,
            current.snapshot,
            cost_evaluation=cost,
            firmware_builds=(build,),
            test_results=tests,
            evaluated_at=NOW,
        )
        stored_decision = StoredReleaseDecision(
            project_id="project-1",
            sequence=1,
            decision_hash=decision.decision_hash,
            report_id=decision.report.report_id,
            decision=decision,
            stored_at=NOW + timedelta(minutes=1),
        )
        return version, stored_decision

    def test_full_release_history_round_trips_and_exports(self) -> None:
        version, decision = self._store_release_inputs()
        version = self.store.append_release_decision(
            decision, expected_project_version=version
        )

        self.assertEqual(
            self.store.get_release_decision(decision.decision_hash), decision
        )
        self.assertEqual(self.store.list_release_decisions("project-1"), (decision,))
        self.assertEqual(len(self.store.list_connector_snapshots("project-1")), 2)
        current = self.store.get_connector_snapshot("project-1", "snapshot-12")
        self.assertEqual(
            tuple(bundle.manifest.adapter_id for bundle in current.capture_bundles),
            ("fake-git", "fake-plm"),
        )
        self.assertTrue(all(bundle.capture_hash for bundle in current.capture_bundles))
        self.assertEqual(len(self.store.list_change_assessments("project-1")), 1)
        self.assertEqual(len(self.store.list_release_evidence("project-1")), 3)
        bundle = json.loads(self.store.export_project("project-1").content)
        paths = {item["path"] for item in bundle["entries"]}
        self.assertTrue(
            {
                "connector-snapshots/snapshot-11.json",
                "connector-snapshots/snapshot-12.json",
                "change-assessments/"
                f"{decision.decision.report.change_analysis_hash}.json",
                "release-evidence/cost-current.json",
                "release-evidence/build-current.json",
                "release-decisions/1.json",
                "release-policy.json",
            }
            <= paths
        )
        self.assertEqual(self.store.get_project("project-1").version, version)

    def test_missing_inputs_and_broken_decision_chain_roll_back(self) -> None:
        _, _, assessment = release_history()
        with self.assertRaises(RecordNotFoundError):
            self.store.store_change_assessment(
                assessment, expected_project_version=self.initial_version
            )
        self.assertEqual(
            self.store.get_project("project-1").version, self.initial_version
        )

        version, decision = self._store_release_inputs()
        bad_chain = decision.model_copy(
            update={
                "sequence": 2,
                "previous_decision_hash": "sha256:" + "0" * 64,
            }
        )
        with self.assertRaises(IntegrityConflictError):
            self.store.append_release_decision(
                bad_chain, expected_project_version=version
            )
        self.assertEqual(self.store.list_release_decisions("project-1"), ())

    def test_decision_requires_exact_immutable_policy_hash(self) -> None:
        version, stored = self._store_release_inputs()
        alternate_policy = ReleaseReadinessPolicy.model_validate(
            DEFAULT_RELEASE_READINESS_POLICY.model_dump(mode="python")
            | {"cost_budget_limit": Decimal("30")}
        )
        self.assertEqual(alternate_policy, DEFAULT_RELEASE_READINESS_POLICY)
        self.assertNotEqual(
            canonical_sha256(alternate_policy),
            canonical_sha256(DEFAULT_RELEASE_READINESS_POLICY),
        )
        original = stored.decision
        selected_build = original.selected_firmware_build
        if selected_build is None:
            raise AssertionError("release fixture must select a firmware build")
        alternate = evaluate_release_readiness(
            original.change_assessment,
            original.target_snapshot,
            cost_evaluation=original.cost_evaluation,
            firmware_builds=(selected_build,),
            test_results=original.selected_test_results,
            evaluated_at=original.report.evaluated_at,
            policy=alternate_policy,
        )
        record = StoredReleaseDecision(
            project_id=stored.project_id,
            sequence=stored.sequence,
            decision_hash=alternate.decision_hash,
            report_id=alternate.report.report_id,
            decision=alternate,
            stored_at=stored.stored_at,
        )

        with self.assertRaises(IntegrityConflictError):
            self.store.append_release_decision(record, expected_project_version=version)
        self.assertEqual(self.store.list_release_decisions("project-1"), ())

    def test_decision_requires_exact_raw_evidence_hashes(self) -> None:
        version, stored = self._store_release_inputs()
        original = stored.decision
        cost = original.cost_evaluation
        selected_build = original.selected_firmware_build
        if cost is None or selected_build is None:
            raise AssertionError("release fixture must select cost and build evidence")
        alternate_quotes = tuple(
            quote.model_copy(update={"unit_price": Decimal("10.0")})
            for quote in cost.quotes
        )
        alternate_cost = type(cost).model_validate(
            cost.model_dump(mode="python") | {"quotes": alternate_quotes}
        )
        self.assertEqual(alternate_cost, cost)
        self.assertNotEqual(canonical_sha256(alternate_cost), canonical_sha256(cost))
        alternate = evaluate_release_readiness(
            original.change_assessment,
            original.target_snapshot,
            cost_evaluation=alternate_cost,
            firmware_builds=(selected_build,),
            test_results=original.selected_test_results,
            evaluated_at=original.report.evaluated_at,
            policy=original.policy,
        )
        record = StoredReleaseDecision(
            project_id=stored.project_id,
            sequence=stored.sequence,
            decision_hash=alternate.decision_hash,
            report_id=alternate.report.report_id,
            decision=alternate,
            stored_at=stored.stored_at,
        )

        with self.assertRaises(IntegrityConflictError):
            self.store.append_release_decision(record, expected_project_version=version)
        self.assertEqual(self.store.list_release_decisions("project-1"), ())

    def test_row_scalar_tampering_is_detected(self) -> None:
        version, decision = self._store_release_inputs()
        self.store.append_release_decision(decision, expected_project_version=version)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE release_decisions SET status = 'blocked' WHERE sequence = 1"
            )
        with self.assertRaises(CorruptRecordError):
            self.store.get_release_decision(decision.decision_hash)

    def test_wrapper_contracts_reject_hash_identity_and_time_tampering(self) -> None:
        previous, _, assessment = release_history()
        with self.assertRaises(ValidationError):
            StoredConnectorSnapshot.model_validate(
                previous.model_dump(mode="python")
                | {"snapshot_hash": "sha256:" + "0" * 64}
            )
        with self.assertRaises(ValidationError):
            StoredConnectorSnapshot.model_validate(
                previous.model_dump(mode="python") | {"capture_bundles": ()}
            )
        with self.assertRaises(ValidationError):
            StoredChangeImpactAssessment.model_validate(
                assessment.model_dump(mode="python")
                | {"analysis_hash": "sha256:" + "0" * 64}
            )
        with self.assertRaises(ValidationError):
            StoredReleaseDecision.model_validate(
                {
                    "project_id": "project-1",
                    "sequence": 2,
                    "previous_decision_hash": None,
                    "decision_hash": "sha256:" + "0" * 64,
                    "report_id": "report",
                    "decision": {},
                    "stored_at": datetime(2026, 8, 29, tzinfo=UTC),
                }
            )


if __name__ == "__main__":
    unittest.main()
