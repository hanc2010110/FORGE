from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from unittest.mock import patch

from forge_core.change_management import ArtifactDomain, ReleaseStatus, SourceSystem
from forge_core.connectors import (
    ConnectorCaptureRequest,
    ConnectorRegistry,
    InMemoryReadOnlyAdapter,
    ReadOnlyAdapterManifest,
    adapter_manifest_hash,
)
from forge_core.constraints import evaluate_cost
from forge_core.persistence import IdempotencyConflictError, VersionConflictError
from forge_core.release_persistence import RawReleaseEvidence
from forge_core.release_service import (
    AnalyzeChangeCommand,
    CaptureSnapshotCommand,
    CreateProjectCommand,
    EvaluateReleaseCommand,
    IngestReleaseEvidenceCommand,
    MutationContext,
    ReleaseIntegrationService,
)
from forge_core.sqlite_store import SQLiteEvidenceStore
from tests.test_release_readiness import (
    NOW,
    artifact,
    build_evidence,
    cost_record,
    interface_contract,
    required_test_evidence,
)


def adapter_manifest(
    adapter_id: str, source_system: SourceSystem
) -> ReadOnlyAdapterManifest:
    capabilities: tuple[Literal["snapshot.read", "interface.read"], ...] = (
        "interface.read",
        "snapshot.read",
    )
    artifact_hash = "sha256:" + ("1" if source_system is SourceSystem.PLM else "2") * 64
    return ReadOnlyAdapterManifest(
        adapter_id=adapter_id,
        adapter_version="1.0.0",
        adapter_artifact_hash=artifact_hash,
        source_system=source_system,
        capabilities=capabilities,
        manifest_hash=adapter_manifest_hash(
            adapter_id=adapter_id,
            adapter_version="1.0.0",
            adapter_artifact_hash=artifact_hash,
            source_system=source_system,
            capabilities=capabilities,
        ),
    )


def registry() -> ConnectorRegistry:
    previous_at = NOW - timedelta(hours=2)
    current_at = NOW - timedelta(hours=1)
    hardware = artifact(
        "controller-board",
        ArtifactDomain.HARDWARE,
        SourceSystem.PLM,
        "12",
        1,
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
    firmware = artifact(
        "controller-firmware",
        ArtifactDomain.FIRMWARE,
        SourceSystem.GIT,
        "commit-42",
        3,
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
    new_doc = artifact(
        "release-notes",
        ArtifactDomain.DOCUMENTATION,
        SourceSystem.GIT,
        "doc-2",
        7,
        captured_at=current_at,
    )
    value = ConnectorRegistry()
    value.register(
        InMemoryReadOnlyAdapter(
            adapter_manifest("fake-git", SourceSystem.GIT),
            {
                "before": (firmware, protocol, old_doc),
                "after": (firmware, protocol, new_doc),
            },
            {
                firmware.artifact_id: interface_contract("firmware"),
                protocol.artifact_id: interface_contract("protocol"),
            },
        )
    )
    value.register(
        InMemoryReadOnlyAdapter(
            adapter_manifest("fake-plm", SourceSystem.PLM),
            {"before": (hardware, bom), "after": (hardware, bom)},
        )
    )
    return value


def capture_command(
    key: str,
    snapshot_id: str,
    captured_at: datetime,
    *,
    project_id: str = "project-1",
) -> CaptureSnapshotCommand:
    return CaptureSnapshotCommand(
        captures=tuple(
            ConnectorCaptureRequest(
                connector_id=connector_id,
                capture_key=key,
                project_id=project_id,
                snapshot_id=snapshot_id,
                hardware_revision_id="HW-12",
                captured_at=captured_at,
            )
            for connector_id in ("fake-git", "fake-plm")
        )
    )


def context(key: str, version: int | None) -> MutationContext:
    return MutationContext(
        local_installation_id="installation-1",
        idempotency_key=key,
        expected_project_version=version,
    )


class ReleaseIntegrationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SQLiteEvidenceStore(Path(self.temporary.name) / "forge.db")
        self.connectors = registry()
        self.service = ReleaseIntegrationService(
            self.store, self.connectors, clock=lambda: NOW
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_complete_change_to_release_flow_is_owned_and_replayable(self) -> None:
        created = self.service.create_project(
            CreateProjectCommand(project_id="project-1", name="Project One"),
            context("create", None),
        )
        self.assertEqual(created.project_version, 1)
        previous = self.service.capture_snapshot(
            "project-1",
            capture_command("before", "snapshot-11", NOW - timedelta(hours=2)),
            context("snapshot-before", 1),
        )
        current = self.service.capture_snapshot(
            "project-1",
            capture_command("after", "snapshot-12", NOW - timedelta(hours=1)),
            context("snapshot-after", 2),
        )
        self.assertEqual(previous.project_version, 2)
        self.assertEqual(current.project_version, 3)
        analyzed = self.service.analyze_change(
            "project-1",
            AnalyzeChangeCommand(
                from_snapshot_id="snapshot-11", to_snapshot_id="snapshot-12"
            ),
            context("analyze", 3),
        )
        analysis_hash = analyzed.payload["change_assessment"]["analysis_hash"]
        assessment = self.store.get_change_assessment(analysis_hash)
        snapshot = self.store.get_connector_snapshot("project-1", "snapshot-12")
        evidence: tuple[RawReleaseEvidence, ...] = (
            cost_record(snapshot.snapshot),
            build_evidence(assessment.assessment, snapshot.snapshot),
            *required_test_evidence(assessment.assessment, snapshot.snapshot),
        )
        version = 4
        for index, item in enumerate(evidence, start=1):
            ingested = self.service.ingest_release_evidence(
                "project-1",
                IngestReleaseEvidenceCommand(
                    analysis_hash=analysis_hash, evidence=item
                ),
                context(f"evidence-{index}", version),
            )
            version = ingested.project_version
        released = self.service.evaluate_release(
            "project-1",
            EvaluateReleaseCommand(analysis_hash=analysis_hash),
            context("release", version),
        )
        decision = released.payload["release_decision"]["decision"]
        self.assertEqual(decision["report"]["status"], ReleaseStatus.READY.value)
        self.assertEqual(len(self.store.list_release_decisions("project-1")), 1)

        replay = self.service.evaluate_release(
            "project-1",
            EvaluateReleaseCommand(analysis_hash=analysis_hash),
            context("release", version),
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.response_json, released.response_json)
        self.assertEqual(len(self.store.list_release_decisions("project-1")), 1)

    def test_idempotency_key_conflict_precedes_connector_side_effects(self) -> None:
        self.service.create_project(
            CreateProjectCommand(project_id="project-1", name="Project One"),
            context("create", None),
        )
        first = capture_command("before", "snapshot-11", NOW - timedelta(hours=2))
        self.service.capture_snapshot("project-1", first, context("snapshot-key", 1))
        changed = capture_command("after", "snapshot-12", NOW - timedelta(hours=1))
        with self.assertRaises(IdempotencyConflictError):
            self.service.capture_snapshot(
                "project-1", changed, context("snapshot-key", 2)
            )
        self.assertEqual(len(self.store.list_connector_snapshots("project-1")), 1)

    def test_transaction_race_replays_the_committed_response(self) -> None:
        command = CreateProjectCommand(project_id="project-1", name="Project One")
        first = self.service.create_project(command, context("create", None))
        with patch.object(self.store, "find_idempotency", return_value=None):
            raced = self.service.create_project(command, context("create", None))
        self.assertTrue(raced.replayed)
        self.assertEqual(raced.response_json, first.response_json)
        self.assertEqual(raced.project_version, 1)
        self.assertEqual(self.store.get_project("project-1").version, 1)

    def test_stale_version_is_rejected_before_connector_capture(self) -> None:
        self.service.create_project(
            CreateProjectCommand(project_id="project-1", name="Project One"),
            context("create", None),
        )
        command = capture_command("before", "snapshot-11", NOW - timedelta(hours=2))
        with (
            patch.object(
                self.connectors, "capture", wraps=self.connectors.capture
            ) as spy,
            self.assertRaises(VersionConflictError),
        ):
            self.service.capture_snapshot("project-1", command, context("stale", 2))
        spy.assert_not_called()

    def test_committed_idempotency_replay_wins_after_preflight_race(self) -> None:
        self.service.create_project(
            CreateProjectCommand(project_id="project-1", name="Project One"),
            context("create", None),
        )
        command = capture_command("before", "snapshot-11", NOW - timedelta(hours=2))
        first = self.service.capture_snapshot(
            "project-1", command, context("snapshot", 1)
        )
        with (
            patch.object(
                self.store,
                "find_idempotency",
                side_effect=[None, first.response_json],
            ),
            patch.object(
                self.connectors, "capture", wraps=self.connectors.capture
            ) as spy,
        ):
            replay = self.service.capture_snapshot(
                "project-1", command, context("snapshot", 1)
            )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.response_json, first.response_json)
        spy.assert_not_called()

    def test_equal_latest_cost_evaluations_block_release(self) -> None:
        self.service.create_project(
            CreateProjectCommand(project_id="project-1", name="Project One"),
            context("create", None),
        )
        self.service.capture_snapshot(
            "project-1",
            capture_command("before", "snapshot-11", NOW - timedelta(hours=2)),
            context("before", 1),
        )
        self.service.capture_snapshot(
            "project-1",
            capture_command("after", "snapshot-12", NOW - timedelta(hours=1)),
            context("after", 2),
        )
        analyzed = self.service.analyze_change(
            "project-1",
            AnalyzeChangeCommand(
                from_snapshot_id="snapshot-11", to_snapshot_id="snapshot-12"
            ),
            context("analysis", 3),
        )
        analysis_hash = analyzed.payload["change_assessment"]["analysis_hash"]
        assessment = self.store.get_change_assessment(analysis_hash)
        snapshot = self.store.get_connector_snapshot("project-1", "snapshot-12")
        first_cost = cost_record(snapshot.snapshot)
        second_cost = first_cost.model_copy(update={"evidence_id": "cost-second"})
        inflated = evaluate_cost(
            first_cost.bom,
            first_cost.quotes,
            currency="USD",
            budget_limit=Decimal("999999"),
            reserve_rate=Decimal("0"),
            evaluated_at=first_cost.evaluated_at,
        )
        malicious_cost = first_cost.model_copy(
            update={
                "evidence_id": "cost-malicious",
                "dependency_hash": "sha256:" + "0" * 64,
                "evaluation": inflated,
            }
        )
        with self.assertRaises(ValueError):
            self.service.ingest_release_evidence(
                "project-1",
                IngestReleaseEvidenceCommand(
                    analysis_hash=analysis_hash, evidence=malicious_cost
                ),
                context("malicious-cost", 4),
            )
        self.assertEqual(self.store.get_project("project-1").version, 4)
        evidence: tuple[RawReleaseEvidence, ...] = (
            first_cost,
            second_cost,
            build_evidence(assessment.assessment, snapshot.snapshot),
            *required_test_evidence(assessment.assessment, snapshot.snapshot),
        )
        version = 4
        for index, item in enumerate(evidence):
            result = self.service.ingest_release_evidence(
                "project-1",
                IngestReleaseEvidenceCommand(
                    analysis_hash=analysis_hash, evidence=item
                ),
                context(f"evidence-{index}", version),
            )
            version = result.project_version
        released = self.service.evaluate_release(
            "project-1",
            EvaluateReleaseCommand(analysis_hash=analysis_hash),
            context("release", version),
        )
        decision = released.payload["release_decision"]["decision"]
        self.assertEqual(decision["report"]["status"], ReleaseStatus.BLOCKED.value)
        self.assertIn(
            "finding:bom_cost_evidence_ambiguous",
            decision["report"]["blocker_codes"],
        )
        self.assertEqual(
            {
                item["reason"]
                for item in decision["rejected_evidence"]
                if item["evidence_id"] in {"cost-current", "cost-second"}
            },
            {"ambiguous_latest_timestamp"},
        )

    def test_release_evidence_ids_are_scoped_to_each_project(self) -> None:
        for project_id in ("project-1", "project-2"):
            self.service.create_project(
                CreateProjectCommand(project_id=project_id, name=project_id),
                context("create", None),
            )
            self.service.capture_snapshot(
                project_id,
                capture_command(
                    "before",
                    "snapshot-11",
                    NOW - timedelta(hours=2),
                    project_id=project_id,
                ),
                context("before", 1),
            )
            self.service.capture_snapshot(
                project_id,
                capture_command(
                    "after",
                    "snapshot-12",
                    NOW - timedelta(hours=1),
                    project_id=project_id,
                ),
                context("after", 2),
            )
            analyzed = self.service.analyze_change(
                project_id,
                AnalyzeChangeCommand(
                    from_snapshot_id="snapshot-11", to_snapshot_id="snapshot-12"
                ),
                context("analysis", 3),
            )
            analysis_hash = analyzed.payload["change_assessment"]["analysis_hash"]
            snapshot = self.store.get_connector_snapshot(project_id, "snapshot-12")
            self.service.ingest_release_evidence(
                project_id,
                IngestReleaseEvidenceCommand(
                    analysis_hash=analysis_hash,
                    evidence=cost_record(snapshot.snapshot),
                ),
                context("cost", 4),
            )

        self.assertEqual(
            self.store.get_release_evidence("project-1", "cost-current").project_id,
            "project-1",
        )
        self.assertEqual(
            self.store.get_release_evidence("project-2", "cost-current").project_id,
            "project-2",
        )


if __name__ == "__main__":
    unittest.main()
