from __future__ import annotations

import base64
import http.client
import json
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any

from forge_core.access_control import Actor, Membership, ProjectAccess, Role
from forge_core.loopback_api import LoopbackAPI, create_loopback_server
from forge_core.release_persistence import RawReleaseEvidence
from forge_core.release_service import (
    AnalyzeChangeCommand,
    CreateProjectCommand,
    EvaluateReleaseCommand,
    IngestReleaseEvidenceCommand,
    ReleaseIntegrationService,
    VerifyPlanCommand,
)
from forge_core.sqlite_store import SQLiteEvidenceStore
from tests.test_cad_geometry import ascii_stl
from tests.test_release_readiness import (
    NOW,
    build_evidence,
    cost_record,
    required_test_evidence,
)
from tests.test_release_service import (
    capture_command,
    change_preview_command,
    external_evidence_plan_command,
    registry,
)


def json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class LoopbackAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SQLiteEvidenceStore(Path(self.temporary.name) / "forge.db")
        self.service = ReleaseIntegrationService(
            self.store, registry(), clock=lambda: NOW
        )
        self.api = LoopbackAPI(
            self.service,
            port=43127,
            csrf_secret=b"test-secret",
            local_installation_id="installation-1",
            nonce_factory=lambda: "a" * 64,
            max_request_bytes=1_048_576,
        )
        session = self.api.handle(
            "GET", "/api/v1/session", [("Host", self.api.expected_host)]
        )
        self.token = str(session.json()["csrf_token"])

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def mutation_headers(
        self,
        body: bytes,
        *,
        key: str = "request-1",
        version: int | None = None,
        project_id: str = "project-1",
        org_id: str | None = None,
        actor_id: str | None = None,
    ) -> list[tuple[str, str]]:
        headers = [
            ("Host", self.api.expected_host),
            ("Origin", self.api.expected_origin),
            ("Cookie", f"forge_csrf={self.token}"),
            ("X-FORGE-CSRF", self.token),
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
            ("Idempotency-Key", key),
        ]
        if version is not None:
            headers.append(("If-Match", f'"project:{project_id}:v{version}"'))
        if org_id is not None:
            headers.append(("X-FORGE-ORG-ID", org_id))
        if actor_id is not None:
            headers.append(("X-FORGE-ACTOR-ID", actor_id))
        return headers

    def test_rbac_denies_viewer_mutation_and_exposes_append_only_audit(self) -> None:
        created = self.post(
            "/api/v1/projects",
            {"project_id": "project-1", "name": "Project One"},
            key="create-rbac",
            version=None,
        )
        self.assertEqual(created.status, 201)
        self.store.insert_actor(
            Actor(actor_id="viewer-1", display_name="Read-only reviewer")
        )
        self.store.insert_membership(
            Membership(
                org_id="local-org",
                actor_id="viewer-1",
                role=Role.VIEWER,
                granted_by="local-operator",
                granted_at=NOW,
            )
        )
        self.store.insert_project_access(
            ProjectAccess(
                org_id="local-org",
                project_id="project-1",
                actor_id="viewer-1",
            )
        )

        readable = self.api.handle(
            "GET",
            "/api/v1/projects/project-1",
            [
                ("Host", self.api.expected_host),
                ("X-FORGE-ORG-ID", "local-org"),
                ("X-FORGE-ACTOR-ID", "viewer-1"),
            ],
        )
        self.assertEqual(readable.status, 200)

        command = capture_command("before", "snapshot-rbac", NOW)
        body = json_bytes(command.model_dump(mode="json"))
        denied = self.api.handle(
            "POST",
            "/api/v1/projects/project-1/connector-snapshots",
            self.mutation_headers(
                body,
                key="viewer-capture",
                version=1,
                org_id="local-org",
                actor_id="viewer-1",
            ),
            body,
        )
        self.assertEqual(denied.status, 403)
        self.assertEqual(denied.json()["error"]["code"], "permission_denied")
        repeated_denial = self.api.handle(
            "POST",
            "/api/v1/projects/project-1/connector-snapshots",
            self.mutation_headers(
                body,
                key="viewer-capture",
                version=1,
                org_id="local-org",
                actor_id="viewer-1",
            ),
            body,
        )
        self.assertEqual(repeated_denial.status, 403)
        self.assertEqual(self.store.get_project("project-1").version, 1)

        audit = self.api.handle(
            "GET",
            "/api/v1/projects/project-1/audit-events",
            [("Host", self.api.expected_host)],
        )
        self.assertEqual(audit.status, 200)
        events = audit.json()["data"]
        self.assertTrue(
            any(
                item["actor_id"] == "viewer-1"
                and item["operation"] == "read_project"
                and item["allowed"] is True
                for item in events
            )
        )
        self.assertEqual(
            sum(
                item["actor_id"] == "viewer-1"
                and item["operation"] == "mutate_evidence"
                and item["allowed"] is False
                for item in events
            ),
            2,
        )
        self.assertTrue(
            any(
                item["actor_id"] == "viewer-1"
                and item["operation"] == "mutate_evidence"
                and item["allowed"] is False
                and item["reason"] == "role_lacks_permission"
                for item in events
            )
        )

    def test_unknown_identity_fails_closed_and_is_audited(self) -> None:
        created = self.post(
            "/api/v1/projects",
            {"project_id": "project-1", "name": "Project One"},
            key="create-unknown",
            version=None,
        )
        self.assertEqual(created.status, 201)
        denied = self.api.handle(
            "GET",
            "/api/v1/projects/project-1",
            [
                ("Host", self.api.expected_host),
                ("X-FORGE-ORG-ID", "unknown-org"),
                ("X-FORGE-ACTOR-ID", "unknown-actor"),
            ],
        )
        self.assertEqual(denied.status, 403)
        events = self.store.list_audit_events("project-1")
        denial = next(item for item in events if item.actor_id == "unknown-actor")
        self.assertEqual(denial.reason, "identity_or_access_not_found")
        self.assertFalse(denial.allowed)

    def test_deep_readiness_health_reports_verified_database_state(self) -> None:
        response = self.api.handle(
            "GET", "/api/v1/health/readiness", [("Host", self.api.expected_host)]
        )
        self.assertEqual(response.status, 200)
        report = response.json()["data"]
        self.assertEqual(report["status"], "READY")
        self.assertEqual(report["integrity_result"], "ok")
        self.assertEqual(report["observed_schema_version"], 13)
        self.assertEqual(report["pending_automatic_reverifications"], 0)
        self.assertEqual(report["automatic_reverification_failures"], 0)
        self.assertTrue(report["report_hash"].startswith("sha256:"))

    def test_local_rag_ingests_project_text_and_returns_cited_chat_result(self) -> None:
        created = self.post(
            "/api/v1/projects",
            {"project_id": "project-1", "name": "Robot Arm"},
            key="create-rag",
            version=None,
        )
        self.assertEqual(created.status, 201)
        ingested = self.post(
            "/api/v1/projects/project-1/knowledge-sources",
            {
                "source_id": "arm-spec",
                "source_kind": "document",
                "source_uri": "file:///arm-spec.txt",
                "source_version": "rev-4",
                "captured_at": NOW.isoformat(),
                "text": "Upper arm length is 420 mm and payload limit is 10 kg.",
            },
            key="ingest-rag",
            version=1,
        )
        self.assertEqual(ingested.status, 201, ingested.json())
        source = ingested.json()["knowledge_source"]
        self.assertEqual(source["source_id"], "arm-spec")
        self.assertTrue(source["ingestion"]["ingestion_hash"].startswith("sha256:"))

        answered = self.post(
            "/api/v1/projects/project-1/conversations",
            {
                "request_id": "chat-1",
                "user_message": "What is the upper arm length?",
                "requested_at": NOW.isoformat(),
            },
            key="chat-rag",
            version=2,
        )
        self.assertEqual(answered.status, 201, answered.json())
        runtime = answered.json()["conversation"]["runtime"]
        self.assertEqual(runtime["draft"]["disposition"], "answer")
        self.assertEqual(
            runtime["draft"]["claims"][0]["context_refs"],
            ["arm-spec:chunk:0001"],
        )
        self.assertTrue(runtime["runtime_hash"].startswith("sha256:"))

        history = self.api.handle(
            "GET",
            "/api/v1/projects/project-1/conversations",
            [("Host", self.api.expected_host)],
        )
        self.assertEqual(history.status, 200)
        self.assertEqual(history.json()["data"][0]["request_id"], "chat-1")
        exported = self.store.export_project("project-1").content
        self.assertIn(b"knowledge-sources/arm-spec.json", exported)
        self.assertIn(b"conversation-runtime/chat-1.json", exported)

    def test_stl_geometry_is_parsed_persisted_and_read_back(self) -> None:
        created = self.post(
            "/api/v1/projects",
            {"project_id": "project-1", "name": "Robot Arm"},
            key="create-stl",
            version=None,
        )
        self.assertEqual(created.status, 201)
        uploaded = self.post(
            "/api/v1/projects/project-1/cad-geometries",
            {
                "asset_id": "upper-arm",
                "source_uri": "upload://upper-arm.stl",
                "source_version": "rev-4",
                "captured_at": NOW.isoformat(),
                "content_base64": base64.b64encode(ascii_stl()).decode("ascii"),
            },
            key="upload-stl",
            version=1,
        )
        self.assertEqual(uploaded.status, 201, uploaded.json())
        geometry = uploaded.json()["cad_geometry"]["asset"]
        self.assertEqual(geometry["format"], "stl-ascii")
        self.assertEqual(geometry["triangle_count"], 1)
        self.assertEqual(geometry["bounds"]["maximum"]["x"], 1.0)

        listed = self.api.handle(
            "GET",
            "/api/v1/projects/project-1/cad-geometries",
            [("Host", self.api.expected_host)],
        )
        self.assertEqual(listed.status, 200)
        self.assertEqual(listed.json()["data"][0]["asset_id"], "upper-arm")
        self.assertIn(
            b"cad-geometries/upper-arm.json",
            self.store.export_project("project-1").content,
        )

    def test_stl_geometry_rejects_invalid_base64_without_mutating_project(self) -> None:
        created = self.post(
            "/api/v1/projects",
            {"project_id": "project-1", "name": "Robot Arm"},
            key="create-invalid-stl",
            version=None,
        )
        self.assertEqual(created.status, 201)

        rejected = self.post(
            "/api/v1/projects/project-1/cad-geometries",
            {
                "asset_id": "upper-arm",
                "source_uri": "upload://upper-arm.stl",
                "source_version": "rev-4",
                "captured_at": NOW.isoformat(),
                "content_base64": "not-base64!",
            },
            key="invalid-stl",
            version=1,
        )

        self.assertEqual(rejected.status, 422, rejected.json())
        self.assertEqual(rejected.json()["error"]["code"], "validation_failed")
        self.assertEqual(self.store.get_project("project-1").version, 1)
        self.assertEqual(self.store.list_cad_geometries("project-1"), ())

    def post(
        self,
        target: str,
        payload: object,
        *,
        key: str,
        version: int | None,
        project_id: str = "project-1",
    ) -> Any:
        body = json_bytes(payload)
        return self.api.handle(
            "POST",
            target,
            self.mutation_headers(
                body, key=key, version=version, project_id=project_id
            ),
            body,
        )

    def test_full_change_release_flow_and_read_routes(self) -> None:
        connectors = self.api.handle(
            "GET", "/api/v1/connectors", [("Host", self.api.expected_host)]
        )
        self.assertEqual(connectors.status, 200)
        self.assertEqual(
            [item["adapter_id"] for item in connectors.json()["data"]],
            ["fake-git", "fake-plm"],
        )

        created = self.post(
            "/api/v1/projects",
            CreateProjectCommand(project_id="project-1", name="Project One").model_dump(
                mode="json"
            ),
            key="create",
            version=None,
        )
        self.assertEqual(created.status, 201)
        self.assertEqual(created.headers["ETag"], '"project:project-1:v1"')
        self.assertEqual(created.headers["Idempotency-Replayed"], "false")

        replay = self.post(
            "/api/v1/projects",
            {"name": "Project One", "project_id": "project-1"},
            key="create",
            version=None,
        )
        self.assertEqual(replay.status, 201)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")

        before = capture_command("before", "snapshot-11", NOW - timedelta(hours=2))
        after = capture_command("after", "snapshot-12", NOW - timedelta(hours=1))
        first = self.post(
            "/api/v1/projects/project-1/connector-snapshots",
            before.model_dump(mode="json"),
            key="before",
            version=1,
        )
        second = self.post(
            "/api/v1/projects/project-1/connector-snapshots",
            after.model_dump(mode="json"),
            key="after",
            version=2,
        )
        self.assertEqual((first.status, second.status), (201, 201))
        baseline = self.store.get_connector_snapshot("project-1", "snapshot-11")
        baseline_source = next(
            item
            for item in baseline.snapshot.artifacts
            if item.domain.value == "hardware"
        )
        preview = self.post(
            "/api/v1/projects/project-1/change-previews",
            change_preview_command(baseline_source).model_dump(mode="json"),
            key="preview",
            version=3,
        )
        self.assertEqual(preview.status, 201)
        preview_hash = preview.json()["change_preview"]["preview_hash"]

        impact = self.post(
            "/api/v1/projects/project-1/change-impacts",
            AnalyzeChangeCommand(
                from_snapshot_id="snapshot-11", to_snapshot_id="snapshot-12"
            ).model_dump(mode="json"),
            key="impact",
            version=4,
        )
        analysis_hash = impact.json()["change_assessment"]["analysis_hash"]
        verification = self.post(
            "/api/v1/projects/project-1/plan-verifications",
            VerifyPlanCommand(
                preview_hash=preview_hash,
                actual_change_analysis_hash=analysis_hash,
            ).model_dump(mode="json"),
            key="verify-plan",
            version=5,
        )
        self.assertEqual(verification.status, 201)
        verification_hash = verification.json()["plan_verification"][
            "verification_hash"
        ]
        assessment = self.store.get_change_assessment(analysis_hash)
        snapshot = self.store.get_connector_snapshot("project-1", "snapshot-12")
        evidence: tuple[RawReleaseEvidence, ...] = (
            cost_record(snapshot.snapshot),
            build_evidence(assessment.assessment, snapshot.snapshot),
            *required_test_evidence(assessment.assessment, snapshot.snapshot),
        )
        evidence_ids: list[str] = []
        version = 6
        for index, item in enumerate(evidence, start=1):
            ingested = self.post(
                "/api/v1/projects/project-1/release-evidence",
                IngestReleaseEvidenceCommand(
                    analysis_hash=analysis_hash, evidence=item
                ).model_dump(mode="json"),
                key=f"evidence-{index}",
                version=version,
            )
            self.assertEqual(ingested.status, 201)
            evidence_ids.append(ingested.json()["release_evidence"]["evidence_id"])
            version += 1

        decision = self.post(
            "/api/v1/projects/project-1/release-decisions",
            EvaluateReleaseCommand(analysis_hash=analysis_hash).model_dump(mode="json"),
            key="decision",
            version=version,
        )
        self.assertEqual(decision.status, 201)
        decision_hash = decision.json()["release_decision"]["decision_hash"]
        self.assertEqual(
            decision.json()["release_decision"]["decision"]["report"]["status"],
            "ready",
        )

        read_targets = (
            "/api/v1/projects/project-1",
            "/api/v1/projects/project-1/connector-snapshots/snapshot-12",
            f"/api/v1/projects/project-1/change-impacts/{analysis_hash}",
            "/api/v1/projects/project-1/change-previews",
            f"/api/v1/projects/project-1/change-previews/{preview_hash}",
            "/api/v1/projects/project-1/plan-verifications",
            f"/api/v1/projects/project-1/plan-verifications/{verification_hash}",
            f"/api/v1/projects/project-1/release-evidence/{evidence_ids[0]}",
            f"/api/v1/projects/project-1/release-decisions/{decision_hash}",
        )
        for target in read_targets:
            response = self.api.handle(
                "GET", target, [("Host", self.api.expected_host)]
            )
            self.assertEqual(response.status, 200, target)
            self.assertIn("data", response.json())
        scoped_reads = (
            f"/api/v1/projects/project-2/change-previews/{preview_hash}",
            f"/api/v1/projects/project-2/plan-verifications/{verification_hash}",
        )
        for target in scoped_reads:
            response = self.api.handle(
                "GET", target, [("Host", self.api.expected_host)]
            )
            self.assertEqual(response.status, 404, target)

    def test_closed_loop_routes_create_and_read_immutable_plan(self) -> None:
        created = self.post(
            "/api/v1/projects",
            {"project_id": "project-1", "name": "Project One"},
            key="create",
            version=None,
        )
        self.assertEqual(created.status, 201)
        before = capture_command("before", "snapshot-11", NOW - timedelta(hours=2))
        after = capture_command("after", "snapshot-12", NOW - timedelta(hours=1))
        self.assertEqual(
            self.post(
                "/api/v1/projects/project-1/connector-snapshots",
                before.model_dump(mode="json"),
                key="before",
                version=1,
            ).status,
            201,
        )
        baseline = self.store.get_connector_snapshot("project-1", "snapshot-11")
        baseline_source = next(
            item
            for item in baseline.snapshot.artifacts
            if item.domain.value == "hardware"
        )
        preview = self.post(
            "/api/v1/projects/project-1/change-previews",
            change_preview_command(baseline_source).model_dump(mode="json"),
            key="preview",
            version=2,
        )
        preview_hash = preview.json()["change_preview"]["preview_hash"]
        proposals = self.post(
            "/api/v1/projects/project-1/design-proposals",
            {
                "goal": "Replace the EOL controller",
                "priority": "reliability",
                "constraints": "preserve 3.3V; preserve pinout",
                "preview_hash": preview_hash,
            },
            key="design",
            version=3,
        )
        self.assertEqual(proposals.status, 201)
        proposal_hash = proposals.json()["design_proposal"]["proposal_hash"]
        proposal_item = self.api.handle(
            "GET",
            f"/api/v1/projects/project-1/design-proposals/{proposal_hash}",
            [("Host", self.api.expected_host)],
        )
        self.assertEqual(proposal_item.status, 200)
        self.assertEqual(len(proposal_item.json()["data"]["alternatives"]), 2)

        self.assertEqual(
            self.post(
                "/api/v1/projects/project-1/connector-snapshots",
                after.model_dump(mode="json"),
                key="after",
                version=4,
            ).status,
            201,
        )
        impact = self.post(
            "/api/v1/projects/project-1/change-impacts",
            AnalyzeChangeCommand(
                from_snapshot_id="snapshot-11", to_snapshot_id="snapshot-12"
            ).model_dump(mode="json"),
            key="impact",
            version=5,
        )
        analysis_hash = impact.json()["change_assessment"]["analysis_hash"]
        release = self.post(
            "/api/v1/projects/project-1/release-decisions",
            {"analysis_hash": analysis_hash},
            key="release",
            version=6,
        )
        decision = release.json()["release_decision"]
        self.assertEqual(decision["decision"]["report"]["status"], "blocked")
        diagnosis = self.post(
            "/api/v1/projects/project-1/release-diagnoses",
            {"decision_hash": decision["decision_hash"]},
            key="diagnose",
            version=7,
        )
        self.assertEqual(diagnosis.status, 201)
        diagnosis_record = diagnosis.json()["release_diagnosis"]
        fix_hash = diagnosis_record["fix_recommendations"][0]["fix_proposal_hash"]
        plan = self.post(
            "/api/v1/projects/project-1/resolution-plans",
            {
                "diagnosis_hash": diagnosis_record["diagnosis_hash"],
                "fix_proposal_hash": fix_hash,
            },
            key="resolve",
            version=8,
        )
        self.assertEqual(plan.status, 201)
        plan_hash = plan.json()["resolution_plan"]["plan_hash"]
        plan_item = self.api.handle(
            "GET",
            f"/api/v1/projects/project-1/resolution-plans/{plan_hash}",
            [("Host", self.api.expected_host)],
        )
        self.assertEqual(plan_item.status, 200)
        self.assertEqual(
            plan_item.json()["data"]["plan_draft"]["priority"], "reliability"
        )
        cross_project = self.api.handle(
            "GET",
            f"/api/v1/projects/project-2/resolution-plans/{plan_hash}",
            [("Host", self.api.expected_host)],
        )
        self.assertEqual(cross_project.status, 404)

    def test_conversational_candidate_simulation_and_history_are_persisted(
        self,
    ) -> None:
        self.assertEqual(
            self.post(
                "/api/v1/projects",
                {"project_id": "project-1", "name": "Project One"},
                key="create-conversation",
                version=None,
            ).status,
            201,
        )
        self.assertEqual(
            self.post(
                "/api/v1/projects/project-1/connector-snapshots",
                capture_command(
                    "before", "snapshot-11", NOW - timedelta(hours=2)
                ).model_dump(mode="json"),
                key="conversation-baseline",
                version=1,
            ).status,
            201,
        )
        baseline = self.store.get_connector_snapshot("project-1", "snapshot-11")
        baseline_source = next(
            item
            for item in baseline.snapshot.artifacts
            if item.domain.value == "hardware"
        )
        preview = self.post(
            "/api/v1/projects/project-1/change-previews",
            change_preview_command(baseline_source).model_dump(mode="json"),
            key="conversation-preview",
            version=2,
        )
        proposal = self.post(
            "/api/v1/projects/project-1/design-proposals",
            {
                "goal": "Extend the robot arm while preserving payload capacity",
                "priority": "reliability",
                "constraints": "mass increase <= 5%",
                "preview_hash": preview.json()["change_preview"]["preview_hash"],
            },
            key="conversation-proposal",
            version=3,
        )
        proposal_hash = proposal.json()["design_proposal"]["proposal_hash"]
        candidate_nonce = "candidate-approval-nonce-value-00000001"
        candidate_payload = {
            "session_id": "SESSION-014",
            "candidate_id": "DC-014",
            "revision": 1,
            "proposal_hash": proposal_hash,
            "parameters": [
                {
                    "name": "upper_arm_length",
                    "current_value": "420 mm",
                    "proposed_value": "520 mm",
                    "source_refs": ["cad:upper-arm-rev4"],
                }
            ],
            "requirements": ["mass increase <= 5%"],
        }
        approval = self.post(
            "/api/v1/projects/project-1/design-candidate-approvals",
            {
                "approval_id": "candidate-approval-1",
                "approval_nonce": candidate_nonce,
                **candidate_payload,
            },
            key="approve-candidate",
            version=4,
        )
        self.assertEqual(approval.status, 201, approval.json())
        self.assertEqual(
            approval.json()["design_candidate_approval"]["nonce_hash"],
            "<redacted>",
        )
        candidate = self.post(
            "/api/v1/projects/project-1/design-candidates",
            {
                "approval_id": "candidate-approval-1",
                "approval_nonce": candidate_nonce,
                **candidate_payload,
                "confirmed_by": "local-operator",
            },
            key="confirm-candidate",
            version=5,
        )
        self.assertEqual(candidate.status, 201, candidate.json())
        self.assertEqual(
            candidate.json()["approval_receipt"]["approval_id"],
            "candidate-approval-1",
        )
        candidate_hash = candidate.json()["design_candidate"]["candidate_hash"]
        first_transition = self.post(
            "/api/v1/projects/project-1/design-transitions",
            {
                "session_id": "SESSION-014",
                "sequence": 1,
                "from_state": "idea",
                "to_state": "proposed",
            },
            key="transition-1",
            version=6,
        )
        self.assertEqual(first_transition.status, 201, first_transition.json())
        wrong_session = self.post(
            "/api/v1/projects/project-1/design-simulations",
            {
                "session_id": "SESSION-OTHER",
                "simulation_id": "SIM-WRONG",
                "candidate_hash": candidate_hash,
                "tool_ref": {"kind": "user", "identifier": "test-fixture"},
                "metrics": [
                    {
                        "metric": "maximum_deflection",
                        "actual": "4.1 mm",
                        "requirement": "<= 3.0 mm",
                        "verdict": "FAIL",
                    }
                ],
            },
            key="reject-cross-session-simulation",
            version=7,
        )
        self.assertEqual(wrong_session.status, 422, wrong_session.json())
        simulation = self.post(
            "/api/v1/projects/project-1/design-simulations",
            {
                "session_id": "SESSION-014",
                "simulation_id": "SIM-023",
                "candidate_hash": candidate_hash,
                "tool_ref": {
                    "kind": "plugin",
                    "identifier": "forge-demo-structural",
                    "version": "1.0.0",
                    "hash": "sha256:" + "a" * 64,
                },
                "metrics": [
                    {
                        "metric": "maximum_deflection",
                        "actual": "4.1 mm",
                        "requirement": "<= 3.0 mm",
                        "verdict": "FAIL",
                    }
                ],
            },
            key="bind-simulation",
            version=7,
        )
        self.assertEqual(simulation.status, 201, simulation.json())
        simulation_hash = simulation.json()["simulation_binding"]["simulation_hash"]
        transitions = (
            (2, "proposed", "selected", None, None),
            (3, "selected", "confirmed", candidate_hash, None),
            (4, "confirmed", "simulated", candidate_hash, simulation_hash),
        )
        version = 8
        for (
            sequence,
            from_state,
            to_state,
            candidate_ref,
            simulation_ref,
        ) in transitions:
            response = self.post(
                "/api/v1/projects/project-1/design-transitions",
                {
                    "session_id": "SESSION-014",
                    "sequence": sequence,
                    "from_state": from_state,
                    "to_state": to_state,
                    "candidate_hash": candidate_ref,
                    "simulation_hash": simulation_ref,
                },
                key=f"transition-{sequence}",
                version=version,
            )
            self.assertEqual(response.status, 201, response.json())
            version += 1
        missing_claim = self.post(
            "/api/v1/projects/project-1/conversational-claims",
            {
                "session_id": "SESSION-014",
                "claim_id": "claim-missing-simulation",
                "kind": "simulated",
                "statement": "This claim must not bind invented evidence.",
                "evidence_refs": ["simulation:sha256:" + "b" * 64],
            },
            key="reject-missing-claim-evidence",
            version=version,
        )
        self.assertEqual(missing_claim.status, 422, missing_claim.json())
        wrong_session_claim = self.post(
            "/api/v1/projects/project-1/conversational-claims",
            {
                "session_id": "SESSION-OTHER",
                "claim_id": "claim-cross-session",
                "kind": "simulated",
                "statement": "This claim must not cross session boundaries.",
                "evidence_refs": [f"simulation:{simulation_hash}"],
            },
            key="reject-cross-session-claim-evidence",
            version=version,
        )
        self.assertEqual(wrong_session_claim.status, 422, wrong_session_claim.json())
        self.assertEqual(
            self.post(
                "/api/v1/projects",
                {"project_id": "project-2", "name": "Project Two"},
                key="create-conversation-project-2",
                version=None,
            ).status,
            201,
        )
        cross_project_claim = self.post(
            "/api/v1/projects/project-2/conversational-claims",
            {
                "session_id": "SESSION-014",
                "claim_id": "claim-cross-project",
                "kind": "simulated",
                "statement": "This claim must not cross project boundaries.",
                "evidence_refs": [f"simulation:{simulation_hash}"],
            },
            key="reject-cross-project-claim-evidence",
            version=1,
            project_id="project-2",
        )
        self.assertEqual(cross_project_claim.status, 422, cross_project_claim.json())
        claim = self.post(
            "/api/v1/projects/project-1/conversational-claims",
            {
                "session_id": "SESSION-014",
                "claim_id": "claim-deflection-1",
                "kind": "simulated",
                "statement": "Maximum deflection exceeds the confirmed requirement.",
                "evidence_refs": [f"simulation:{simulation_hash}"],
            },
            key="claim-1",
            version=version,
        )
        self.assertEqual(claim.status, 201, claim.json())
        claim_hash = claim.json()["evidence_claim"]["claim_hash"]
        version += 1

        self.assertEqual(
            self.post(
                "/api/v1/projects/project-1/connector-snapshots",
                capture_command(
                    "after", "snapshot-12", NOW - timedelta(hours=1)
                ).model_dump(mode="json"),
                key="conversation-target",
                version=version,
            ).status,
            201,
        )
        version += 1
        impact = self.post(
            "/api/v1/projects/project-1/change-impacts",
            AnalyzeChangeCommand(
                from_snapshot_id="snapshot-11", to_snapshot_id="snapshot-12"
            ).model_dump(mode="json"),
            key="conversation-impact",
            version=version,
        )
        self.assertEqual(impact.status, 201, impact.json())
        version += 1
        analysis_hash = impact.json()["change_assessment"]["analysis_hash"]
        assessment = self.store.get_change_assessment(analysis_hash)
        target_snapshot = self.store.get_connector_snapshot("project-1", "snapshot-12")
        imported_test = required_test_evidence(
            assessment.assessment, target_snapshot.snapshot
        )[0]
        imported = self.post(
            "/api/v1/projects/project-1/release-evidence",
            IngestReleaseEvidenceCommand(
                analysis_hash=analysis_hash,
                evidence=imported_test,
            ).model_dump(mode="json"),
            key="conversation-imported-evidence",
            version=version,
        )
        self.assertEqual(imported.status, 201, imported.json())
        version += 1
        evidence_id = imported.json()["release_evidence"]["evidence_id"]
        accepted = self.post(
            "/api/v1/projects/project-1/design-transitions",
            {
                "session_id": "SESSION-014",
                "sequence": 5,
                "from_state": "simulated",
                "to_state": "accepted",
                "candidate_hash": candidate_hash,
                "simulation_hash": simulation_hash,
            },
            key="transition-5",
            version=version,
        )
        self.assertEqual(accepted.status, 201, accepted.json())
        version += 1
        fake_verified = self.post(
            "/api/v1/projects/project-1/design-transitions",
            {
                "session_id": "SESSION-014",
                "sequence": 6,
                "from_state": "accepted",
                "to_state": "verified",
                "evidence_refs": ["release-evidence:not-stored"],
            },
            key="reject-fake-verified-evidence",
            version=version,
        )
        self.assertEqual(fake_verified.status, 422, fake_verified.json())
        cross_project_verified = self.post(
            "/api/v1/projects/project-2/design-transitions",
            {
                "session_id": "SESSION-014",
                "sequence": 1,
                "from_state": "accepted",
                "to_state": "verified",
                "evidence_refs": [f"release-evidence:{evidence_id}"],
            },
            key="reject-cross-project-verified-evidence",
            version=1,
            project_id="project-2",
        )
        self.assertEqual(
            cross_project_verified.status, 422, cross_project_verified.json()
        )
        verified = self.post(
            "/api/v1/projects/project-1/design-transitions",
            {
                "session_id": "SESSION-014",
                "sequence": 6,
                "from_state": "accepted",
                "to_state": "verified",
                "evidence_refs": [f"release-evidence:{evidence_id}"],
            },
            key="transition-6",
            version=version,
        )
        self.assertEqual(verified.status, 201, verified.json())

        reads = (
            (
                "/api/v1/projects/project-1/design-candidates",
                candidate_hash,
            ),
            (
                f"/api/v1/projects/project-1/design-candidates/{candidate_hash}",
                candidate_hash,
            ),
            (
                "/api/v1/projects/project-1/design-simulations",
                simulation_hash,
            ),
            (
                f"/api/v1/projects/project-1/design-simulations/{simulation_hash}",
                simulation_hash,
            ),
            (
                "/api/v1/projects/project-1/conversational-claims",
                claim_hash,
            ),
            (
                f"/api/v1/projects/project-1/conversational-claims/{claim_hash}",
                claim_hash,
            ),
            (
                "/api/v1/projects/project-1/design-transitions",
                "SESSION-014",
            ),
        )
        for target, expected in reads:
            response = self.api.handle(
                "GET", target, [("Host", self.api.expected_host)]
            )
            self.assertEqual(response.status, 200, (target, response.json()))
            self.assertIn(expected, response.body.decode("utf-8"))

        stale = self.post(
            "/api/v1/projects/project-1/design-transitions",
            {
                "session_id": "SESSION-014",
                "sequence": 5,
                "from_state": "simulated",
                "to_state": "accepted",
                "simulation_hash": simulation_hash,
            },
            key="stale-transition",
            version=4,
        )
        self.assertEqual(stale.status, 409)

    def test_external_evidence_plan_routes_store_and_return_operator_plan(self) -> None:
        self.post(
            "/api/v1/projects",
            CreateProjectCommand(project_id="project-1", name="Project One").model_dump(
                mode="json"
            ),
            key="create",
            version=None,
        )
        self.post(
            "/api/v1/projects/project-1/connector-snapshots",
            capture_command(
                "before", "snapshot-11", NOW - timedelta(hours=2)
            ).model_dump(mode="json"),
            key="before",
            version=1,
        )
        created = self.post(
            "/api/v1/projects/project-1/external-evidence-plans",
            external_evidence_plan_command().model_dump(mode="json"),
            key="external-plan",
            version=2,
        )
        self.assertEqual(created.status, 201)
        record = created.json()["external_evidence_plan"]
        plan_hash = record["plan_hash"]
        self.assertEqual(
            {item["tier"] for item in record["plan"]["required_evidence"]},
            {"simulation", "bench", "hil", "physical_device"},
        )

        collection = self.api.handle(
            "GET",
            "/api/v1/projects/project-1/external-evidence-plans",
            [("Host", self.api.expected_host)],
        )
        item = self.api.handle(
            "GET",
            f"/api/v1/projects/project-1/external-evidence-plans/{plan_hash}",
            [("Host", self.api.expected_host)],
        )
        self.assertEqual(collection.status, 200)
        self.assertEqual(collection.json()["data"], [record])
        self.assertEqual(item.status, 200)
        self.assertEqual(item.json()["data"], record)

        self.post(
            "/api/v1/projects/project-1/connector-snapshots",
            capture_command(
                "after", "snapshot-12", NOW - timedelta(hours=1)
            ).model_dump(mode="json"),
            key="after",
            version=3,
        )
        impact = self.post(
            "/api/v1/projects/project-1/change-impacts",
            AnalyzeChangeCommand(
                from_snapshot_id="snapshot-11", to_snapshot_id="snapshot-12"
            ).model_dump(mode="json"),
            key="impact",
            version=4,
        )
        analysis_hash = impact.json()["change_assessment"]["analysis_hash"]
        verified = self.post(
            "/api/v1/projects/project-1/external-evidence-plan-verifications",
            {
                "evidence_plan_hash": plan_hash,
                "actual_change_analysis_hash": analysis_hash,
                "imported_evidence_ids": [],
            },
            key="verify-external-plan",
            version=5,
        )
        self.assertEqual(verified.status, 201)
        verification = verified.json()["external_evidence_plan_verification"]
        self.assertEqual(
            verification["verification"]["check_result"], "differs_from_plan"
        )
        self.assertEqual(
            len(verification["verification"]["missing_required_evidence_ids"]),
            4,
        )

    def test_host_origin_csrf_and_preflight_are_strict(self) -> None:
        health = self.api.handle(
            "GET", "/api/v1/health", [("Host", self.api.expected_host)]
        )
        self.assertEqual(health.status, 200)
        self.assertEqual(health.headers["Cache-Control"], "no-store")
        self.assertEqual(health.headers["X-Content-Type-Options"], "nosniff")

        invalid_requests: tuple[list[tuple[str, str]], ...] = (
            [],
            [("Host", "localhost:43127")],
            [("Host", self.api.expected_host), ("Host", self.api.expected_host)],
        )
        for headers in invalid_requests:
            response = self.api.handle("GET", "/api/v1/health", headers)
            self.assertEqual(response.status, 400)

        body = json_bytes({"project_id": "project-1", "name": "Project One"})
        base = self.mutation_headers(body)
        no_origin = [item for item in base if item[0] != "Origin"]
        wrong_origin = [
            (name, "http://evil.invalid") if name == "Origin" else (name, value)
            for name, value in base
        ]
        no_csrf = [item for item in base if item[0] != "X-FORGE-CSRF"]
        self.assertEqual(
            self.api.handle("POST", "/api/v1/projects", no_origin, body).status, 403
        )
        self.assertEqual(
            self.api.handle("POST", "/api/v1/projects", wrong_origin, body).status,
            403,
        )
        self.assertEqual(
            self.api.handle("POST", "/api/v1/projects", no_csrf, body).status, 400
        )

        preflight_headers = [
            ("Host", self.api.expected_host),
            ("Origin", self.api.expected_origin),
            ("Access-Control-Request-Method", "POST"),
            (
                "Access-Control-Request-Headers",
                "content-type,idempotency-key,if-match,x-forge-csrf",
            ),
        ]
        preflight = self.api.handle("OPTIONS", "/api/v1/projects", preflight_headers)
        self.assertEqual(preflight.status, 204)
        self.assertEqual(preflight.body, b"")
        missing_origin = [item for item in preflight_headers if item[0] != "Origin"]
        self.assertEqual(
            self.api.handle("OPTIONS", "/api/v1/projects", missing_origin).status,
            403,
        )
        forbidden = preflight_headers[:-1] + [
            ("Access-Control-Request-Headers", "authorization")
        ]
        self.assertEqual(
            self.api.handle("OPTIONS", "/api/v1/projects", forbidden).status, 403
        )

    def test_json_framing_media_type_and_validation_fail_closed(self) -> None:
        target = "/api/v1/projects"
        valid = json_bytes({"project_id": "project-1", "name": "Project One"})
        cases: tuple[tuple[int, list[tuple[str, str]], bytes], ...] = (
            (
                411,
                [
                    item
                    for item in self.mutation_headers(valid)
                    if item[0] != "Content-Length"
                ],
                valid,
            ),
            (
                400,
                self.mutation_headers(valid) + [("Content-Length", str(len(valid)))],
                valid,
            ),
            (
                400,
                self.mutation_headers(valid) + [("Transfer-Encoding", "chunked")],
                valid,
            ),
            (
                415,
                [
                    (name, "application/json; charset=utf-8")
                    if name == "Content-Type"
                    else (name, value)
                    for name, value in self.mutation_headers(valid)
                ],
                valid,
            ),
            (
                400,
                self.mutation_headers(b'{"name":"a","name":"b"}'),
                b'{"name":"a","name":"b"}',
            ),
            (400, self.mutation_headers(b'{"value":NaN}'), b'{"value":NaN}'),
            (400, self.mutation_headers(b"[]"), b"[]"),
            (400, self.mutation_headers(b"{broken"), b"{broken"),
            (
                413,
                self.mutation_headers(b"x" * 1_048_577),
                b"x" * 1_048_577,
            ),
        )
        for expected, headers, body in cases:
            response = self.api.handle("POST", target, headers, body)
            self.assertEqual(response.status, expected, response.json())
            self.assertNotIn(str(Path(self.temporary.name)), response.body.decode())

        validation = self.post(
            target, {"project_id": "bad/id", "name": "x"}, key="bad", version=None
        )
        self.assertEqual(validation.status, 422)
        self.assertEqual(validation.json()["error"]["code"], "validation_failed")
        self.assertNotIn("input", validation.body.decode())

    def test_change_preview_api_rejects_stale_and_forged_inputs(self) -> None:
        self.assertEqual(
            self.post(
                "/api/v1/projects",
                {"project_id": "project-1", "name": "Project One"},
                key="create",
                version=None,
            ).status,
            201,
        )
        baseline_response = self.post(
            "/api/v1/projects/project-1/connector-snapshots",
            capture_command(
                "before",
                "snapshot-11",
                NOW - timedelta(hours=2),
            ).model_dump(mode="json"),
            key="baseline",
            version=1,
        )
        self.assertEqual(baseline_response.status, 201)
        baseline = self.store.get_connector_snapshot("project-1", "snapshot-11")
        baseline_source = next(
            item
            for item in baseline.snapshot.artifacts
            if item.domain.value == "hardware"
        )
        command = change_preview_command(baseline_source).model_dump(mode="json")

        stale = self.post(
            "/api/v1/projects/project-1/change-previews",
            command,
            key="stale-preview",
            version=1,
        )
        self.assertEqual(stale.status, 409)

        forged_hash = self.post(
            "/api/v1/projects/project-1/change-previews",
            command | {"scenario_hash": "sha256:" + "0" * 64},
            key="forged-hash",
            version=2,
        )
        self.assertEqual(forged_hash.status, 422)
        self.assertEqual(forged_hash.json()["error"]["code"], "validation_failed")

        forged_source = json.loads(json.dumps(command))
        forged_source["changes"][0]["before"]["source_ref"]["content_hash"] = (
            "sha256:" + "7" * 64
        )
        forged_baseline = self.post(
            "/api/v1/projects/project-1/change-previews",
            forged_source,
            key="forged-source",
            version=2,
        )
        self.assertEqual(forged_baseline.status, 422)
        self.assertEqual(
            forged_baseline.json()["error"]["code"],
            "domain_validation_failed",
        )

    def test_routes_methods_conflicts_and_targets_are_bounded(self) -> None:
        created = self.post(
            "/api/v1/projects",
            {"project_id": "project-1", "name": "Project One"},
            key="create",
            version=None,
        )
        self.assertEqual(created.status, 201)

        conflict = self.post(
            "/api/v1/projects",
            {"project_id": "project-1", "name": "Changed Name"},
            key="create",
            version=None,
        )
        self.assertEqual(conflict.status, 409)
        stale = self.post(
            "/api/v1/projects/project-1/connector-snapshots",
            capture_command("before", "snapshot-11", NOW).model_dump(mode="json"),
            key="stale",
            version=2,
        )
        self.assertEqual(stale.status, 409)

        for target in (
            "/api/v1/projects?x=1",
            "/api/v1/%2e%2e/.git/config",
            "/api/v1/../secret",
            "/etc/passwd",
        ):
            response = self.api.handle(
                "GET", target, [("Host", self.api.expected_host)]
            )
            self.assertIn(response.status, {400, 404})
            self.assertNotIn("/Users/", response.body.decode())

        method = self.api.handle(
            "DELETE",
            "/api/v1/projects/project-1",
            [
                ("Host", self.api.expected_host),
                ("Origin", self.api.expected_origin),
            ],
        )
        self.assertEqual(method.status, 405)
        self.assertEqual(method.headers["Allow"], "GET")
        missing = self.api.handle(
            "GET",
            "/api/v1/projects/missing",
            [("Host", self.api.expected_host)],
        )
        self.assertEqual(missing.status, 404)

    def test_server_binds_only_ipv4_loopback_and_hides_runtime_banner(self) -> None:
        for host in ("0.0.0.0", "localhost", "::1"):
            with self.assertRaises(ValueError):
                create_loopback_server(self.service, host=host)
        with self.assertRaises(ValueError):
            create_loopback_server(self.service, csrf_secret=b"")
        with self.assertRaises(ValueError):
            LoopbackAPI(
                self.service,
                port=1,
                csrf_secret=b"secret",
                local_installation_id="bad/path",
            )
        with self.assertRaises(ValueError):
            LoopbackAPI(
                self.service,
                port=1,
                csrf_secret=b"secret",
                local_installation_id="installation-1",
                max_request_bytes=0,
            )

        server = create_loopback_server(
            self.service,
            csrf_secret=b"server-secret",
            local_installation_id="installation-1",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertEqual(server.server_address[0], "127.0.0.1")
            connection = http.client.HTTPConnection(
                "127.0.0.1", int(server.server_address[1]), timeout=2
            )
            connection.request("GET", "/api/v1/health")
            response = connection.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(response.getheader("Server"), "FORGE")
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
