from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from forge_core.access_control import (  # noqa: E402
    Actor,
    AuthorizationRequest,
    Permission,
    Role,
    authorize,
    build_audit_event,
)
from forge_core.constraints import BOMLine, QuoteSnapshot, evaluate_cost  # noqa: E402
from forge_core.conversational_design import (  # noqa: E402
    ClaimConfidence,
    DesignConversationState,
    DesignStateTransition,
    EvidenceClaimKind,
    build_evidence_claim,
)
from forge_core.conversational_persistence import (  # noqa: E402
    StoredDesignStateTransition,
    StoredEvidenceClaim,
)
from forge_core.design import (  # noqa: E402
    ArtifactKind,
    EvidenceClass,
    EvidenceRecord,
    RevisionArtifact,
    SystemDesignRevision,
    dependency_hash,
    revision_hash,
)
from forge_core.hashing import canonical_sha256  # noqa: E402
from forge_core.models import (  # noqa: E402
    AnalysisResult,
    AnalysisRunRecord,
    ApprovalRef,
    ApprovalSubjectKind,
    EngineeringSpec,
    PluginRef,
    PreflightResult,
    PreparedAnalysis,
    PreparedAnalysisBinding,
    Quantity,
    Requirement,
    RequirementPriority,
    RunLifecycleStatus,
    RunManifest,
    RunStateEvent,
    RunStatus,
    SourceRef,
    SpecStatus,
    Verdict,
    VerificationBundle,
    VerificationResult,
)
from forge_core.persistence import (  # noqa: E402
    CorruptRecordError,
    IdempotencyConflictError,
    IdempotencyRecord,
    IntegrityConflictError,
    MigrationError,
    ProjectRecord,
    RecordNotFoundError,
    SpecStateEvent,
    StorageBusyError,
    StoredCostEvaluation,
    StoredPreparation,
    StoredRevision,
    StoredRun,
    StoredSpec,
    VersionConflictError,
)
from forge_core.release_readiness import (  # noqa: E402
    DEFAULT_RELEASE_READINESS_POLICY,
)
from forge_core.sqlite_store import (  # noqa: E402
    LOCAL_BOOTSTRAP_ACTOR_ID,
    LOCAL_BOOTSTRAP_ORG_ID,
    AtomicProjectWrite,
    SQLiteEvidenceStore,
)

PLUGIN = PluginRef(
    plugin_id="historical.fixture",
    plugin_version="1.0.0",
    schema_version="1.0.0",
    artifact_hash="sha256:" + "3" * 64,
)


def quantity(value: float) -> Quantity:
    return Quantity(
        value=value,
        unit="1",
        dimension="ratio",
        source=SourceRef(kind="user", identifier="test:persistence"),
    )


def make_spec() -> EngineeringSpec:
    return EngineeringSpec(
        project_id="project-1",
        spec_id="spec-1",
        spec_version=1,
        status=SpecStatus.APPROVED,
        intent="historical immutable analysis record",
        model=PLUGIN,
        parameters={"input_value": quantity(2.0)},
        requirements=(
            Requirement(
                id="minimum_score",
                metric="fixture_score",
                operator=">=",
                target=quantity(4.0),
                priority=RequirementPriority.REQUIRED,
            ),
        ),
        approved_at=datetime(2026, 8, 27, tzinfo=UTC),
    )


def make_revision(number: int = 1) -> SystemDesignRevision:
    required = (
        ArtifactKind.SYSTEM_SPEC,
        ArtifactKind.INTERFACE,
        ArtifactKind.BOM,
        ArtifactKind.BUDGET_POLICY,
    )
    return SystemDesignRevision(
        project_id="project-1",
        revision_id=f"revision-{number}",
        revision_number=number,
        parent_revision_id="revision-1" if number > 1 else None,
        approved_at=datetime(2026, 8, 27, tzinfo=UTC),
        artifacts=tuple(
            RevisionArtifact(
                artifact_id=f"artifact:{item.value}",
                kind=item,
                version=str(number),
                content_hash=f"sha256:{number:064x}",
            )
            for item in required
        ),
    )


def approval_for(
    spec: EngineeringSpec,
    *,
    revision: SystemDesignRevision | None = None,
) -> tuple[ApprovalRef, ...]:
    assert spec.approved_at is not None
    approvals = [
        ApprovalRef(
            approval_id=f"approval:{spec.spec_id}:{spec.spec_version}",
            subject_kind=ApprovalSubjectKind.ENGINEERING_SPEC,
            project_id=spec.project_id,
            subject_id=spec.spec_id,
            subject_version=spec.spec_version,
            subject_hash=canonical_sha256(spec),
            approved_by="local-user",
            approved_at=spec.approved_at,
        )
    ]
    if revision is not None:
        approvals.append(
            ApprovalRef(
                approval_id=f"approval:{revision.revision_id}",
                subject_kind=ApprovalSubjectKind.SYSTEM_DESIGN_REVISION,
                project_id=revision.project_id,
                subject_id=revision.revision_id,
                subject_version=revision.revision_number,
                subject_hash=revision_hash(revision),
                approved_by="local-user",
                approved_at=revision.approved_at,
            )
        )
    return tuple(approvals)


def make_prepared(
    spec: EngineeringSpec,
    approval_refs: tuple[ApprovalRef, ...],
    *,
    preparation_id: str,
    revision: SystemDesignRevision | None = None,
) -> PreparedAnalysis:
    binding = PreparedAnalysisBinding(
        project_id=spec.project_id,
        spec_id=spec.spec_id,
        spec_version=spec.spec_version,
        spec_hash=canonical_sha256(spec),
        revision_id=None if revision is None else revision.revision_id,
        revision_hash=None if revision is None else revision_hash(revision),
        revision_number=None if revision is None else revision.revision_number,
        engine_version="historical-fixture-1",
        policy_version="historical-policy-1",
        plugin=PLUGIN,
        input_hash=canonical_sha256(spec),
        expected_requirement_ids=tuple(item.id for item in spec.requirements),
        expected_metrics=tuple(item.metric for item in spec.requirements),
        approval_refs=approval_refs,
        preflight=PreflightResult(
            spec_id=spec.spec_id,
            spec_version=spec.spec_version,
            accepted=True,
        ),
    )
    return PreparedAnalysis(
        preparation_id=preparation_id,
        binding=binding,
        prepare_hash=canonical_sha256(binding),
    )


def successful_run(
    run_id: str, prepared: PreparedAnalysis
) -> tuple[AnalysisRunRecord, VerificationBundle]:
    observed = quantity(4.0)
    target = quantity(4.0)
    result = AnalysisResult(
        outputs={"fixture_score": observed},
        formula_id="historical.fixture",
        formula_version="1.0.0",
    )
    verification = VerificationResult(
        requirement_id="minimum_score",
        priority=RequirementPriority.REQUIRED,
        status=Verdict.PASS,
        observed=observed,
        target=target,
        margin=0.0,
        evidence_refs=("analysis:fixture_score",),
    )
    bundle = VerificationBundle(results=(verification,), overall_verdict=Verdict.PASS)
    manifest = RunManifest(
        spec_hash=prepared.binding.spec_hash,
        plugin_id=prepared.binding.plugin.plugin_id,
        plugin_version=prepared.binding.plugin.plugin_version,
        plugin_schema_version=prepared.binding.plugin.schema_version,
        plugin_artifact_hash=prepared.binding.plugin.artifact_hash,
        engine_version=prepared.binding.engine_version,
        policy_version=prepared.binding.policy_version,
        input_hash=prepared.binding.input_hash,
        output_hash=canonical_sha256(result),
        verification_hash=canonical_sha256(bundle),
    )
    return (
        AnalysisRunRecord(
            run_id=run_id,
            status=RunStatus.SUCCEEDED,
            analysis_result=result,
            manifest=manifest,
        ),
        bundle,
    )


class SQLiteEvidenceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "forge.db"
        self.store = SQLiteEvidenceStore(self.path)
        self.base = datetime(2026, 8, 27, tzinfo=UTC)
        self.project = ProjectRecord(
            project_id="project-1",
            name="Project One",
            version=1,
            created_at=self.base,
            updated_at=self.base,
        )
        self.spec = make_spec()
        self.spec_approval = approval_for(self.spec)[0]
        self.prepared = make_prepared(
            self.spec,
            (self.spec_approval,),
            preparation_id="preparation-1",
        )

    def _stored_spec(self, spec: EngineeringSpec | None = None) -> StoredSpec:
        selected = self.spec if spec is None else spec
        return StoredSpec(
            project_id=selected.project_id,
            spec_id=selected.spec_id,
            spec_version=selected.spec_version,
            spec_hash=canonical_sha256(selected),
            spec=selected,
            stored_at=self.base,
        )

    def _store_prepared(self) -> int:
        self.store.create_project(self.project)
        version = self.store.store_spec(self._stored_spec(), expected_project_version=1)
        version = self.store.store_approval(
            self.spec_approval, expected_project_version=version
        )
        return self.store.store_preparation(
            StoredPreparation(
                project_id="project-1",
                preparation=self.prepared,
                stored_at=self.base,
            ),
            expected_project_version=version,
        )

    def _event(
        self,
        sequence: int,
        previous: RunLifecycleStatus | None,
        status: RunLifecycleStatus,
        *,
        result: AnalysisRunRecord | None = None,
        run_id: str = "run-1",
    ) -> RunStateEvent:
        return RunStateEvent(
            event_id=f"{run_id}-event-{sequence}",
            project_id="project-1",
            run_id=run_id,
            preparation_id=self.prepared.preparation_id,
            prepare_hash=self.prepared.prepare_hash,
            sequence=sequence,
            previous_status=previous,
            status=status,
            actor="test",
            reason_code="test_transition",
            occurred_at=self.base + timedelta(seconds=sequence),
            run_record_hash=(None if result is None else canonical_sha256(result)),
        )

    def _store_running(self) -> int:
        version = self._store_prepared()
        transitions = (
            self._event(1, None, RunLifecycleStatus.PREPARED),
            self._event(
                2,
                RunLifecycleStatus.PREPARED,
                RunLifecycleStatus.QUEUED,
            ),
            self._event(
                3,
                RunLifecycleStatus.QUEUED,
                RunLifecycleStatus.RUNNING,
            ),
        )
        for event in transitions:
            version = self.store.append_run_event(
                event, expected_project_version=version
            )
        return version

    def _drop_access_control_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute("DROP TABLE cad_geometry_assets")
        connection.execute("DROP TABLE conversation_runtime_results")
        connection.execute("DROP TABLE knowledge_sources")
        connection.execute("DROP TABLE audit_events")
        connection.execute("DROP TABLE project_access")
        connection.execute("DROP TABLE memberships")
        connection.execute("DROP TABLE actors")
        connection.execute("DROP TABLE organizations")
        connection.execute("DELETE FROM schema_migrations WHERE version = 10")
        connection.execute("DELETE FROM schema_migrations WHERE version = 9")
        connection.execute("DELETE FROM schema_migrations WHERE version = 8")

    def test_schema_initialization_pragmas_reopen_and_newer_rejection(self) -> None:
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 10
            )
            self.assertEqual(
                connection.execute("PRAGMA journal_mode").fetchone()[0], "wal"
            )
            self.assertEqual(
                connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0],
                10,
            )
        reopened = SQLiteEvidenceStore(self.path)
        reopened.create_project(self.project)
        self.assertEqual(reopened.get_project("project-1"), self.project)
        self.assertEqual(
            reopened.get_actor(LOCAL_BOOTSTRAP_ACTOR_ID).display_name,
            "Local Operator",
        )
        self.assertEqual(
            reopened.get_membership(
                LOCAL_BOOTSTRAP_ORG_ID, LOCAL_BOOTSTRAP_ACTOR_ID
            ).role,
            Role.ADMIN,
        )
        self.assertTrue(
            reopened.get_project_access(
                LOCAL_BOOTSTRAP_ORG_ID,
                "project-1",
                LOCAL_BOOTSTRAP_ACTOR_ID,
            ).active
        )

        newer = Path(self.temporary.name) / "newer.db"
        with sqlite3.connect(newer) as connection:
            connection.execute("PRAGMA user_version = 11")
        with self.assertRaises(MigrationError):
            SQLiteEvidenceStore(newer)
        with sqlite3.connect(newer) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 11
            )

    def test_schema_v1_is_migrated_to_v7_without_losing_records(self) -> None:
        self.store.create_project(self.project)
        with sqlite3.connect(self.path) as connection:
            self._drop_access_control_schema(connection)
            connection.execute("DROP TABLE design_state_transitions")
            connection.execute("DROP TABLE conversational_evidence_claims")
            connection.execute("DROP TABLE simulation_bindings")
            connection.execute("DROP TABLE design_candidates")
            connection.execute("DROP TABLE resolution_plans")
            connection.execute("DROP TABLE release_diagnoses")
            connection.execute("DROP TABLE design_proposals")
            connection.execute("DROP TABLE external_evidence_plan_verifications")
            connection.execute("DROP TABLE external_evidence_plans")
            connection.execute("DROP TABLE plan_verifications")
            connection.execute("DROP TABLE change_previews")
            connection.execute("DROP TABLE release_decisions")
            connection.execute("DROP TABLE release_evidence")
            connection.execute("DROP TABLE change_assessments")
            connection.execute("DROP TABLE connector_snapshots")
            connection.execute("DROP TABLE release_policies")
            connection.execute("DROP TABLE cost_evaluations")
            connection.execute("DROP TABLE spec_state_events")
            connection.execute("DROP TABLE approved_specs")
            connection.execute("DELETE FROM schema_migrations WHERE version = 7")
            connection.execute("DELETE FROM schema_migrations WHERE version = 6")
            connection.execute("DELETE FROM schema_migrations WHERE version = 5")
            connection.execute("DELETE FROM schema_migrations WHERE version = 4")
            connection.execute("DELETE FROM schema_migrations WHERE version = 3")
            connection.execute("DELETE FROM schema_migrations WHERE version = 2")
            connection.execute("PRAGMA user_version = 1")

        migrated = SQLiteEvidenceStore(self.path)
        self.assertEqual(migrated.get_project(self.project.project_id), self.project)
        migrated_policy = migrated.get_release_policy(self.project.project_id)
        self.assertEqual(migrated_policy.policy, DEFAULT_RELEASE_READINESS_POLICY)
        self.assertEqual(
            migrated_policy.policy_hash,
            canonical_sha256(DEFAULT_RELEASE_READINESS_POLICY),
        )
        self.assertEqual(migrated_policy.stored_at, self.project.created_at)
        self.assertEqual(
            migrated.get_membership(
                LOCAL_BOOTSTRAP_ORG_ID, LOCAL_BOOTSTRAP_ACTOR_ID
            ).role,
            Role.ADMIN,
        )
        self.assertTrue(
            migrated.get_project_access(
                LOCAL_BOOTSTRAP_ORG_ID,
                self.project.project_id,
                LOCAL_BOOTSTRAP_ACTOR_ID,
            ).active
        )
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 10
            )
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertTrue(
            {
                "approved_specs",
                "spec_state_events",
                "cost_evaluations",
                "connector_snapshots",
                "change_assessments",
                "release_evidence",
                "release_decisions",
                "release_policies",
                "change_previews",
                "plan_verifications",
                "external_evidence_plans",
                "external_evidence_plan_verifications",
                "design_proposals",
                "design_candidates",
                "simulation_bindings",
                "conversational_evidence_claims",
                "design_state_transitions",
                "release_diagnoses",
                "resolution_plans",
                "organizations",
                "actors",
                "memberships",
                "project_access",
                "audit_events",
                "knowledge_sources",
                "conversation_runtime_results",
                "cad_geometry_assets",
            }
            <= tables
        )

    def test_schema_v2_is_migrated_to_v7_without_losing_records(self) -> None:
        self.store.create_project(self.project)
        with sqlite3.connect(self.path) as connection:
            self._drop_access_control_schema(connection)
            connection.execute("DROP TABLE design_state_transitions")
            connection.execute("DROP TABLE conversational_evidence_claims")
            connection.execute("DROP TABLE simulation_bindings")
            connection.execute("DROP TABLE design_candidates")
            connection.execute("DROP TABLE resolution_plans")
            connection.execute("DROP TABLE release_diagnoses")
            connection.execute("DROP TABLE design_proposals")
            connection.execute("DROP TABLE external_evidence_plan_verifications")
            connection.execute("DROP TABLE external_evidence_plans")
            connection.execute("DROP TABLE plan_verifications")
            connection.execute("DROP TABLE change_previews")
            connection.execute("DROP TABLE release_decisions")
            connection.execute("DROP TABLE release_evidence")
            connection.execute("DROP TABLE change_assessments")
            connection.execute("DROP TABLE connector_snapshots")
            connection.execute("DROP TABLE release_policies")
            connection.execute("DELETE FROM schema_migrations WHERE version = 7")
            connection.execute("DELETE FROM schema_migrations WHERE version = 6")
            connection.execute("DELETE FROM schema_migrations WHERE version = 5")
            connection.execute("DELETE FROM schema_migrations WHERE version = 4")
            connection.execute("DELETE FROM schema_migrations WHERE version = 3")
            connection.execute("PRAGMA user_version = 2")

        migrated = SQLiteEvidenceStore(self.path)

        self.assertEqual(migrated.get_project(self.project.project_id), self.project)
        migrated_policy = migrated.get_release_policy(self.project.project_id)
        self.assertEqual(migrated_policy.policy, DEFAULT_RELEASE_READINESS_POLICY)
        self.assertEqual(
            migrated_policy.policy_hash,
            canonical_sha256(DEFAULT_RELEASE_READINESS_POLICY),
        )
        self.assertEqual(migrated_policy.stored_at, self.project.created_at)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 10
            )
            self.assertEqual(
                connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0],
                10,
            )

    def test_schema_v3_is_migrated_to_v7_without_losing_records(self) -> None:
        self.store.create_project(self.project)
        with sqlite3.connect(self.path) as connection:
            self._drop_access_control_schema(connection)
            connection.execute("DROP TABLE design_state_transitions")
            connection.execute("DROP TABLE conversational_evidence_claims")
            connection.execute("DROP TABLE simulation_bindings")
            connection.execute("DROP TABLE design_candidates")
            connection.execute("DROP TABLE resolution_plans")
            connection.execute("DROP TABLE release_diagnoses")
            connection.execute("DROP TABLE design_proposals")
            connection.execute("DROP TABLE external_evidence_plan_verifications")
            connection.execute("DROP TABLE external_evidence_plans")
            connection.execute("DROP TABLE plan_verifications")
            connection.execute("DROP TABLE change_previews")
            connection.execute("DELETE FROM schema_migrations WHERE version = 7")
            connection.execute("DELETE FROM schema_migrations WHERE version = 6")
            connection.execute("DELETE FROM schema_migrations WHERE version = 5")
            connection.execute("DELETE FROM schema_migrations WHERE version = 4")
            connection.execute("PRAGMA user_version = 3")

        migrated = SQLiteEvidenceStore(self.path)

        self.assertEqual(migrated.get_project(self.project.project_id), self.project)
        self.assertEqual(migrated.list_change_previews(self.project.project_id), ())
        self.assertEqual(migrated.list_plan_verifications(self.project.project_id), ())
        self.assertEqual(
            migrated.list_external_evidence_plans(self.project.project_id), ()
        )
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 10
            )

    def test_schema_v4_is_migrated_to_v7_without_losing_records(self) -> None:
        self.store.create_project(self.project)
        with sqlite3.connect(self.path) as connection:
            self._drop_access_control_schema(connection)
            connection.execute("DROP TABLE design_state_transitions")
            connection.execute("DROP TABLE conversational_evidence_claims")
            connection.execute("DROP TABLE simulation_bindings")
            connection.execute("DROP TABLE design_candidates")
            connection.execute("DROP TABLE resolution_plans")
            connection.execute("DROP TABLE release_diagnoses")
            connection.execute("DROP TABLE design_proposals")
            connection.execute("DROP TABLE external_evidence_plan_verifications")
            connection.execute("DROP TABLE external_evidence_plans")
            connection.execute("DELETE FROM schema_migrations WHERE version = 7")
            connection.execute("DELETE FROM schema_migrations WHERE version = 6")
            connection.execute("DELETE FROM schema_migrations WHERE version = 5")
            connection.execute("PRAGMA user_version = 4")

        migrated = SQLiteEvidenceStore(self.path)

        self.assertEqual(migrated.get_project(self.project.project_id), self.project)
        self.assertEqual(
            migrated.list_external_evidence_plans(self.project.project_id), ()
        )
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 10
            )

    def test_schema_v5_is_migrated_to_v7_with_empty_resolution_ledgers(self) -> None:
        self.store.create_project(self.project)
        with sqlite3.connect(self.path) as connection:
            self._drop_access_control_schema(connection)
            connection.execute("DROP TABLE design_state_transitions")
            connection.execute("DROP TABLE conversational_evidence_claims")
            connection.execute("DROP TABLE simulation_bindings")
            connection.execute("DROP TABLE design_candidates")
            connection.execute("DROP TABLE resolution_plans")
            connection.execute("DROP TABLE release_diagnoses")
            connection.execute("DROP TABLE design_proposals")
            connection.execute("DELETE FROM schema_migrations WHERE version = 7")
            connection.execute("DELETE FROM schema_migrations WHERE version = 6")
            connection.execute("PRAGMA user_version = 5")

        migrated = SQLiteEvidenceStore(self.path)

        self.assertEqual(migrated.get_project(self.project.project_id), self.project)
        self.assertEqual(migrated.list_design_proposals(self.project.project_id), ())
        self.assertEqual(migrated.list_design_candidates(self.project.project_id), ())
        self.assertEqual(migrated.list_simulation_bindings(self.project.project_id), ())
        self.assertEqual(migrated.list_evidence_claims(self.project.project_id), ())
        self.assertEqual(
            migrated.list_design_state_transitions(
                self.project.project_id, "session-1"
            ),
            (),
        )
        self.assertEqual(migrated.list_release_diagnoses(self.project.project_id), ())
        self.assertEqual(migrated.list_resolution_plans(self.project.project_id), ())
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 10
            )

    def test_schema_v6_is_migrated_to_v7_with_empty_conversation_ledgers(
        self,
    ) -> None:
        self.store.create_project(self.project)
        with sqlite3.connect(self.path) as connection:
            self._drop_access_control_schema(connection)
            connection.execute("DROP TABLE design_state_transitions")
            connection.execute("DROP TABLE conversational_evidence_claims")
            connection.execute("DROP TABLE simulation_bindings")
            connection.execute("DROP TABLE design_candidates")
            connection.execute("DELETE FROM schema_migrations WHERE version = 7")
            connection.execute("PRAGMA user_version = 6")

        migrated = SQLiteEvidenceStore(self.path)

        self.assertEqual(migrated.get_project(self.project.project_id), self.project)
        self.assertEqual(migrated.list_design_candidates("project-1"), ())
        self.assertEqual(migrated.list_simulation_bindings("project-1"), ())
        self.assertEqual(migrated.list_evidence_claims("project-1"), ())
        self.assertEqual(
            migrated.list_design_state_transitions("project-1", "session-1"), ()
        )
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 10
            )

    def test_schema_v7_is_migrated_to_v8_with_local_access_bootstrap(self) -> None:
        self.store.create_project(self.project)
        with sqlite3.connect(self.path) as connection:
            self._drop_access_control_schema(connection)
            connection.execute("PRAGMA user_version = 7")

        migrated = SQLiteEvidenceStore(self.path)

        self.assertEqual(migrated.get_project(self.project.project_id), self.project)
        self.assertEqual(
            migrated.get_organization(LOCAL_BOOTSTRAP_ORG_ID).name,
            "Local FORGE Workspace",
        )
        self.assertEqual(
            migrated.get_actor(LOCAL_BOOTSTRAP_ACTOR_ID).display_name,
            "Local Operator",
        )
        self.assertEqual(
            migrated.get_membership(
                LOCAL_BOOTSTRAP_ORG_ID, LOCAL_BOOTSTRAP_ACTOR_ID
            ).role,
            Role.ADMIN,
        )
        self.assertTrue(
            migrated.get_project_access(
                LOCAL_BOOTSTRAP_ORG_ID,
                self.project.project_id,
                LOCAL_BOOTSTRAP_ACTOR_ID,
            ).active
        )
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 10
            )

    def test_schema_v8_is_migrated_to_v9_with_empty_local_rag_ledgers(self) -> None:
        self.store.create_project(self.project)
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE cad_geometry_assets")
            connection.execute("DROP TABLE conversation_runtime_results")
            connection.execute("DROP TABLE knowledge_sources")
            connection.execute("DELETE FROM schema_migrations WHERE version = 10")
            connection.execute("DELETE FROM schema_migrations WHERE version = 9")
            connection.execute("PRAGMA user_version = 8")

        migrated = SQLiteEvidenceStore(self.path)

        self.assertEqual(migrated.list_knowledge_sources("project-1"), ())
        self.assertEqual(migrated.list_runtime_results("project-1"), ())
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 10
            )

    def test_schema_v9_is_migrated_to_v10_with_empty_cad_geometry_ledger(self) -> None:
        self.store.create_project(self.project)
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE cad_geometry_assets")
            connection.execute("DELETE FROM schema_migrations WHERE version = 10")
            connection.execute("PRAGMA user_version = 9")

        migrated = SQLiteEvidenceStore(self.path)

        self.assertEqual(migrated.list_cad_geometries("project-1"), ())
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 10
            )

    def test_access_control_records_and_audit_events_are_persisted(self) -> None:
        self.store.create_project(self.project)
        actor = self.store.get_actor(LOCAL_BOOTSTRAP_ACTOR_ID)
        membership = self.store.get_membership(
            LOCAL_BOOTSTRAP_ORG_ID, LOCAL_BOOTSTRAP_ACTOR_ID
        )
        project_access = self.store.get_project_access(
            LOCAL_BOOTSTRAP_ORG_ID, self.project.project_id, LOCAL_BOOTSTRAP_ACTOR_ID
        )
        request = AuthorizationRequest(
            org_id=LOCAL_BOOTSTRAP_ORG_ID,
            project_id=self.project.project_id,
            actor_id=LOCAL_BOOTSTRAP_ACTOR_ID,
            permission=Permission.DECIDE_RELEASE,
        )
        result = authorize(
            actor=actor,
            membership=membership,
            project_access=project_access,
            request=request,
        )
        event = build_audit_event(
            event_id="audit-1",
            request=request,
            result=result,
            occurred_at=self.base,
        )

        self.assertTrue(result.allowed)
        self.assertEqual(self.store.append_audit_event(event), event)
        self.assertEqual(self.store.get_audit_event("audit-1"), event)
        self.assertEqual(
            self.store.list_audit_events(self.project.project_id), (event,)
        )
        self.assertEqual(self.store.append_audit_event(event), event)
        self.assertEqual(len(self.store.list_audit_events(self.project.project_id)), 1)
        conflict = build_audit_event(
            event_id="audit-1",
            request=request.model_copy(update={"permission": Permission.READ_PROJECT}),
            result=result.model_copy(update={"permission": Permission.READ_PROJECT}),
            occurred_at=self.base,
        )
        with self.assertRaises(IntegrityConflictError):
            self.store.append_audit_event(conflict)
        export_payload = json.loads(self.store.export_project("project-1").content)
        export_paths = {entry["path"] for entry in export_payload["entries"]}
        self.assertIn("audit-events/audit-1.json", export_paths)

        inactive = Actor(
            actor_id=LOCAL_BOOTSTRAP_ACTOR_ID,
            display_name="Local Operator",
            active=False,
        )
        self.store.insert_actor(inactive)
        self.assertFalse(self.store.get_actor(LOCAL_BOOTSTRAP_ACTOR_ID).active)

    def test_conversational_evidence_claims_and_state_transitions_are_persisted(
        self,
    ) -> None:
        self.store.create_project(self.project)
        claim = build_evidence_claim(
            claim_id="claim-1",
            kind=EvidenceClaimKind.INFERRED,
            statement=(
                "Upper arm extension needs physical device evidence before release."
            ),
            evidence_refs=("design-candidate:dc-014",),
            confidence=ClaimConfidence.MEDIUM,
        )
        version = self.store.store_evidence_claim(
            StoredEvidenceClaim(
                project_id="project-1",
                session_id="session-1",
                claim_hash=claim.claim_hash,
                claim_id=claim.claim_id,
                claim=claim,
                stored_at=self.base + timedelta(minutes=1),
            ),
            expected_project_version=1,
        )
        transition = DesignStateTransition(
            session_id="session-1",
            sequence=1,
            from_state=DesignConversationState.IDEA,
            to_state=DesignConversationState.PROPOSED,
            occurred_at=self.base + timedelta(minutes=2),
        )
        version = self.store.append_design_state_transition(
            StoredDesignStateTransition(
                project_id="project-1",
                session_id=transition.session_id,
                sequence=transition.sequence,
                transition_hash=canonical_sha256(transition),
                from_state=transition.from_state,
                to_state=transition.to_state,
                candidate_hash=transition.candidate_hash,
                simulation_hash=transition.simulation_hash,
                transition=transition,
                stored_at=self.base + timedelta(minutes=3),
            ),
            expected_project_version=version,
        )

        self.assertEqual(version, 3)
        self.assertEqual(self.store.get_evidence_claim(claim.claim_hash).claim, claim)
        self.assertEqual(
            self.store.list_evidence_claims("project-1")[0].claim_hash,
            claim.claim_hash,
        )
        self.assertEqual(
            self.store.list_design_state_transitions("project-1", "session-1")[0],
            StoredDesignStateTransition(
                project_id="project-1",
                session_id=transition.session_id,
                sequence=transition.sequence,
                transition_hash=canonical_sha256(transition),
                from_state=transition.from_state,
                to_state=transition.to_state,
                candidate_hash=None,
                simulation_hash=None,
                transition=transition,
                stored_at=self.base + timedelta(minutes=3),
            ),
        )
        export_payload = json.loads(self.store.export_project("project-1").content)
        export_paths = {entry["path"] for entry in export_payload["entries"]}
        self.assertIn(f"evidence-claims/{claim.claim_hash}.json", export_paths)
        self.assertIn("design-state-transitions/session-1-1.json", export_paths)

    def test_migration_history_corruption_is_rejected(self) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute("DELETE FROM schema_migrations")
        with self.assertRaises(MigrationError):
            SQLiteEvidenceStore(self.path)

    def test_project_contract_create_get_delete_and_cascade(self) -> None:
        for unsafe in ("../escape", "a/b", "a\\b", "bad\nname", "x" * 129):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValidationError):
                self.project.model_copy(update={"project_id": unsafe}).model_validate(
                    self.project.model_dump() | {"project_id": unsafe}
                )
        self.store.create_project(self.project)
        self.assertEqual(self.store.get_project("project-1"), self.project)
        with self.assertRaises(IntegrityConflictError):
            self.store.create_project(self.project)
        with self.assertRaises(RecordNotFoundError):
            self.store.get_project("missing")

        version = self.store.store_spec(self._stored_spec(), expected_project_version=1)
        with self.assertRaises(VersionConflictError):
            self.store.delete_project("project-1", expected_project_version=1)
        self.store.delete_project("project-1", expected_project_version=version)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM specs").fetchone()[0], 0
            )

    def test_spec_round_trip_cas_contiguous_versions_and_corruption(self) -> None:
        self.store.create_project(self.project)
        with self.assertRaises(VersionConflictError):
            self.store.store_spec(self._stored_spec(), expected_project_version=9)
        self.assertEqual(self.store.get_project("project-1").version, 1)
        version = self.store.store_spec(self._stored_spec(), expected_project_version=1)
        self.assertEqual(version, 2)
        self.assertEqual(
            self.store.get_spec("project-1", "spec-1", 1),
            self._stored_spec(),
        )

        skipped = self.spec.model_copy(update={"spec_version": 3})
        with self.assertRaises(IntegrityConflictError):
            self.store.store_spec(
                self._stored_spec(skipped), expected_project_version=version
            )
        self.assertEqual(self.store.get_project("project-1").version, version)

        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE specs SET payload_json = '{bad json'")
        with self.assertRaises(CorruptRecordError):
            self.store.get_spec("project-1", "spec-1", 1)

    def test_immutable_spec_approval_and_lifecycle_round_trip(self) -> None:
        self.store.create_project(self.project)
        draft = self.spec.model_copy(
            update={"status": SpecStatus.DRAFT, "approved_at": None}
        )
        stored_draft = self._stored_spec(draft)
        version = self.store.store_spec(stored_draft, expected_project_version=1)
        draft_event = SpecStateEvent(
            event_id="spec-1-v1-draft",
            project_id="project-1",
            spec_id="spec-1",
            spec_version=1,
            sequence=1,
            previous_status=None,
            status=SpecStatus.DRAFT,
            actor="test",
            reason_code="created",
            occurred_at=self.base,
        )
        version = self.store.append_spec_event(
            draft_event, expected_project_version=version
        )
        approved = self._stored_spec()
        version = self.store.store_approved_spec(
            approved, expected_project_version=version
        )
        approved_event = SpecStateEvent(
            event_id="spec-1-v1-approved",
            project_id="project-1",
            spec_id="spec-1",
            spec_version=1,
            sequence=2,
            previous_status=SpecStatus.DRAFT,
            status=SpecStatus.APPROVED,
            actor="test",
            reason_code="approved",
            occurred_at=self.base,
        )
        version = self.store.append_spec_event(
            approved_event, expected_project_version=version
        )
        version = self.store.store_approval(
            self.spec_approval, expected_project_version=version
        )
        version = self.store.store_preparation(
            StoredPreparation(
                project_id="project-1",
                preparation=self.prepared,
                stored_at=self.base,
            ),
            expected_project_version=version,
        )

        self.assertEqual(
            self.store.get_draft_spec("project-1", "spec-1", 1), stored_draft
        )
        self.assertEqual(
            self.store.get_approved_spec("project-1", "spec-1", 1), approved
        )
        self.assertEqual(self.store.get_spec("project-1", "spec-1", 1), approved)
        self.assertEqual(
            self.store.list_spec_events("project-1", "spec-1", 1),
            (draft_event, approved_event),
        )
        self.assertEqual(
            self.store.get_preparation(self.prepared.preparation_id).preparation,
            self.prepared,
        )

        next_draft = draft.model_copy(update={"spec_version": 2, "intent": "v2"})
        version = self.store.store_spec(
            StoredSpec(
                project_id="project-1",
                spec_id="spec-1",
                spec_version=2,
                spec_hash=canonical_sha256(next_draft),
                spec=next_draft,
                stored_at=self.base + timedelta(seconds=2),
            ),
            expected_project_version=version,
        )
        superseded = SpecStateEvent(
            event_id="spec-1-v1-superseded",
            project_id="project-1",
            spec_id="spec-1",
            spec_version=1,
            sequence=3,
            previous_status=SpecStatus.APPROVED,
            status=SpecStatus.SUPERSEDED,
            actor="test",
            reason_code="new_revision",
            occurred_at=self.base + timedelta(seconds=2),
        )
        self.store.append_spec_event(superseded, expected_project_version=version)
        self.assertEqual(
            self.store.list_spec_events("project-1", "spec-1", 1)[-1], superseded
        )
        self.assertEqual(
            self.store.get_approved_spec("project-1", "spec-1", 1), approved
        )

        export = self.store.export_project("project-1").content
        self.assertIn(b"specs/spec-1-v1-approved.json", export)
        self.assertIn(b"spec-events/spec-1-v1-3.json", export)

        with sqlite3.connect(self.path) as connection:
            payload = json.loads(
                connection.execute(
                    "SELECT payload_json FROM specs WHERE spec_version = 1"
                ).fetchone()[0]
            )
            payload["spec"]["intent"] = "tampered"
            connection.execute(
                "UPDATE specs SET payload_json = ? WHERE spec_version = 1",
                (json.dumps(payload),),
            )
        with self.assertRaises(CorruptRecordError):
            self.store.get_approved_spec("project-1", "spec-1", 1)

    def test_revision_approval_and_preparation_are_bound_and_immutable(self) -> None:
        self.store.create_project(self.project)
        version = self.store.store_spec(self._stored_spec(), expected_project_version=1)
        revision = make_revision()
        stored_revision = StoredRevision(
            project_id=revision.project_id,
            revision_id=revision.revision_id,
            revision_number=revision.revision_number,
            revision_hash=canonical_sha256(revision),
            revision=revision,
            stored_at=self.base,
        )
        version = self.store.store_revision(
            stored_revision, expected_project_version=version
        )
        self.assertEqual(
            self.store.get_revision("project-1", "revision-1"), stored_revision
        )
        approvals = approval_for(self.spec, revision=revision)
        version = self.store.store_approval(
            approvals[0], expected_project_version=version
        )
        version = self.store.store_approval(
            approvals[1], expected_project_version=version
        )
        self.assertEqual(
            self.store.get_approval(approvals[1].approval_id), approvals[1]
        )
        with self.assertRaises(IntegrityConflictError):
            self.store.store_approval(approvals[1], expected_project_version=version)

        prepared = make_prepared(
            self.spec,
            approvals,
            revision=revision,
            preparation_id="revision-preparation",
        )
        version = self.store.store_preparation(
            StoredPreparation(
                project_id="project-1",
                preparation=prepared,
                stored_at=self.base,
            ),
            expected_project_version=version,
        )
        self.assertEqual(
            self.store.get_preparation("revision-preparation").preparation,
            prepared,
        )
        self.assertEqual(self.store.get_project("project-1").version, version)

        stale = approvals[0].model_copy(
            update={"approval_id": "stale", "subject_hash": "sha256:" + "0" * 64}
        )
        with self.assertRaises(IntegrityConflictError):
            self.store.store_approval(stale, expected_project_version=version)

    def test_missing_prepared_approval_rolls_back_without_version_bump(self) -> None:
        self.store.create_project(self.project)
        version = self.store.store_spec(self._stored_spec(), expected_project_version=1)
        record = StoredPreparation(
            project_id="project-1",
            preparation=self.prepared,
            stored_at=self.base,
        )
        with self.assertRaises(RecordNotFoundError):
            self.store.store_preparation(record, expected_project_version=version)
        self.assertEqual(self.store.get_project("project-1").version, version)

    def test_interrupted_run_atomic_completion_and_stable_export(self) -> None:
        version = self._store_running()
        interrupted = self.store.list_interrupted_runs()
        self.assertEqual(len(interrupted), 1)
        self.assertEqual(interrupted[0].run_id, "run-1")
        self.assertEqual(interrupted[0].project_version, version)

        run_result, verification = successful_run("run-1", self.prepared)
        terminal = self._event(
            4,
            RunLifecycleStatus.RUNNING,
            RunLifecycleStatus.SUCCEEDED,
            result=run_result,
        )
        with self.assertRaises(IntegrityConflictError):
            self.store.append_run_event(terminal, expected_project_version=version)
        version = self.store.complete_run(
            terminal,
            run_result,
            verification,
            expected_project_version=version,
        )
        run = self.store.get_run("run-1")
        self.assertEqual(run.result, run_result)
        self.assertEqual(run.verification, verification)
        self.assertEqual(self.store.list_interrupted_runs(), ())

        first = self.store.export_project("project-1")
        second = SQLiteEvidenceStore(self.path).export_project("project-1")
        self.assertEqual(first, second)
        self.assertEqual(
            first.filename,
            f"project-project-1-v{version}.forge.json",
        )
        bundle = json.loads(first.content)
        self.assertEqual(bundle["project_version"], version)
        paths = [entry["path"] for entry in bundle["entries"]]
        self.assertEqual(paths, sorted(paths))
        for entry, manifest in zip(bundle["entries"], bundle["manifest"], strict=True):
            payload = json.dumps(
                entry["payload"],
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            self.assertEqual(entry["size"], len(payload))
            self.assertEqual(
                entry["sha256"],
                "sha256:" + hashlib.sha256(payload).hexdigest(),
            )
            self.assertEqual(
                manifest,
                {
                    "path": entry["path"],
                    "size": entry["size"],
                    "sha256": entry["sha256"],
                },
            )

    def test_bad_terminal_result_rolls_back_event_and_version(self) -> None:
        version = self._store_running()
        run_result, verification = successful_run("run-1", self.prepared)
        bad_event = self._event(
            4,
            RunLifecycleStatus.RUNNING,
            RunLifecycleStatus.SUCCEEDED,
            result=run_result,
        ).model_copy(update={"run_record_hash": "sha256:" + "0" * 64})
        with self.assertRaises(IntegrityConflictError):
            self.store.complete_run(
                bad_event,
                run_result,
                verification,
                expected_project_version=version,
            )
        self.assertEqual(len(self.store.get_run("run-1").events), 3)
        self.assertEqual(self.store.get_project("project-1").version, version)

    def test_cancelled_run_is_not_interrupted_and_has_no_result(self) -> None:
        version = self._store_running()
        version = self.store.append_run_event(
            self._event(
                4,
                RunLifecycleStatus.RUNNING,
                RunLifecycleStatus.CANCELLED,
            ),
            expected_project_version=version,
        )
        self.assertEqual(self.store.list_interrupted_runs(), ())
        self.assertIsNone(self.store.get_run("run-1").result)
        self.assertEqual(self.store.get_project("project-1").version, version)

    def test_idempotency_replay_and_conflict(self) -> None:
        self.store.create_project(self.project)
        record = IdempotencyRecord(
            operation="create-spec",
            project_id="project-1",
            local_installation_id="install-1",
            key="key-1",
            request_hash="sha256:" + "1" * 64,
            response_json='{"ok":true}',
            created_at=self.base,
        )
        self.assertEqual(self.store.put_idempotency(record), record.response_json)
        changed_response = record.model_copy(update={"response_json": '{"ok":false}'})
        self.assertEqual(
            self.store.put_idempotency(changed_response), record.response_json
        )
        with self.assertRaises(IdempotencyConflictError):
            self.store.put_idempotency(
                record.model_copy(update={"request_hash": "sha256:" + "2" * 64})
            )
        with self.assertRaises(ValidationError):
            IdempotencyRecord(
                operation="x",
                project_id="project-1",
                local_installation_id="i",
                key="k",
                request_hash="sha256:" + "1" * 64,
                response_json="not-json",
                created_at=self.base,
            )

    def test_atomic_idempotent_compound_write_replay_conflict_and_rollback(
        self,
    ) -> None:
        self.store.create_project(self.project)
        record = IdempotencyRecord(
            operation="approve-spec",
            project_id="project-1",
            local_installation_id="install-1",
            key="compound-1",
            request_hash="sha256:" + "3" * 64,
            response_json='{"version":2}',
            created_at=self.base,
        )

        def write_spec_and_approval(atomic: AtomicProjectWrite) -> None:
            atomic.insert_spec(self._stored_spec())
            atomic.insert_approval(self.spec_approval)

        result = self.store.transact_idempotently(
            record,
            expected_project_version=1,
            updated_at=self.base,
            apply=write_spec_and_approval,
        )
        self.assertFalse(result.replayed)
        self.assertEqual(result.project_version, 2)
        self.assertEqual(result.response_json, record.response_json)
        self.assertEqual(self.store.get_project("project-1").version, 2)
        self.assertEqual(
            self.store.get_spec("project-1", "spec-1", 1), self._stored_spec()
        )
        self.assertEqual(
            self.store.get_approval(self.spec_approval.approval_id),
            self.spec_approval,
        )

        callback_called = False

        def must_not_run(_atomic: AtomicProjectWrite) -> None:
            nonlocal callback_called
            callback_called = True

        replay = self.store.transact_idempotently(
            record.model_copy(update={"response_json": '{"ignored":true}'}),
            expected_project_version=1,
            updated_at=self.base,
            apply=must_not_run,
        )
        self.assertTrue(replay.replayed)
        self.assertFalse(callback_called)
        self.assertEqual(replay.response_json, record.response_json)
        self.assertEqual(replay.project_version, 2)
        with self.assertRaises(IdempotencyConflictError):
            self.store.transact_idempotently(
                record.model_copy(update={"request_hash": "sha256:" + "4" * 64}),
                expected_project_version=2,
                updated_at=self.base,
                apply=must_not_run,
            )

        rollback_record = record.model_copy(
            update={"key": "compound-rollback", "response_json": '{"ok":false}'}
        )
        revision = make_revision()
        stored_revision = StoredRevision(
            project_id=revision.project_id,
            revision_id=revision.revision_id,
            revision_number=revision.revision_number,
            revision_hash=canonical_sha256(revision),
            revision=revision,
            stored_at=self.base,
        )

        def fail_after_insert(atomic: AtomicProjectWrite) -> None:
            atomic.insert_revision(stored_revision)
            raise RuntimeError("injected failure")

        with self.assertRaisesRegex(RuntimeError, "injected failure"):
            self.store.transact_idempotently(
                rollback_record,
                expected_project_version=2,
                updated_at=self.base,
                apply=fail_after_insert,
            )
        self.assertEqual(self.store.get_project("project-1").version, 2)
        with self.assertRaises(RecordNotFoundError):
            self.store.get_revision("project-1", revision.revision_id)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM idempotency_records WHERE key = ?",
                    (rollback_record.key,),
                ).fetchone()[0],
                0,
            )

    def test_atomic_idempotent_create_and_delete_remain_replayable(self) -> None:
        project = self.project.model_copy(
            update={"project_id": "project-atomic", "name": "Atomic"}
        )
        create_record = IdempotencyRecord(
            operation="create-project",
            project_id=project.project_id,
            local_installation_id="install-1",
            key="create-1",
            request_hash="sha256:" + "5" * 64,
            response_json='{"created":true}',
            created_at=self.base,
        )
        created = self.store.transact_idempotently(
            create_record,
            expected_project_version=None,
            updated_at=self.base,
            apply=lambda atomic: atomic.create_project(project),
        )
        self.assertEqual(created.project_version, 1)
        self.assertFalse(created.replayed)
        replayed_create = self.store.transact_idempotently(
            create_record,
            expected_project_version=None,
            updated_at=self.base,
            apply=lambda _atomic: self.fail("create replay invoked callback"),
        )
        self.assertTrue(replayed_create.replayed)
        self.assertEqual(replayed_create.project_version, 1)

        delete_record = create_record.model_copy(
            update={
                "operation": "delete-project",
                "key": "delete-1",
                "request_hash": "sha256:" + "6" * 64,
                "response_json": '{"deleted":true}',
            }
        )
        deleted = self.store.transact_idempotently(
            delete_record,
            expected_project_version=1,
            updated_at=self.base,
            apply=lambda atomic: atomic.delete_project(),
        )
        self.assertIsNone(deleted.project_version)
        with self.assertRaises(RecordNotFoundError):
            self.store.get_project(project.project_id)
        replayed_delete = self.store.transact_idempotently(
            delete_record,
            expected_project_version=1,
            updated_at=self.base,
            apply=lambda _atomic: self.fail("delete replay invoked callback"),
        )
        self.assertTrue(replayed_delete.replayed)
        self.assertIsNone(replayed_delete.project_version)

    def test_row_payload_drift_and_corrupt_idempotency_are_rejected(self) -> None:
        self.store.create_project(self.project)
        other = self.project.model_copy(
            update={"project_id": "project-2", "name": "Other"}
        )
        self.store.create_project(other)
        self.store.store_spec(self._stored_spec(), expected_project_version=1)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE specs SET project_id = ? WHERE project_id = ?",
                (other.project_id, self.project.project_id),
            )
        with self.assertRaises(CorruptRecordError):
            self.store.get_spec(other.project_id, "spec-1", 1)
        with self.assertRaises(CorruptRecordError):
            self.store.export_project(other.project_id)

        record = IdempotencyRecord(
            operation="corrupt-check",
            project_id="project-1",
            local_installation_id="install-1",
            key="corrupt-1",
            request_hash="sha256:" + "7" * 64,
            response_json='{"ok":true}',
            created_at=self.base,
        )
        self.store.put_idempotency(record)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE idempotency_records SET response_json = 'not-json' "
                "WHERE key = ?",
                (record.key,),
            )
        with self.assertRaises(CorruptRecordError):
            self.store.put_idempotency(record)

    def test_interrupted_run_rejects_scalar_event_corruption(self) -> None:
        self._store_running()
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE run_events SET prepare_hash = ? WHERE sequence = 3",
                ("sha256:" + "0" * 64,),
            )
        with self.assertRaises(CorruptRecordError):
            self.store.list_interrupted_runs()
        with self.assertRaises(CorruptRecordError):
            self.store.get_run("run-1")

    def test_interrupted_run_does_not_hide_tampered_scalar_status(self) -> None:
        self._store_running()
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE run_events SET status = 'failed' WHERE sequence = 3"
            )
        with self.assertRaises(CorruptRecordError):
            self.store.list_interrupted_runs()

    def test_evidence_round_trip_duplicate_and_export(self) -> None:
        self.store.create_project(self.project)
        revision = make_revision()
        version = self.store.store_revision(
            StoredRevision(
                project_id="project-1",
                revision_id=revision.revision_id,
                revision_number=revision.revision_number,
                revision_hash=canonical_sha256(revision),
                revision=revision,
                stored_at=self.base,
            ),
            expected_project_version=1,
        )
        evidence = EvidenceRecord(
            evidence_id="evidence-1",
            evidence_class=EvidenceClass.ANALYSIS,
            produced_for_revision_id=revision.revision_id,
            dependency_hash=dependency_hash(revision, EvidenceClass.ANALYSIS),
            verdict=Verdict.PASS,
            evidence_refs=("run:run-1",),
            recorded_at=self.base,
        )
        forged = evidence.model_copy(
            update={
                "evidence_id": "evidence-forged-dependency",
                "dependency_hash": "sha256:" + "0" * 64,
            }
        )
        with self.assertRaises(IntegrityConflictError):
            self.store.store_evidence(
                "project-1", forged, expected_project_version=version
            )
        version = self.store.store_evidence(
            "project-1", evidence, expected_project_version=version
        )
        self.assertEqual(self.store.list_evidence("project-1"), (evidence,))
        with self.assertRaises(IntegrityConflictError):
            self.store.store_evidence(
                "project-1", evidence, expected_project_version=version
            )
        self.assertIn(
            b"evidence/evidence-1.json", self.store.export_project("project-1").content
        )

    def test_cost_provenance_round_trip_reproduces_and_exports(self) -> None:
        self.store.create_project(self.project)
        revision = make_revision()
        version = self.store.store_revision(
            StoredRevision(
                project_id="project-1",
                revision_id=revision.revision_id,
                revision_number=revision.revision_number,
                revision_hash=canonical_sha256(revision),
                revision=revision,
                stored_at=self.base,
            ),
            expected_project_version=1,
        )
        evaluated_at = self.base + timedelta(days=1)
        bom = (BOMLine(part_number="FAN-120", quantity=2),)
        quotes = (
            QuoteSnapshot(
                quote_id="quote-1",
                part_number="FAN-120",
                supplier="supplier",
                region="KR",
                currency="USD",
                unit_price=Decimal("10.00"),
                minimum_quantity=1,
                observed_at=self.base,
                expires_at=self.base + timedelta(days=30),
                shipping_included=False,
                shipping_cost=Decimal("5.00"),
                tax_included=False,
                tax_cost=Decimal("2.00"),
                source_url="https://supplier.example/FAN-120",
                source_hash="sha256:" + "a" * 64,
            ),
        )
        evaluation = evaluate_cost(
            bom,
            quotes,
            currency="USD",
            budget_limit=Decimal("30.00"),
            reserve_rate=Decimal("0.10"),
            evaluated_at=evaluated_at,
        )
        evidence = EvidenceRecord(
            evidence_id="cost-1",
            evidence_class=EvidenceClass.COST,
            produced_for_revision_id=revision.revision_id,
            dependency_hash=dependency_hash(revision, EvidenceClass.COST),
            verdict=evaluation.verdict,
            evidence_refs=("quote:quote-1", "source:sha256:" + "a" * 64),
            recorded_at=evaluated_at,
        )
        version = self.store.store_evidence(
            "project-1", evidence, expected_project_version=version
        )
        stored = StoredCostEvaluation(
            project_id="project-1",
            evidence_id=evidence.evidence_id,
            revision_id=revision.revision_id,
            bom_artifact_hash=revision.artifact_map()[ArtifactKind.BOM].content_hash,
            dependency_hash=evidence.dependency_hash,
            bom=bom,
            quotes=quotes,
            evaluated_at=evaluated_at,
            evaluation=evaluation,
        )
        with self.assertRaises(IntegrityConflictError):
            self.store.store_cost_evaluation(
                stored.model_copy(update={"bom_artifact_hash": "sha256:" + "0" * 64}),
                expected_project_version=version,
            )
        self.store.store_cost_evaluation(stored, expected_project_version=version)
        self.assertEqual(self.store.get_cost_evaluation("cost-1"), stored)
        self.assertEqual(self.store.list_cost_evaluations("project-1"), (stored,))
        export = self.store.export_project("project-1").content
        self.assertIn(b"cost/cost-1.json", export)
        self.assertIn(b"https://supplier.example/FAN-120", export)

    def test_bounded_busy_retry_then_recovery(self) -> None:
        delays: list[float] = []
        store = SQLiteEvidenceStore(self.path, sleeper=delays.append)
        lock = sqlite3.connect(self.path, isolation_level=None)
        lock.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(StorageBusyError):
                store.create_project(self.project)
        finally:
            lock.rollback()
            lock.close()
        self.assertEqual(delays, [0.025, 0.05, 0.1])
        store.create_project(self.project)
        self.assertEqual(store.get_project("project-1"), self.project)

    def test_busy_idempotent_write_has_no_partial_commit_then_succeeds_once(
        self,
    ) -> None:
        delays: list[float] = []
        store = SQLiteEvidenceStore(self.path, sleeper=delays.append)
        project = self.project.model_copy(update={"project_id": "project-busy"})
        record = IdempotencyRecord(
            operation="create-project",
            project_id=project.project_id,
            local_installation_id="install-1",
            key="busy-create",
            request_hash="sha256:" + "8" * 64,
            response_json='{"created":true}',
            created_at=self.base,
        )
        lock = sqlite3.connect(self.path, isolation_level=None)
        lock.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(StorageBusyError):
                store.transact_idempotently(
                    record,
                    expected_project_version=None,
                    updated_at=self.base,
                    apply=lambda atomic: atomic.create_project(project),
                )
            with sqlite3.connect(self.path) as reader:
                self.assertEqual(
                    reader.execute(
                        "SELECT COUNT(*) FROM projects WHERE project_id = ?",
                        (project.project_id,),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    reader.execute(
                        "SELECT COUNT(*) FROM idempotency_records WHERE key = ?",
                        (record.key,),
                    ).fetchone()[0],
                    0,
                )
        finally:
            lock.rollback()
            lock.close()
        self.assertEqual(delays, [0.025, 0.05, 0.1])
        completed = store.transact_idempotently(
            record,
            expected_project_version=None,
            updated_at=self.base,
            apply=lambda atomic: atomic.create_project(project),
        )
        self.assertFalse(completed.replayed)
        replayed = store.transact_idempotently(
            record,
            expected_project_version=None,
            updated_at=self.base,
            apply=lambda _atomic: self.fail("busy replay invoked callback"),
        )
        self.assertTrue(replayed.replayed)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM projects WHERE project_id = ?",
                    (project.project_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM idempotency_records WHERE key = ?",
                    (record.key,),
                ).fetchone()[0],
                1,
            )

    def test_stored_run_rejects_mixed_hash_status_and_verdict(self) -> None:
        version = self._store_running()
        running = self.store.get_run("run-1")
        mixed = running.events[1].model_copy(
            update={"prepare_hash": "sha256:" + "0" * 64}
        )
        with self.assertRaises(ValidationError):
            StoredRun(
                project_id="project-1",
                run_id="run-1",
                preparation_id=self.prepared.preparation_id,
                events=(running.events[0], mixed, running.events[2]),
            )
        self.assertEqual(self.store.get_project("project-1").version, version)


if __name__ == "__main__":
    unittest.main()
