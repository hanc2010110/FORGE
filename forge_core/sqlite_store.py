from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn, TypeVar

from pydantic import ValidationError

from forge_core.access_control import (
    Actor,
    AuditEvent,
    Membership,
    Organization,
    ProjectAccess,
    Role,
)
from forge_core.cad_geometry import StoredCADGeometryAsset
from forge_core.change_management import ArtifactDomain
from forge_core.conversational_persistence import (
    StoredDesignCandidate,
    StoredDesignStateTransition,
    StoredEvidenceClaim,
    StoredSimulationBinding,
)
from forge_core.design import (
    ArtifactKind,
    EvidenceClass,
    EvidenceRecord,
    dependency_hash,
)
from forge_core.hashing import canonical_sha256
from forge_core.local_rag import StoredKnowledgeSource, StoredRuntimeResult
from forge_core.models import (
    AnalysisRunRecord,
    ApprovalRef,
    ApprovalSubjectKind,
    ContractModel,
    RunLifecycleStatus,
    RunStateEvent,
    SpecStatus,
    VerificationBundle,
)
from forge_core.persistence import (
    CorruptRecordError,
    EvidenceExport,
    IdempotencyConflictError,
    IdempotencyRecord,
    IdempotentWriteResult,
    IntegrityConflictError,
    InterruptedRun,
    MigrationError,
    PersistenceError,
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
from forge_core.planning_persistence import (
    StoredChangeImpactPreview,
    StoredExternalEvidencePlan,
    StoredExternalEvidencePlanVerification,
    StoredPlanVerification,
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
    FirmwareBuildEvidence,
    TestExecutionEvidence,
    cost_evidence_matches_policy,
)
from forge_core.resolution_persistence import (
    StoredDesignProposalSet,
    StoredReleaseDiagnosis,
    StoredResolutionPlan,
)

ModelT = TypeVar("ModelT", bound=ContractModel)
ResultT = TypeVar("ResultT")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
LOCAL_BOOTSTRAP_ORG_ID = "local-org"
LOCAL_BOOTSTRAP_ACTOR_ID = "local-operator"


def _utc_text(value: datetime) -> str:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise IntegrityConflictError("stored timestamps must be timezone-aware UTC")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_model(value: ContractModel) -> str:
    return json.dumps(
        value.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _content_hash(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _safe_id(value: str, field_name: str) -> str:
    if _SAFE_ID.fullmatch(value) is None:
        raise IntegrityConflictError(f"{field_name} is not a safe identifier")
    return value


class AtomicProjectWrite:
    """High-level domain inserts sharing one repository transaction."""

    def __init__(
        self,
        store: SQLiteEvidenceStore,
        connection: sqlite3.Connection,
        project_id: str,
    ) -> None:
        self._store = store
        self._connection = connection
        self.project_id = project_id
        self.created = False
        self.deleted = False

    def create_project(self, project: ProjectRecord) -> None:
        self._require_active()
        if self.created:
            raise IntegrityConflictError("atomic write already created its project")
        self._require_project(project.project_id)
        self._store._insert_project(self._connection, project)
        self._store._bootstrap_local_access_for_project(self._connection, project)
        self.created = True

    def delete_project(self) -> None:
        self._require_active()
        cursor = self._connection.execute(
            "DELETE FROM projects WHERE project_id = ?", (self.project_id,)
        )
        if cursor.rowcount != 1:
            raise RecordNotFoundError("project not found")
        self.deleted = True

    def insert_spec(self, record: StoredSpec) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._insert_spec(self._connection, record)

    def insert_approved_spec(self, record: StoredSpec) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._insert_approved_spec(self._connection, record)

    def append_spec_event(self, event: SpecStateEvent) -> None:
        self._require_active()
        self._require_project(event.project_id)
        self._store._append_spec_event(self._connection, event)

    def insert_revision(self, record: StoredRevision) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._insert_revision(self._connection, record)

    def insert_approval(self, approval: ApprovalRef) -> None:
        self._require_active()
        self._require_project(approval.project_id)
        self._store._insert_approval(self._connection, approval)

    def insert_preparation(self, record: StoredPreparation) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._insert_preparation(self._connection, record)

    def append_run_event(self, event: RunStateEvent) -> None:
        self._require_active()
        self._require_project(event.project_id)
        if event.status in {
            RunLifecycleStatus.SUCCEEDED,
            RunLifecycleStatus.FAILED,
        }:
            raise IntegrityConflictError("terminal result events require complete_run")
        self._store._append_event(self._connection, event)

    def complete_run(
        self,
        event: RunStateEvent,
        result: AnalysisRunRecord,
        verification: VerificationBundle | None,
    ) -> None:
        self._require_active()
        self._require_project(event.project_id)
        self._store._complete_run(self._connection, event, result, verification)

    def insert_evidence(self, evidence: EvidenceRecord) -> None:
        self._require_active()
        self._store._insert_evidence(self._connection, self.project_id, evidence)

    def insert_cost_evaluation(self, record: StoredCostEvaluation) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._insert_cost_evaluation(self._connection, record)

    def insert_connector_snapshot(self, record: StoredConnectorSnapshot) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._insert_connector_snapshot(self._connection, record)

    def insert_release_policy(self, record: StoredReleasePolicy) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._insert_release_policy(self._connection, record)

    def insert_change_assessment(self, record: StoredChangeImpactAssessment) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._insert_change_assessment(self._connection, record)

    def insert_release_evidence(self, record: StoredRawReleaseEvidence) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._insert_release_evidence(self._connection, record)

    def append_release_decision(self, record: StoredReleaseDecision) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._append_release_decision(self._connection, record)

    def insert_change_preview(self, record: StoredChangeImpactPreview) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._insert_change_preview(self._connection, record)

    def insert_plan_verification(self, record: StoredPlanVerification) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._insert_plan_verification(self._connection, record)

    def insert_external_evidence_plan(self, record: StoredExternalEvidencePlan) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._insert_external_evidence_plan(self._connection, record)

    def insert_external_evidence_plan_verification(
        self, record: StoredExternalEvidencePlanVerification
    ) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._insert_external_evidence_plan_verification(
            self._connection, record
        )

    def insert_design_proposal(self, record: StoredDesignProposalSet) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._insert_design_proposal(self._connection, record)

    def insert_release_diagnosis(self, record: StoredReleaseDiagnosis) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._insert_release_diagnosis(self._connection, record)

    def insert_resolution_plan(self, record: StoredResolutionPlan) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._insert_resolution_plan(self._connection, record)

    def insert_design_candidate(self, record: StoredDesignCandidate) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._insert_design_candidate(self._connection, record)

    def insert_simulation_binding(self, record: StoredSimulationBinding) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._insert_simulation_binding(self._connection, record)

    def insert_evidence_claim(self, record: StoredEvidenceClaim) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._insert_evidence_claim(self._connection, record)

    def append_design_state_transition(
        self, record: StoredDesignStateTransition
    ) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._append_design_state_transition(self._connection, record)

    def insert_organization(self, record: Organization) -> None:
        self._require_active()
        self._store._put_organization(self._connection, record)

    def insert_actor(self, record: Actor) -> None:
        self._require_active()
        self._store._put_actor(self._connection, record)

    def insert_membership(self, record: Membership) -> None:
        self._require_active()
        self._store._put_membership(self._connection, record)

    def insert_project_access(self, record: ProjectAccess) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._put_project_access(self._connection, record)

    def append_audit_event(self, record: AuditEvent) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._append_audit_event(self._connection, record)

    def insert_knowledge_source(self, record: StoredKnowledgeSource) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._insert_knowledge_source(self._connection, record)

    def insert_runtime_result(self, record: StoredRuntimeResult) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._insert_runtime_result(self._connection, record)

    def insert_cad_geometry(self, record: StoredCADGeometryAsset) -> None:
        self._require_active()
        self._require_project(record.project_id)
        self._store._insert_cad_geometry(self._connection, record)

    def _require_active(self) -> None:
        if self.deleted:
            raise IntegrityConflictError("atomic write cannot mutate a deleted project")

    def _require_project(self, project_id: str) -> None:
        if project_id != self.project_id:
            raise IntegrityConflictError(
                "atomic write records must belong to one project"
            )


class SQLiteEvidenceStore:
    """Short-transaction SQLite repository for immutable FORGE evidence."""

    SCHEMA_VERSION = 10
    BUSY_TIMEOUT_MS = 100
    RETRY_DELAYS = (0.025, 0.05, 0.1)

    def __init__(
        self,
        path: str | Path,
        *,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.path = Path(path)
        self._sleeper = sleeper
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def __enter__(self) -> SQLiteEvidenceStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        """Connections are operation-scoped; no persistent handle is retained."""

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self.path,
                isolation_level=None,
                timeout=self.BUSY_TIMEOUT_MS / 1000,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {self.BUSY_TIMEOUT_MS}")
            return connection
        except sqlite3.OperationalError as exc:
            self._raise_operational(exc)

    @staticmethod
    def _raise_operational(exc: sqlite3.OperationalError) -> NoReturn:
        message = str(exc).lower()
        if "locked" in message or "busy" in message:
            raise StorageBusyError("SQLite storage is busy") from exc
        raise PersistenceError("SQLite operation failed") from exc

    @contextmanager
    def _reading(self) -> Any:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            yield connection
            connection.commit()
        except sqlite3.OperationalError as exc:
            connection.rollback()
            self._raise_operational(exc)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _write(self, operation: Callable[[sqlite3.Connection], ResultT]) -> ResultT:
        for attempt in range(len(self.RETRY_DELAYS) + 1):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                result = operation(connection)
                connection.commit()
                return result
            except sqlite3.OperationalError as exc:
                connection.rollback()
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    self._raise_operational(exc)
                if attempt == len(self.RETRY_DELAYS):
                    raise StorageBusyError("SQLite storage is busy") from exc
                self._sleeper(self.RETRY_DELAYS[attempt])
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        raise AssertionError("unreachable SQLite retry state")

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > self.SCHEMA_VERSION:
                raise MigrationError(
                    f"database schema {version} is newer than supported schema "
                    f"{self.SCHEMA_VERSION}"
                )
            connection.execute("PRAGMA journal_mode = WAL")
            if version == 0:
                self._create_schema(connection)
                return
            row = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if row is None or int(row["version"]) != version:
                raise MigrationError("migration history does not match user_version")
            if version < self.SCHEMA_VERSION:
                self._migrate(connection, version)
        except sqlite3.OperationalError as exc:
            self._raise_operational(exc)
        finally:
            connection.close()

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        try:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE schema_migrations(
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE projects(
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK(version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE specs(
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    spec_id TEXT NOT NULL,
                    spec_version INTEGER NOT NULL CHECK(spec_version >= 1),
                    spec_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, spec_id, spec_version)
                );
                CREATE TABLE approved_specs(
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    spec_id TEXT NOT NULL,
                    spec_version INTEGER NOT NULL CHECK(spec_version >= 1),
                    spec_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, spec_id, spec_version),
                    FOREIGN KEY(project_id, spec_id, spec_version)
                        REFERENCES specs(project_id, spec_id, spec_version)
                        ON DELETE CASCADE
                );
                CREATE TABLE spec_state_events(
                    event_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    spec_id TEXT NOT NULL,
                    spec_version INTEGER NOT NULL CHECK(spec_version >= 1),
                    sequence INTEGER NOT NULL CHECK(sequence BETWEEN 1 AND 3),
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    UNIQUE(project_id, spec_id, spec_version, sequence),
                    FOREIGN KEY(project_id, spec_id, spec_version)
                        REFERENCES specs(project_id, spec_id, spec_version)
                        ON DELETE CASCADE
                );
                CREATE TABLE revisions(
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    revision_id TEXT NOT NULL,
                    revision_number INTEGER NOT NULL CHECK(revision_number >= 1),
                    revision_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, revision_id),
                    UNIQUE(project_id, revision_number)
                );
                CREATE TABLE approvals(
                    approval_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    subject_kind TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    subject_version INTEGER NOT NULL,
                    subject_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(project_id, subject_kind, subject_id, subject_version)
                );
                CREATE TABLE preparations(
                    preparation_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    prepare_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL
                );
                CREATE TABLE run_events(
                    event_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    run_id TEXT NOT NULL,
                    preparation_id TEXT NOT NULL REFERENCES preparations(preparation_id)
                        ON DELETE CASCADE,
                    prepare_hash TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK(sequence >= 1),
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    UNIQUE(run_id, sequence)
                );
                CREATE INDEX run_events_latest
                    ON run_events(run_id, sequence DESC);
                CREATE TABLE run_results(
                    run_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    result_json TEXT NOT NULL,
                    verification_json TEXT,
                    stored_at TEXT NOT NULL
                );
                CREATE TABLE evidence_records(
                    evidence_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    revision_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE cost_evaluations(
                    evidence_id TEXT PRIMARY KEY
                        REFERENCES evidence_records(evidence_id) ON DELETE CASCADE,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    revision_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL
                );
                CREATE TABLE release_policies(
                    project_id TEXT PRIMARY KEY REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    policy_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL
                );
                CREATE TABLE connector_snapshots(
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    snapshot_id TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL UNIQUE,
                    hardware_revision_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, snapshot_id)
                );
                CREATE TABLE change_assessments(
                    analysis_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    from_snapshot_id TEXT NOT NULL,
                    to_snapshot_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    FOREIGN KEY(project_id, from_snapshot_id)
                        REFERENCES connector_snapshots(project_id, snapshot_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(project_id, to_snapshot_id)
                        REFERENCES connector_snapshots(project_id, snapshot_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE change_previews(
                    preview_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    scenario_id TEXT NOT NULL,
                    scenario_hash TEXT NOT NULL UNIQUE,
                    baseline_snapshot_id TEXT NOT NULL,
                    baseline_snapshot_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    FOREIGN KEY(project_id, baseline_snapshot_id)
                        REFERENCES connector_snapshots(project_id, snapshot_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE plan_verifications(
                    verification_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    scenario_id TEXT NOT NULL,
                    preview_hash TEXT NOT NULL
                        REFERENCES change_previews(preview_hash) ON DELETE CASCADE,
                    actual_change_analysis_hash TEXT NOT NULL
                        REFERENCES change_assessments(analysis_hash) ON DELETE CASCADE,
                    payload_json TEXT NOT NULL,
                    verified_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL
                );
                CREATE TABLE external_evidence_plans(
                    plan_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    scenario_id TEXT NOT NULL,
                    scenario_hash TEXT NOT NULL UNIQUE,
                    baseline_snapshot_id TEXT NOT NULL,
                    baseline_snapshot_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    FOREIGN KEY(project_id, baseline_snapshot_id)
                        REFERENCES connector_snapshots(project_id, snapshot_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE external_evidence_plan_verifications(
                    verification_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    plan_hash TEXT NOT NULL
                        REFERENCES external_evidence_plans(plan_hash)
                        ON DELETE CASCADE,
                    actual_change_analysis_hash TEXT NOT NULL
                        REFERENCES change_assessments(analysis_hash)
                        ON DELETE CASCADE,
                    payload_json TEXT NOT NULL,
                    verified_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL
                );
                CREATE TABLE design_proposals(
                    proposal_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    preview_hash TEXT NOT NULL
                        REFERENCES change_previews(preview_hash) ON DELETE CASCADE,
                    payload_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL
                );
                CREATE TABLE design_candidates(
                    candidate_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    session_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    proposal_hash TEXT NOT NULL
                        REFERENCES design_proposals(proposal_hash) ON DELETE CASCADE,
                    payload_json TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    UNIQUE(project_id, candidate_id, revision),
                    UNIQUE(project_id, candidate_hash)
                );
                CREATE TABLE simulation_bindings(
                    simulation_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    session_id TEXT NOT NULL,
                    simulation_id TEXT NOT NULL,
                    candidate_hash TEXT NOT NULL
                        REFERENCES design_candidates(candidate_hash) ON DELETE CASCADE,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    UNIQUE(project_id, simulation_id),
                    UNIQUE(project_id, simulation_hash)
                );
                CREATE TABLE conversational_evidence_claims(
                    claim_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    session_id TEXT NOT NULL,
                    claim_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    UNIQUE(project_id, claim_id)
                );
                CREATE TABLE design_state_transitions(
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK(sequence >= 1),
                    from_state TEXT NOT NULL,
                    to_state TEXT NOT NULL,
                    candidate_hash TEXT,
                    simulation_hash TEXT,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, session_id, sequence),
                    FOREIGN KEY(project_id, candidate_hash)
                        REFERENCES design_candidates(project_id, candidate_hash)
                        ON DELETE CASCADE,
                    FOREIGN KEY(project_id, simulation_hash)
                        REFERENCES simulation_bindings(project_id, simulation_hash)
                        ON DELETE CASCADE
                );
                CREATE TABLE release_diagnoses(
                    diagnosis_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    decision_hash TEXT NOT NULL
                        REFERENCES release_decisions(decision_hash) ON DELETE CASCADE,
                    payload_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL
                );
                CREATE TABLE resolution_plans(
                    plan_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    diagnosis_hash TEXT NOT NULL
                        REFERENCES release_diagnoses(diagnosis_hash) ON DELETE CASCADE,
                    fix_proposal_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    selected_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL
                );
                CREATE TABLE release_evidence(
                    evidence_id TEXT NOT NULL,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    evidence_kind TEXT NOT NULL CHECK(
                        evidence_kind IN ('bom_cost', 'firmware_build', 'test_result')
                    ),
                    change_analysis_hash TEXT NOT NULL
                        REFERENCES change_assessments(analysis_hash) ON DELETE CASCADE,
                    snapshot_hash TEXT NOT NULL,
                    hardware_revision_id TEXT NOT NULL,
                    evidence_hash TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, evidence_id)
                );
                CREATE TABLE release_decisions(
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    sequence INTEGER NOT NULL CHECK(sequence >= 1),
                    previous_decision_hash TEXT
                        REFERENCES release_decisions(decision_hash),
                    decision_hash TEXT NOT NULL UNIQUE,
                    report_id TEXT NOT NULL,
                    change_analysis_hash TEXT NOT NULL
                        REFERENCES change_assessments(analysis_hash) ON DELETE CASCADE,
                    target_snapshot_id TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL,
                    hardware_revision_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('ready', 'blocked')),
                    payload_json TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, sequence),
                    FOREIGN KEY(project_id, target_snapshot_id)
                        REFERENCES connector_snapshots(project_id, snapshot_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE organizations(
                    org_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE actors(
                    actor_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK(active IN (0, 1))
                );
                CREATE TABLE memberships(
                    org_id TEXT NOT NULL REFERENCES organizations(org_id)
                        ON DELETE CASCADE,
                    actor_id TEXT NOT NULL REFERENCES actors(actor_id)
                        ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    granted_at TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK(active IN (0, 1)),
                    PRIMARY KEY(org_id, actor_id)
                );
                CREATE TABLE project_access(
                    org_id TEXT NOT NULL REFERENCES organizations(org_id)
                        ON DELETE CASCADE,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    actor_id TEXT NOT NULL REFERENCES actors(actor_id)
                        ON DELETE CASCADE,
                    payload_json TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK(active IN (0, 1)),
                    PRIMARY KEY(org_id, project_id, actor_id)
                );
                CREATE INDEX project_access_by_actor
                    ON project_access(actor_id, org_id, project_id);
                CREATE TABLE audit_events(
                    event_id TEXT PRIMARY KEY,
                    event_hash TEXT NOT NULL UNIQUE,
                    org_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    result_hash TEXT NOT NULL,
                    allowed INTEGER NOT NULL CHECK(allowed IN (0, 1)),
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX audit_events_by_project
                    ON audit_events(project_id, occurred_at, event_id);
                CREATE TABLE knowledge_sources(
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    source_id TEXT NOT NULL,
                    source_hash TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, source_id)
                );
                CREATE TABLE conversation_runtime_results(
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    request_id TEXT NOT NULL,
                    runtime_hash TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, request_id)
                );
                CREATE TABLE cad_geometry_assets(
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    asset_id TEXT NOT NULL,
                    asset_hash TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, asset_id)
                );
                CREATE TABLE idempotency_records(
                    operation TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    local_installation_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(operation, project_id, local_installation_id, key)
                );
                INSERT INTO schema_migrations(version, applied_at)
                    VALUES(1, '2026-08-28T00:00:00.000000Z');
                INSERT INTO schema_migrations(version, applied_at)
                    VALUES(2, '2026-08-29T00:00:00.000000Z');
                INSERT INTO schema_migrations(version, applied_at)
                    VALUES(3, '2026-08-29T02:20:00.000000Z');
                INSERT INTO schema_migrations(version, applied_at)
                    VALUES(4, '2026-08-30T00:00:00.000000Z');
                INSERT INTO schema_migrations(version, applied_at)
                    VALUES(5, '2026-08-31T00:00:00.000000Z');
                INSERT INTO schema_migrations(version, applied_at)
                    VALUES(6, '2026-08-31T06:00:00.000000Z');
                INSERT INTO schema_migrations(version, applied_at)
                    VALUES(7, '2026-09-01T00:00:00.000000Z');
                INSERT INTO schema_migrations(version, applied_at)
                    VALUES(8, '2026-09-03T00:00:00.000000Z');
                INSERT INTO schema_migrations(version, applied_at)
                    VALUES(9, '2026-09-03T06:00:00.000000Z');
                INSERT INTO schema_migrations(version, applied_at)
                    VALUES(10, '2026-09-03T08:00:00.000000Z');
                PRAGMA user_version = 10;
                COMMIT;
                """
            )
        except sqlite3.Error as exc:
            with suppress(sqlite3.Error):
                connection.rollback()
            raise MigrationError("failed to initialize SQLite schema") from exc

    def _migrate(self, connection: sqlite3.Connection, version: int) -> None:
        if version not in {1, 2, 3, 4, 5, 6, 7, 8, 9}:
            raise MigrationError(f"no migration path from schema {version}")
        try:
            if version == 1:
                connection.executescript(
                    """
                BEGIN IMMEDIATE;
                CREATE TABLE approved_specs(
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    spec_id TEXT NOT NULL,
                    spec_version INTEGER NOT NULL CHECK(spec_version >= 1),
                    spec_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, spec_id, spec_version),
                    FOREIGN KEY(project_id, spec_id, spec_version)
                        REFERENCES specs(project_id, spec_id, spec_version)
                        ON DELETE CASCADE
                );
                CREATE TABLE spec_state_events(
                    event_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    spec_id TEXT NOT NULL,
                    spec_version INTEGER NOT NULL CHECK(spec_version >= 1),
                    sequence INTEGER NOT NULL CHECK(sequence BETWEEN 1 AND 3),
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    UNIQUE(project_id, spec_id, spec_version, sequence),
                    FOREIGN KEY(project_id, spec_id, spec_version)
                        REFERENCES specs(project_id, spec_id, spec_version)
                        ON DELETE CASCADE
                );
                CREATE TABLE cost_evaluations(
                    evidence_id TEXT PRIMARY KEY
                        REFERENCES evidence_records(evidence_id) ON DELETE CASCADE,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    revision_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL
                );
                INSERT INTO schema_migrations(version, applied_at)
                    VALUES(2, '2026-08-29T00:00:00.000000Z');
                PRAGMA user_version = 2;
                COMMIT;
                """
                )
                version = 2
            if version == 2:
                connection.executescript(
                    """
                BEGIN IMMEDIATE;
                CREATE TABLE release_policies(
                    project_id TEXT PRIMARY KEY REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    policy_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL
                );
                CREATE TABLE connector_snapshots(
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    snapshot_id TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL UNIQUE,
                    hardware_revision_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, snapshot_id)
                );
                CREATE TABLE change_assessments(
                    analysis_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    from_snapshot_id TEXT NOT NULL,
                    to_snapshot_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    FOREIGN KEY(project_id, from_snapshot_id)
                        REFERENCES connector_snapshots(project_id, snapshot_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(project_id, to_snapshot_id)
                        REFERENCES connector_snapshots(project_id, snapshot_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE release_evidence(
                    evidence_id TEXT NOT NULL,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    evidence_kind TEXT NOT NULL CHECK(
                        evidence_kind IN ('bom_cost', 'firmware_build', 'test_result')
                    ),
                    change_analysis_hash TEXT NOT NULL
                        REFERENCES change_assessments(analysis_hash) ON DELETE CASCADE,
                    snapshot_hash TEXT NOT NULL,
                    hardware_revision_id TEXT NOT NULL,
                    evidence_hash TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, evidence_id)
                );
                CREATE TABLE release_decisions(
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    sequence INTEGER NOT NULL CHECK(sequence >= 1),
                    previous_decision_hash TEXT
                        REFERENCES release_decisions(decision_hash),
                    decision_hash TEXT NOT NULL UNIQUE,
                    report_id TEXT NOT NULL,
                    change_analysis_hash TEXT NOT NULL
                        REFERENCES change_assessments(analysis_hash) ON DELETE CASCADE,
                    target_snapshot_id TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL,
                    hardware_revision_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('ready', 'blocked')),
                    payload_json TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, sequence),
                    FOREIGN KEY(project_id, target_snapshot_id)
                        REFERENCES connector_snapshots(project_id, snapshot_id)
                        ON DELETE CASCADE
                );
                INSERT INTO schema_migrations(version, applied_at)
                    VALUES(3, '2026-08-29T02:20:00.000000Z');
                PRAGMA user_version = 3;
                """
                )
                policy_hash = canonical_sha256(DEFAULT_RELEASE_READINESS_POLICY)
                projects = connection.execute(
                    "SELECT project_id, created_at FROM projects ORDER BY project_id"
                ).fetchall()
                for project in projects:
                    stored_at = datetime.fromisoformat(
                        str(project["created_at"]).replace("Z", "+00:00")
                    )
                    release_policy = StoredReleasePolicy(
                        project_id=str(project["project_id"]),
                        policy_hash=policy_hash,
                        policy=DEFAULT_RELEASE_READINESS_POLICY,
                        stored_at=stored_at,
                    )
                    connection.execute(
                        "INSERT INTO release_policies VALUES(?, ?, ?, ?)",
                        (
                            release_policy.project_id,
                            release_policy.policy_hash,
                            _canonical_model(release_policy),
                            _utc_text(release_policy.stored_at),
                        ),
                    )
                connection.commit()
                version = 3
            if version == 3:
                connection.executescript(
                    """
                BEGIN IMMEDIATE;
                CREATE TABLE change_previews(
                    preview_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    scenario_id TEXT NOT NULL,
                    scenario_hash TEXT NOT NULL UNIQUE,
                    baseline_snapshot_id TEXT NOT NULL,
                    baseline_snapshot_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    FOREIGN KEY(project_id, baseline_snapshot_id)
                        REFERENCES connector_snapshots(project_id, snapshot_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE plan_verifications(
                    verification_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    scenario_id TEXT NOT NULL,
                    preview_hash TEXT NOT NULL
                        REFERENCES change_previews(preview_hash) ON DELETE CASCADE,
                    actual_change_analysis_hash TEXT NOT NULL
                        REFERENCES change_assessments(analysis_hash) ON DELETE CASCADE,
                    payload_json TEXT NOT NULL,
                    verified_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL
                );
                INSERT INTO schema_migrations(version, applied_at)
                    VALUES(4, '2026-08-30T00:00:00.000000Z');
                PRAGMA user_version = 4;
                COMMIT;
                """
                )
                version = 4
            if version == 4:
                connection.executescript(
                    """
                BEGIN IMMEDIATE;
                CREATE TABLE external_evidence_plans(
                    plan_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    scenario_id TEXT NOT NULL,
                    scenario_hash TEXT NOT NULL UNIQUE,
                    baseline_snapshot_id TEXT NOT NULL,
                    baseline_snapshot_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    FOREIGN KEY(project_id, baseline_snapshot_id)
                        REFERENCES connector_snapshots(project_id, snapshot_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE external_evidence_plan_verifications(
                    verification_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    plan_hash TEXT NOT NULL
                        REFERENCES external_evidence_plans(plan_hash)
                        ON DELETE CASCADE,
                    actual_change_analysis_hash TEXT NOT NULL
                        REFERENCES change_assessments(analysis_hash)
                        ON DELETE CASCADE,
                    payload_json TEXT NOT NULL,
                    verified_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL
                );
                INSERT INTO schema_migrations(version, applied_at)
                    VALUES(5, '2026-08-31T00:00:00.000000Z');
                PRAGMA user_version = 5;
                COMMIT;
                """
                )
                version = 5
            if version == 5:
                connection.executescript(
                    """
                BEGIN IMMEDIATE;
                CREATE TABLE design_proposals(
                    proposal_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    preview_hash TEXT NOT NULL
                        REFERENCES change_previews(preview_hash) ON DELETE CASCADE,
                    payload_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL
                );
                CREATE TABLE release_diagnoses(
                    diagnosis_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    decision_hash TEXT NOT NULL
                        REFERENCES release_decisions(decision_hash) ON DELETE CASCADE,
                    payload_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL
                );
                CREATE TABLE resolution_plans(
                    plan_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    diagnosis_hash TEXT NOT NULL
                        REFERENCES release_diagnoses(diagnosis_hash) ON DELETE CASCADE,
                    fix_proposal_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    selected_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL
                );
                INSERT INTO schema_migrations(version, applied_at)
                    VALUES(6, '2026-08-31T06:00:00.000000Z');
                PRAGMA user_version = 6;
                COMMIT;
                """
                )
                version = 6
            if version == 6:
                connection.executescript(
                    """
                BEGIN IMMEDIATE;
                CREATE TABLE design_candidates(
                    candidate_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    session_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    proposal_hash TEXT NOT NULL
                        REFERENCES design_proposals(proposal_hash) ON DELETE CASCADE,
                    payload_json TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    UNIQUE(project_id, candidate_id, revision),
                    UNIQUE(project_id, candidate_hash)
                );
                CREATE TABLE simulation_bindings(
                    simulation_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    session_id TEXT NOT NULL,
                    simulation_id TEXT NOT NULL,
                    candidate_hash TEXT NOT NULL
                        REFERENCES design_candidates(candidate_hash) ON DELETE CASCADE,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    UNIQUE(project_id, simulation_id),
                    UNIQUE(project_id, simulation_hash)
                );
                CREATE TABLE conversational_evidence_claims(
                    claim_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    session_id TEXT NOT NULL,
                    claim_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    UNIQUE(project_id, claim_id)
                );
                CREATE TABLE design_state_transitions(
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK(sequence >= 1),
                    from_state TEXT NOT NULL,
                    to_state TEXT NOT NULL,
                    candidate_hash TEXT,
                    simulation_hash TEXT,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, session_id, sequence),
                    FOREIGN KEY(project_id, candidate_hash)
                        REFERENCES design_candidates(project_id, candidate_hash)
                        ON DELETE CASCADE,
                    FOREIGN KEY(project_id, simulation_hash)
                        REFERENCES simulation_bindings(project_id, simulation_hash)
                        ON DELETE CASCADE
                );
                INSERT INTO schema_migrations(version, applied_at)
                    VALUES(7, '2026-09-01T00:00:00.000000Z');
                PRAGMA user_version = 7;
                COMMIT;
                """
                )
                version = 7
            if version == 7:
                connection.executescript(
                    """
                BEGIN IMMEDIATE;
                CREATE TABLE organizations(
                    org_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE actors(
                    actor_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK(active IN (0, 1))
                );
                CREATE TABLE memberships(
                    org_id TEXT NOT NULL REFERENCES organizations(org_id)
                        ON DELETE CASCADE,
                    actor_id TEXT NOT NULL REFERENCES actors(actor_id)
                        ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    granted_at TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK(active IN (0, 1)),
                    PRIMARY KEY(org_id, actor_id)
                );
                CREATE TABLE project_access(
                    org_id TEXT NOT NULL REFERENCES organizations(org_id)
                        ON DELETE CASCADE,
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    actor_id TEXT NOT NULL REFERENCES actors(actor_id)
                        ON DELETE CASCADE,
                    payload_json TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK(active IN (0, 1)),
                    PRIMARY KEY(org_id, project_id, actor_id)
                );
                CREATE INDEX project_access_by_actor
                    ON project_access(actor_id, org_id, project_id);
                CREATE TABLE audit_events(
                    event_id TEXT PRIMARY KEY,
                    event_hash TEXT NOT NULL UNIQUE,
                    org_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    result_hash TEXT NOT NULL,
                    allowed INTEGER NOT NULL CHECK(allowed IN (0, 1)),
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX audit_events_by_project
                    ON audit_events(project_id, occurred_at, event_id);
                INSERT INTO schema_migrations(version, applied_at)
                    VALUES(8, '2026-09-03T00:00:00.000000Z');
                PRAGMA user_version = 8;
                """
                )
                projects = connection.execute(
                    "SELECT * FROM projects ORDER BY project_id"
                ).fetchall()
                for row in projects:
                    project = self._validate(
                        ProjectRecord,
                        {
                            "project_id": row["project_id"],
                            "name": row["name"],
                            "version": row["version"],
                            "created_at": row["created_at"],
                            "updated_at": row["updated_at"],
                        },
                    )
                    self._bootstrap_local_access_for_project(connection, project)
                connection.commit()
                version = 8
            if version == 8:
                connection.executescript(
                    """
                BEGIN IMMEDIATE;
                CREATE TABLE knowledge_sources(
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    source_id TEXT NOT NULL,
                    source_hash TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, source_id)
                );
                CREATE TABLE conversation_runtime_results(
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    request_id TEXT NOT NULL,
                    runtime_hash TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, request_id)
                );
                INSERT INTO schema_migrations(version, applied_at)
                    VALUES(9, '2026-09-03T06:00:00.000000Z');
                PRAGMA user_version = 9;
                COMMIT;
                """
                )
                version = 9
            if version == 9:
                connection.executescript(
                    """
                BEGIN IMMEDIATE;
                CREATE TABLE cad_geometry_assets(
                    project_id TEXT NOT NULL REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    asset_id TEXT NOT NULL,
                    asset_hash TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, asset_id)
                );
                INSERT INTO schema_migrations(version, applied_at)
                    VALUES(10, '2026-09-03T08:00:00.000000Z');
                PRAGMA user_version = 10;
                COMMIT;
                """
                )
        except (sqlite3.Error, ValidationError, ValueError) as exc:
            with suppress(sqlite3.Error):
                connection.rollback()
            raise MigrationError("failed to migrate SQLite schema") from exc

    def create_project(self, project: ProjectRecord) -> ProjectRecord:
        def insert(connection: sqlite3.Connection) -> ProjectRecord:
            self._insert_project(connection, project)
            self._bootstrap_local_access_for_project(connection, project)
            return project

        return self._write(insert)

    @staticmethod
    def _insert_project(connection: sqlite3.Connection, project: ProjectRecord) -> None:
        if project.version != 1:
            raise IntegrityConflictError("new projects must start at version 1")
        _safe_id(project.project_id, "project_id")
        try:
            connection.execute(
                "INSERT INTO projects VALUES(?, ?, ?, ?, ?)",
                (
                    project.project_id,
                    project.name,
                    project.version,
                    _utc_text(project.created_at),
                    _utc_text(project.updated_at),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise IntegrityConflictError("project already exists") from exc

    def get_project(self, project_id: str) -> ProjectRecord:
        with self._reading() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("project not found")
        return self._validate(
            ProjectRecord,
            {
                "project_id": row["project_id"],
                "name": row["name"],
                "version": row["version"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            },
        )

    @classmethod
    def _bootstrap_local_access_for_project(
        cls, connection: sqlite3.Connection, project: ProjectRecord
    ) -> None:
        organization = Organization(
            org_id=LOCAL_BOOTSTRAP_ORG_ID,
            name="Local FORGE Workspace",
            created_at=project.created_at,
        )
        actor = Actor(
            actor_id=LOCAL_BOOTSTRAP_ACTOR_ID,
            display_name="Local Operator",
            active=True,
        )
        membership = Membership(
            org_id=organization.org_id,
            actor_id=actor.actor_id,
            role=Role.ADMIN,
            granted_by=actor.actor_id,
            granted_at=project.created_at,
            active=True,
        )
        access = ProjectAccess(
            org_id=organization.org_id,
            project_id=project.project_id,
            actor_id=actor.actor_id,
            active=True,
        )
        cls._put_organization(connection, organization)
        cls._put_actor(connection, actor)
        cls._put_membership(connection, membership)
        cls._put_project_access(connection, access)

    def insert_organization(self, record: Organization) -> Organization:
        def put(connection: sqlite3.Connection) -> Organization:
            self._put_organization(connection, record)
            return record

        return self._write(put)

    def get_organization(self, org_id: str) -> Organization:
        with self._reading() as connection:
            row = connection.execute(
                "SELECT * FROM organizations WHERE org_id = ?", (org_id,)
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("organization not found")
        return self._organization_from_row(row)

    def insert_actor(self, record: Actor) -> Actor:
        def put(connection: sqlite3.Connection) -> Actor:
            self._put_actor(connection, record)
            return record

        return self._write(put)

    def get_actor(self, actor_id: str) -> Actor:
        with self._reading() as connection:
            row = connection.execute(
                "SELECT * FROM actors WHERE actor_id = ?", (actor_id,)
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("actor not found")
        return self._actor_from_row(row)

    def insert_membership(self, record: Membership) -> Membership:
        def put(connection: sqlite3.Connection) -> Membership:
            self._put_membership(connection, record)
            return record

        return self._write(put)

    def get_membership(self, org_id: str, actor_id: str) -> Membership:
        with self._reading() as connection:
            row = connection.execute(
                """
                SELECT * FROM memberships
                WHERE org_id = ? AND actor_id = ?
                """,
                (org_id, actor_id),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("membership not found")
        return self._membership_from_row(row)

    def insert_project_access(self, record: ProjectAccess) -> ProjectAccess:
        def put(connection: sqlite3.Connection) -> ProjectAccess:
            self._put_project_access(connection, record)
            return record

        return self._write(put)

    def get_project_access(
        self, org_id: str, project_id: str, actor_id: str
    ) -> ProjectAccess:
        with self._reading() as connection:
            row = connection.execute(
                """
                SELECT * FROM project_access
                WHERE org_id = ? AND project_id = ? AND actor_id = ?
                """,
                (org_id, project_id, actor_id),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("project access not found")
        return self._project_access_from_row(row)

    def append_audit_event(self, record: AuditEvent) -> AuditEvent:
        def append(connection: sqlite3.Connection) -> AuditEvent:
            self._append_audit_event(connection, record)
            return record

        return self._write(append)

    def get_audit_event(self, event_id: str) -> AuditEvent:
        with self._reading() as connection:
            row = connection.execute(
                "SELECT * FROM audit_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("audit event not found")
        return self._audit_event_from_row(row)

    def list_audit_events(self, project_id: str) -> tuple[AuditEvent, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT * FROM audit_events
                WHERE project_id = ? ORDER BY occurred_at, event_id
                """,
                (project_id,),
            ).fetchall()
        return tuple(self._audit_event_from_row(row) for row in rows)

    def insert_knowledge_source(
        self, record: StoredKnowledgeSource
    ) -> StoredKnowledgeSource:
        def insert(connection: sqlite3.Connection) -> StoredKnowledgeSource:
            self._insert_knowledge_source(connection, record)
            return record

        return self._write(insert)

    def get_knowledge_source(
        self, project_id: str, source_id: str
    ) -> StoredKnowledgeSource:
        with self._reading() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_sources "
                "WHERE project_id = ? AND source_id = ?",
                (project_id, source_id),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("knowledge source not found")
        return self._knowledge_source_from_row(row)

    def list_knowledge_sources(
        self, project_id: str
    ) -> tuple[StoredKnowledgeSource, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_sources WHERE project_id = ? "
                "ORDER BY stored_at, source_id",
                (project_id,),
            ).fetchall()
        return tuple(self._knowledge_source_from_row(row) for row in rows)

    def insert_runtime_result(self, record: StoredRuntimeResult) -> StoredRuntimeResult:
        def insert(connection: sqlite3.Connection) -> StoredRuntimeResult:
            self._insert_runtime_result(connection, record)
            return record

        return self._write(insert)

    def get_runtime_result(
        self, project_id: str, request_id: str
    ) -> StoredRuntimeResult:
        with self._reading() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_runtime_results "
                "WHERE project_id = ? AND request_id = ?",
                (project_id, request_id),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("conversation runtime result not found")
        return self._runtime_result_from_row(row)

    def list_runtime_results(self, project_id: str) -> tuple[StoredRuntimeResult, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                "SELECT * FROM conversation_runtime_results WHERE project_id = ? "
                "ORDER BY stored_at, request_id",
                (project_id,),
            ).fetchall()
        return tuple(self._runtime_result_from_row(row) for row in rows)

    def insert_cad_geometry(
        self, record: StoredCADGeometryAsset
    ) -> StoredCADGeometryAsset:
        def insert(connection: sqlite3.Connection) -> StoredCADGeometryAsset:
            self._insert_cad_geometry(connection, record)
            return record

        return self._write(insert)

    def get_cad_geometry(
        self, project_id: str, asset_id: str
    ) -> StoredCADGeometryAsset:
        with self._reading() as connection:
            row = connection.execute(
                "SELECT * FROM cad_geometry_assets "
                "WHERE project_id = ? AND asset_id = ?",
                (project_id, asset_id),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("CAD geometry asset not found")
        return self._cad_geometry_from_row(row)

    def list_cad_geometries(
        self, project_id: str
    ) -> tuple[StoredCADGeometryAsset, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                "SELECT * FROM cad_geometry_assets WHERE project_id = ? "
                "ORDER BY stored_at, asset_id",
                (project_id,),
            ).fetchall()
        return tuple(self._cad_geometry_from_row(row) for row in rows)

    def delete_project(self, project_id: str, *, expected_project_version: int) -> None:
        def delete(connection: sqlite3.Connection) -> None:
            self._require_project_version(
                connection, project_id, expected_project_version
            )
            connection.execute(
                "DELETE FROM projects WHERE project_id = ?", (project_id,)
            )

        self._write(delete)

    def _project_write(
        self,
        project_id: str,
        expected_project_version: int,
        updated_at: datetime,
        operation: Callable[[sqlite3.Connection], None],
    ) -> int:
        def write(connection: sqlite3.Connection) -> int:
            row = self._require_project_version(
                connection, project_id, expected_project_version
            )
            timestamp = _utc_text(updated_at)
            if timestamp < str(row["updated_at"]):
                raise IntegrityConflictError("project timestamp cannot move backwards")
            operation(connection)
            cursor = connection.execute(
                """
                UPDATE projects SET version = version + 1, updated_at = ?
                WHERE project_id = ? AND version = ?
                """,
                (timestamp, project_id, expected_project_version),
            )
            if cursor.rowcount != 1:
                raise VersionConflictError("project version changed during write")
            return expected_project_version + 1

        return self._write(write)

    @staticmethod
    def _require_project_version(
        connection: sqlite3.Connection,
        project_id: str,
        expected_version: int,
    ) -> sqlite3.Row:
        row: sqlite3.Row | None = connection.execute(
            "SELECT version, updated_at FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise RecordNotFoundError("project not found")
        if int(row["version"]) != expected_version:
            raise VersionConflictError(
                f"expected version {expected_version}; current version {row['version']}"
            )
        return row

    def store_spec(self, record: StoredSpec, *, expected_project_version: int) -> int:
        def insert(connection: sqlite3.Connection) -> None:
            self._insert_spec(connection, record)

        return self._project_write(
            record.project_id, expected_project_version, record.stored_at, insert
        )

    def _insert_spec(self, connection: sqlite3.Connection, record: StoredSpec) -> None:
        _safe_id(record.spec_id, "spec_id")
        latest = connection.execute(
            """
            SELECT MAX(spec_version) AS version FROM specs
            WHERE project_id = ? AND spec_id = ?
            """,
            (record.project_id, record.spec_id),
        ).fetchone()["version"]
        if record.spec_version != (1 if latest is None else int(latest) + 1):
            raise IntegrityConflictError("spec versions must be contiguous")
        self._insert_immutable(
            connection,
            "INSERT INTO specs VALUES(?, ?, ?, ?, ?, ?)",
            (
                record.project_id,
                record.spec_id,
                record.spec_version,
                record.spec_hash,
                _canonical_model(record),
                _utc_text(record.stored_at),
            ),
        )

    def get_spec(self, project_id: str, spec_id: str, spec_version: int) -> StoredSpec:
        with self._reading() as connection:
            row = connection.execute(
                """
                SELECT * FROM approved_specs
                WHERE project_id = ? AND spec_id = ? AND spec_version = ?
                """,
                (project_id, spec_id, spec_version),
            ).fetchone()
            draft_row = connection.execute(
                """
                SELECT * FROM specs
                WHERE project_id = ? AND spec_id = ? AND spec_version = ?
                """,
                (project_id, spec_id, spec_version),
            ).fetchone()
        if row is None:
            if draft_row is None:
                raise RecordNotFoundError("record not found")
            return self._spec_from_row(draft_row)
        if draft_row is None:
            raise CorruptRecordError("approved spec has no draft")
        return self._approved_spec_from_rows(row, draft_row)

    def get_draft_spec(
        self, project_id: str, spec_id: str, spec_version: int
    ) -> StoredSpec:
        with self._reading() as connection:
            row = connection.execute(
                """
                SELECT * FROM specs
                WHERE project_id = ? AND spec_id = ? AND spec_version = ?
                """,
                (project_id, spec_id, spec_version),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("record not found")
        return self._spec_from_row(row)

    def get_approved_spec(
        self, project_id: str, spec_id: str, spec_version: int
    ) -> StoredSpec:
        with self._reading() as connection:
            row = connection.execute(
                """
                SELECT * FROM approved_specs
                WHERE project_id = ? AND spec_id = ? AND spec_version = ?
                """,
                (project_id, spec_id, spec_version),
            ).fetchone()
            draft_row = connection.execute(
                """
                SELECT * FROM specs
                WHERE project_id = ? AND spec_id = ? AND spec_version = ?
                """,
                (project_id, spec_id, spec_version),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("approved spec not found")
        if draft_row is None:
            raise CorruptRecordError("approved spec has no draft")
        return self._approved_spec_from_rows(row, draft_row)

    def list_specs(self, project_id: str, spec_id: str) -> tuple[StoredSpec, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT d.*, a.payload_json AS approved_payload_json,
                       a.spec_hash AS approved_spec_hash,
                       a.stored_at AS approved_stored_at
                FROM specs d
                LEFT JOIN approved_specs a
                  ON a.project_id = d.project_id
                 AND a.spec_id = d.spec_id
                 AND a.spec_version = d.spec_version
                WHERE d.project_id = ? AND d.spec_id = ?
                ORDER BY d.spec_version
                """,
                (project_id, spec_id),
            ).fetchall()
        values: list[StoredSpec] = []
        for row in rows:
            draft = self._spec_from_row(row)
            if row["approved_payload_json"] is None:
                values.append(draft)
                continue
            approved = self._decode(StoredSpec, row["approved_payload_json"])
            self._require_binding(
                approved.project_id == row["project_id"]
                and approved.spec_id == row["spec_id"]
                and approved.spec_version == int(row["spec_version"])
                and approved.spec_hash == row["approved_spec_hash"]
                and _utc_text(approved.stored_at) == row["approved_stored_at"],
                "approved spec row does not match its payload",
            )
            self._require_approved_snapshot(draft, approved)
            values.append(approved)
        return tuple(values)

    def store_approved_spec(
        self, record: StoredSpec, *, expected_project_version: int
    ) -> int:
        def insert(connection: sqlite3.Connection) -> None:
            self._insert_approved_spec(connection, record)

        return self._project_write(
            record.project_id, expected_project_version, record.stored_at, insert
        )

    def _insert_approved_spec(
        self, connection: sqlite3.Connection, record: StoredSpec
    ) -> None:
        if record.spec.status is not SpecStatus.APPROVED:
            raise IntegrityConflictError("approved snapshot must be approved")
        row = connection.execute(
            """
            SELECT * FROM specs
            WHERE project_id = ? AND spec_id = ? AND spec_version = ?
            """,
            (record.project_id, record.spec_id, record.spec_version),
        ).fetchone()
        if row is None:
            raise RecordNotFoundError("draft spec not found")
        draft = self._spec_from_row(row)
        expected = draft.spec.model_copy(
            update={
                "status": SpecStatus.APPROVED,
                "approved_at": record.spec.approved_at,
            }
        )
        if draft.spec.status is not SpecStatus.DRAFT or expected != record.spec:
            raise IntegrityConflictError(
                "approved snapshot must preserve the exact draft payload"
            )
        self._insert_immutable(
            connection,
            "INSERT INTO approved_specs VALUES(?, ?, ?, ?, ?, ?)",
            (
                record.project_id,
                record.spec_id,
                record.spec_version,
                record.spec_hash,
                _canonical_model(record),
                _utc_text(record.stored_at),
            ),
        )

    def append_spec_event(
        self, event: SpecStateEvent, *, expected_project_version: int
    ) -> int:
        def insert(connection: sqlite3.Connection) -> None:
            self._append_spec_event(connection, event)

        return self._project_write(
            event.project_id, expected_project_version, event.occurred_at, insert
        )

    def _append_spec_event(
        self, connection: sqlite3.Connection, event: SpecStateEvent
    ) -> None:
        _safe_id(event.event_id, "event_id")
        latest = connection.execute(
            """
            SELECT * FROM spec_state_events
            WHERE project_id = ? AND spec_id = ? AND spec_version = ?
            ORDER BY sequence DESC LIMIT 1
            """,
            (event.project_id, event.spec_id, event.spec_version),
        ).fetchone()
        if latest is None:
            if event.sequence != 1:
                raise IntegrityConflictError("spec lifecycle must start at draft")
        else:
            previous = self._spec_event_from_row(latest)
            if (
                event.sequence != previous.sequence + 1
                or event.previous_status is not previous.status
                or event.occurred_at < previous.occurred_at
            ):
                raise IntegrityConflictError(
                    "spec lifecycle must be append-only and contiguous"
                )
        self._insert_immutable(
            connection,
            "INSERT INTO spec_state_events VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.project_id,
                event.spec_id,
                event.spec_version,
                event.sequence,
                event.status.value,
                _canonical_model(event),
                _utc_text(event.occurred_at),
            ),
        )

    def list_spec_events(
        self, project_id: str, spec_id: str, spec_version: int
    ) -> tuple[SpecStateEvent, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT * FROM spec_state_events
                WHERE project_id = ? AND spec_id = ? AND spec_version = ?
                ORDER BY sequence
                """,
                (project_id, spec_id, spec_version),
            ).fetchall()
        return tuple(self._spec_event_from_row(row) for row in rows)

    def store_revision(
        self, record: StoredRevision, *, expected_project_version: int
    ) -> int:
        def insert(connection: sqlite3.Connection) -> None:
            self._insert_revision(connection, record)

        return self._project_write(
            record.project_id, expected_project_version, record.stored_at, insert
        )

    def _insert_revision(
        self, connection: sqlite3.Connection, record: StoredRevision
    ) -> None:
        _safe_id(record.revision_id, "revision_id")
        previous = connection.execute(
            """
            SELECT revision_id, revision_number FROM revisions
            WHERE project_id = ? ORDER BY revision_number DESC LIMIT 1
            """,
            (record.project_id,),
        ).fetchone()
        if previous is None and record.revision_number != 1:
            raise IntegrityConflictError("first revision must be number 1")
        if previous is not None and (
            record.revision_number != int(previous["revision_number"]) + 1
            or record.revision.parent_revision_id != previous["revision_id"]
        ):
            raise IntegrityConflictError("revision lineage must be contiguous")
        self._insert_immutable(
            connection,
            "INSERT INTO revisions VALUES(?, ?, ?, ?, ?, ?)",
            (
                record.project_id,
                record.revision_id,
                record.revision_number,
                record.revision_hash,
                _canonical_model(record),
                _utc_text(record.stored_at),
            ),
        )

    def get_revision(self, project_id: str, revision_id: str) -> StoredRevision:
        with self._reading() as connection:
            row = connection.execute(
                """
                SELECT * FROM revisions
                WHERE project_id = ? AND revision_id = ?
                """,
                (project_id, revision_id),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("record not found")
        return self._revision_from_row(row)

    def list_revisions(self, project_id: str) -> tuple[StoredRevision, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT * FROM revisions
                WHERE project_id = ? ORDER BY revision_number
                """,
                (project_id,),
            ).fetchall()
        return tuple(self._revision_from_row(row) for row in rows)

    def store_approval(
        self, approval: ApprovalRef, *, expected_project_version: int
    ) -> int:
        def insert(connection: sqlite3.Connection) -> None:
            self._insert_approval(connection, approval)

        return self._project_write(
            approval.project_id,
            expected_project_version,
            approval.approved_at,
            insert,
        )

    def _insert_approval(
        self, connection: sqlite3.Connection, approval: ApprovalRef
    ) -> None:
        _safe_id(approval.approval_id, "approval_id")
        if approval.subject_kind is ApprovalSubjectKind.ENGINEERING_SPEC:
            row = connection.execute(
                """
                SELECT * FROM approved_specs
                WHERE project_id = ? AND spec_id = ? AND spec_version = ?
                """,
                (
                    approval.project_id,
                    approval.subject_id,
                    approval.subject_version,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    SELECT * FROM specs
                    WHERE project_id = ? AND spec_id = ? AND spec_version = ?
                    """,
                    (
                        approval.project_id,
                        approval.subject_id,
                        approval.subject_version,
                    ),
                ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT * FROM revisions
                WHERE project_id = ? AND revision_id = ?
                    AND revision_number = ?
                """,
                (
                    approval.project_id,
                    approval.subject_id,
                    approval.subject_version,
                ),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("approval subject not found")
        subject_hash = (
            self._spec_from_row(row).spec_hash
            if approval.subject_kind is ApprovalSubjectKind.ENGINEERING_SPEC
            else self._revision_from_row(row).revision_hash
        )
        if subject_hash != approval.subject_hash:
            raise IntegrityConflictError("approval subject hash is stale")
        self._insert_immutable(
            connection,
            "INSERT INTO approvals VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                approval.approval_id,
                approval.project_id,
                approval.subject_kind.value,
                approval.subject_id,
                approval.subject_version,
                approval.subject_hash,
                _canonical_model(approval),
            ),
        )

    def get_approval(self, approval_id: str) -> ApprovalRef:
        with self._reading() as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("record not found")
        return self._approval_from_row(row)

    def get_subject_approval(
        self,
        project_id: str,
        subject_kind: ApprovalSubjectKind,
        subject_id: str,
        subject_version: int,
    ) -> ApprovalRef:
        with self._reading() as connection:
            row = connection.execute(
                """
                SELECT * FROM approvals
                WHERE project_id = ? AND subject_kind = ?
                  AND subject_id = ? AND subject_version = ?
                """,
                (project_id, subject_kind.value, subject_id, subject_version),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("approval not found")
        return self._approval_from_row(row)

    def store_preparation(
        self, record: StoredPreparation, *, expected_project_version: int
    ) -> int:
        def insert(connection: sqlite3.Connection) -> None:
            self._insert_preparation(connection, record)

        return self._project_write(
            record.project_id, expected_project_version, record.stored_at, insert
        )

    def _insert_preparation(
        self, connection: sqlite3.Connection, record: StoredPreparation
    ) -> None:
        preparation = record.preparation
        _safe_id(preparation.preparation_id, "preparation_id")
        binding = preparation.binding
        spec = connection.execute(
            """
            SELECT * FROM approved_specs
            WHERE project_id = ? AND spec_id = ? AND spec_version = ?
            """,
            (record.project_id, binding.spec_id, binding.spec_version),
        ).fetchone()
        if spec is None:
            spec = connection.execute(
                """
            SELECT * FROM specs
            WHERE project_id = ? AND spec_id = ? AND spec_version = ?
            """,
                (record.project_id, binding.spec_id, binding.spec_version),
            ).fetchone()
        if spec is None:
            raise RecordNotFoundError("prepared spec not found")
        if self._spec_from_row(spec).spec_hash != binding.spec_hash:
            raise IntegrityConflictError("prepared spec hash is stale")
        if binding.revision_id is not None:
            revision = connection.execute(
                """
                SELECT * FROM revisions
                WHERE project_id = ? AND revision_id = ?
                """,
                (record.project_id, binding.revision_id),
            ).fetchone()
            if revision is None:
                raise RecordNotFoundError("prepared revision not found")
            stored_revision = self._revision_from_row(revision)
            if (
                stored_revision.revision_hash != binding.revision_hash
                or stored_revision.revision_number != binding.revision_number
            ):
                raise IntegrityConflictError("prepared revision binding is stale")
        for approval in binding.approval_refs:
            stored = connection.execute(
                "SELECT * FROM approvals WHERE approval_id = ?",
                (approval.approval_id,),
            ).fetchone()
            if stored is None:
                raise RecordNotFoundError("prepared approval not found")
            if self._approval_from_row(stored) != approval:
                raise IntegrityConflictError("prepared approval was tampered")
        self._insert_immutable(
            connection,
            "INSERT INTO preparations VALUES(?, ?, ?, ?, ?)",
            (
                preparation.preparation_id,
                record.project_id,
                preparation.prepare_hash,
                _canonical_model(record),
                _utc_text(record.stored_at),
            ),
        )

    def get_preparation(self, preparation_id: str) -> StoredPreparation:
        with self._reading() as connection:
            row = connection.execute(
                "SELECT * FROM preparations WHERE preparation_id = ?",
                (preparation_id,),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("record not found")
        return self._preparation_from_row(row)

    def append_run_event(
        self, event: RunStateEvent, *, expected_project_version: int
    ) -> int:
        if event.status in {
            RunLifecycleStatus.SUCCEEDED,
            RunLifecycleStatus.FAILED,
        }:
            raise IntegrityConflictError("terminal result events require complete_run")

        def insert(connection: sqlite3.Connection) -> None:
            self._append_event(connection, event)

        return self._project_write(
            event.project_id, expected_project_version, event.occurred_at, insert
        )

    def _append_event(
        self, connection: sqlite3.Connection, event: RunStateEvent
    ) -> None:
        if event.status in {
            RunLifecycleStatus.SUCCEEDED,
            RunLifecycleStatus.FAILED,
        }:
            raise IntegrityConflictError("terminal result events require complete_run")
        self._validate_event_append(connection, event)
        self._insert_event(connection, event)

    def complete_run(
        self,
        event: RunStateEvent,
        result: AnalysisRunRecord,
        verification: VerificationBundle | None,
        *,
        expected_project_version: int,
    ) -> int:
        def insert(connection: sqlite3.Connection) -> None:
            self._complete_run(connection, event, result, verification)

        return self._project_write(
            event.project_id,
            expected_project_version,
            event.occurred_at,
            insert,
        )

    def _complete_run(
        self,
        connection: sqlite3.Connection,
        event: RunStateEvent,
        result: AnalysisRunRecord,
        verification: VerificationBundle | None,
    ) -> None:
        if event.status not in {
            RunLifecycleStatus.SUCCEEDED,
            RunLifecycleStatus.FAILED,
        }:
            raise IntegrityConflictError("complete_run requires a result event")
        if event.run_id != result.run_id:
            raise IntegrityConflictError("terminal event and result run IDs differ")
        if event.run_record_hash != canonical_sha256(result):
            raise IntegrityConflictError("terminal event hash does not match result")
        prior = self._load_events(connection, event.run_id)
        self._validate_event_append(connection, event)
        self._validate(
            StoredRun,
            {
                "project_id": event.project_id,
                "run_id": event.run_id,
                "preparation_id": event.preparation_id,
                "events": (*prior, event),
                "result": result,
                "verification": verification,
            },
        )
        self._insert_event(connection, event)
        self._insert_immutable(
            connection,
            "INSERT INTO run_results VALUES(?, ?, ?, ?, ?)",
            (
                result.run_id,
                event.project_id,
                _canonical_model(result),
                None if verification is None else _canonical_model(verification),
                _utc_text(event.occurred_at),
            ),
        )

    def _validate_event_append(
        self, connection: sqlite3.Connection, event: RunStateEvent
    ) -> None:
        _safe_id(event.event_id, "event_id")
        _safe_id(event.run_id, "run_id")
        preparation = connection.execute(
            "SELECT * FROM preparations WHERE preparation_id = ?",
            (event.preparation_id,),
        ).fetchone()
        if preparation is None:
            raise RecordNotFoundError("run preparation not found")
        stored_preparation = self._preparation_from_row(preparation)
        if (
            stored_preparation.project_id != event.project_id
            or stored_preparation.preparation.prepare_hash != event.prepare_hash
        ):
            raise IntegrityConflictError("run preparation binding is stale")
        latest = connection.execute(
            """
            SELECT *
            FROM run_events WHERE run_id = ? ORDER BY sequence DESC LIMIT 1
            """,
            (event.run_id,),
        ).fetchone()
        if latest is None:
            if event.sequence != 1:
                raise IntegrityConflictError("run events must start at sequence 1")
            return
        latest_event = self._event_from_row(latest)
        if event.previous_status is None or (
            event.sequence != latest_event.sequence + 1
            or event.previous_status is not latest_event.status
            or event.preparation_id != latest_event.preparation_id
            or event.prepare_hash != latest_event.prepare_hash
            or event.occurred_at < latest_event.occurred_at
        ):
            raise IntegrityConflictError(
                "run events must be append-only and contiguous"
            )

    @staticmethod
    def _insert_event(connection: sqlite3.Connection, event: RunStateEvent) -> None:
        SQLiteEvidenceStore._insert_immutable(
            connection,
            "INSERT INTO run_events VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.project_id,
                event.run_id,
                event.preparation_id,
                event.prepare_hash,
                event.sequence,
                event.status.value,
                _canonical_model(event),
                _utc_text(event.occurred_at),
            ),
        )

    def get_run(self, run_id: str) -> StoredRun:
        with self._reading() as connection:
            return self._load_run(connection, run_id)

    def _load_run(self, connection: sqlite3.Connection, run_id: str) -> StoredRun:
        events = self._load_events(connection, run_id)
        if not events:
            raise RecordNotFoundError("run not found")
        row = connection.execute(
            "SELECT * FROM run_results WHERE run_id = ?", (run_id,)
        ).fetchone()
        result = None
        verification = None
        if row is not None:
            result = self._decode(AnalysisRunRecord, row["result_json"])
            if row["verification_json"] is not None:
                verification = self._decode(
                    VerificationBundle, row["verification_json"]
                )
            self._require_binding(
                row["run_id"] == result.run_id == run_id
                and row["project_id"] == events[0].project_id
                and row["stored_at"] == _utc_text(events[-1].occurred_at),
                "run result row does not match its payload",
            )
        return self._validate(
            StoredRun,
            {
                "project_id": events[0].project_id,
                "run_id": run_id,
                "preparation_id": events[0].preparation_id,
                "events": events,
                "result": result,
                "verification": verification,
            },
        )

    def _load_events(
        self, connection: sqlite3.Connection, run_id: str
    ) -> tuple[RunStateEvent, ...]:
        rows = connection.execute(
            """
            SELECT * FROM run_events
            WHERE run_id = ? ORDER BY sequence
            """,
            (run_id,),
        ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    def store_evidence(
        self,
        project_id: str,
        evidence: EvidenceRecord,
        *,
        expected_project_version: int,
    ) -> int:
        def insert(connection: sqlite3.Connection) -> None:
            self._insert_evidence(connection, project_id, evidence)

        return self._project_write(
            project_id,
            expected_project_version,
            evidence.recorded_at,
            insert,
        )

    def _insert_evidence(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        evidence: EvidenceRecord,
    ) -> None:
        _safe_id(evidence.evidence_id, "evidence_id")
        revision = connection.execute(
            """
            SELECT * FROM revisions
            WHERE project_id = ? AND revision_id = ?
            """,
            (project_id, evidence.produced_for_revision_id),
        ).fetchone()
        if revision is None:
            raise RecordNotFoundError("evidence revision not found")
        stored_revision = self._revision_from_row(revision)
        expected_dependency_hash = dependency_hash(
            stored_revision.revision, evidence.evidence_class
        )
        if evidence.dependency_hash != expected_dependency_hash:
            raise IntegrityConflictError(
                "evidence dependency hash must match its stored revision"
            )
        self._insert_immutable(
            connection,
            "INSERT INTO evidence_records VALUES(?, ?, ?, ?, ?)",
            (
                evidence.evidence_id,
                project_id,
                evidence.produced_for_revision_id,
                _canonical_model(evidence),
                _utc_text(evidence.recorded_at),
            ),
        )

    def list_evidence(self, project_id: str) -> tuple[EvidenceRecord, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT * FROM evidence_records
                WHERE project_id = ? ORDER BY evidence_id
                """,
                (project_id,),
            ).fetchall()
        return tuple(self._evidence_from_row(row, project_id) for row in rows)

    def store_cost_evaluation(
        self, record: StoredCostEvaluation, *, expected_project_version: int
    ) -> int:
        def insert(connection: sqlite3.Connection) -> None:
            self._insert_cost_evaluation(connection, record)

        return self._project_write(
            record.project_id, expected_project_version, record.evaluated_at, insert
        )

    def _insert_cost_evaluation(
        self, connection: sqlite3.Connection, record: StoredCostEvaluation
    ) -> None:
        _safe_id(record.evidence_id, "evidence_id")
        evidence = connection.execute(
            "SELECT * FROM evidence_records WHERE evidence_id = ?",
            (record.evidence_id,),
        ).fetchone()
        if evidence is None:
            raise RecordNotFoundError("cost evidence record not found")
        stored_evidence = self._evidence_from_row(evidence, record.project_id)
        revision_row = connection.execute(
            """
            SELECT * FROM revisions
            WHERE project_id = ? AND revision_id = ?
            """,
            (record.project_id, record.revision_id),
        ).fetchone()
        if revision_row is None:
            raise RecordNotFoundError("cost revision not found")
        stored_revision = self._revision_from_row(revision_row)
        revision_artifacts = stored_revision.revision.artifact_map()
        expected_dependency_hash = dependency_hash(
            stored_revision.revision, EvidenceClass.COST
        )
        expected_evidence_refs = {
            *(f"quote:{quote.quote_id}" for quote in record.quotes),
            *(f"source:{quote.source_hash}" for quote in record.quotes),
        }
        if (
            stored_evidence.evidence_class is not EvidenceClass.COST
            or stored_evidence.produced_for_revision_id != record.revision_id
            or stored_evidence.dependency_hash != expected_dependency_hash
            or stored_evidence.dependency_hash != record.dependency_hash
            or stored_evidence.verdict is not record.evaluation.verdict
            or set(stored_evidence.evidence_refs) != expected_evidence_refs
            or record.bom_artifact_hash
            != revision_artifacts[ArtifactKind.BOM].content_hash
            or stored_evidence.recorded_at != record.evaluated_at
        ):
            raise IntegrityConflictError(
                "cost provenance must bind its exact COST evidence record"
            )
        self._insert_immutable(
            connection,
            "INSERT INTO cost_evaluations VALUES(?, ?, ?, ?, ?)",
            (
                record.evidence_id,
                record.project_id,
                record.revision_id,
                _canonical_model(record),
                _utc_text(record.evaluated_at),
            ),
        )

    def get_cost_evaluation(self, evidence_id: str) -> StoredCostEvaluation:
        with self._reading() as connection:
            row = connection.execute(
                "SELECT * FROM cost_evaluations WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("cost evaluation not found")
        return self._cost_evaluation_from_row(row)

    def list_cost_evaluations(
        self, project_id: str
    ) -> tuple[StoredCostEvaluation, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT * FROM cost_evaluations
                WHERE project_id = ? ORDER BY evaluated_at, evidence_id
                """,
                (project_id,),
            ).fetchall()
        return tuple(self._cost_evaluation_from_row(row) for row in rows)

    def store_release_policy(
        self, record: StoredReleasePolicy, *, expected_project_version: int
    ) -> int:
        return self._project_write(
            record.project_id,
            expected_project_version,
            record.stored_at,
            lambda connection: self._insert_release_policy(connection, record),
        )

    def _insert_release_policy(
        self, connection: sqlite3.Connection, record: StoredReleasePolicy
    ) -> None:
        record = StoredReleasePolicy.model_validate(record.model_dump(mode="python"))
        self._insert_immutable(
            connection,
            "INSERT INTO release_policies VALUES(?, ?, ?, ?)",
            (
                record.project_id,
                record.policy_hash,
                _canonical_model(record),
                _utc_text(record.stored_at),
            ),
        )

    def get_release_policy(self, project_id: str) -> StoredReleasePolicy:
        with self._reading() as connection:
            row = connection.execute(
                "SELECT * FROM release_policies WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("release policy not found")
        return self._release_policy_from_row(row)

    def store_connector_snapshot(
        self, record: StoredConnectorSnapshot, *, expected_project_version: int
    ) -> int:
        return self._project_write(
            record.project_id,
            expected_project_version,
            record.stored_at,
            lambda connection: self._insert_connector_snapshot(connection, record),
        )

    def _insert_connector_snapshot(
        self, connection: sqlite3.Connection, record: StoredConnectorSnapshot
    ) -> None:
        _safe_id(record.snapshot_id, "snapshot_id")
        self._insert_immutable(
            connection,
            "INSERT INTO connector_snapshots VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                record.project_id,
                record.snapshot_id,
                record.snapshot_hash,
                record.snapshot.hardware_revision_id,
                _canonical_model(record),
                _utc_text(record.snapshot.captured_at),
                _utc_text(record.stored_at),
            ),
        )

    def get_connector_snapshot(
        self, project_id: str, snapshot_id: str
    ) -> StoredConnectorSnapshot:
        with self._reading() as connection:
            row = connection.execute(
                """
                SELECT * FROM connector_snapshots
                WHERE project_id = ? AND snapshot_id = ?
                """,
                (project_id, snapshot_id),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("connector snapshot not found")
        return self._connector_snapshot_from_row(row)

    def list_connector_snapshots(
        self, project_id: str
    ) -> tuple[StoredConnectorSnapshot, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT * FROM connector_snapshots
                WHERE project_id = ? ORDER BY captured_at, snapshot_id
                """,
                (project_id,),
            ).fetchall()
        return tuple(self._connector_snapshot_from_row(row) for row in rows)

    def store_change_assessment(
        self, record: StoredChangeImpactAssessment, *, expected_project_version: int
    ) -> int:
        return self._project_write(
            record.project_id,
            expected_project_version,
            record.stored_at,
            lambda connection: self._insert_change_assessment(connection, record),
        )

    def _insert_change_assessment(
        self, connection: sqlite3.Connection, record: StoredChangeImpactAssessment
    ) -> None:
        previous_row = connection.execute(
            """
            SELECT * FROM connector_snapshots
            WHERE project_id = ? AND snapshot_id = ?
            """,
            (record.project_id, record.from_snapshot_id),
        ).fetchone()
        current_row = connection.execute(
            """
            SELECT * FROM connector_snapshots
            WHERE project_id = ? AND snapshot_id = ?
            """,
            (record.project_id, record.to_snapshot_id),
        ).fetchone()
        if previous_row is None or current_row is None:
            raise RecordNotFoundError("change assessment snapshot not found")
        previous = self._connector_snapshot_from_row(previous_row)
        current = self._connector_snapshot_from_row(current_row)
        assessment = record.assessment
        projection_hashes = tuple(
            sorted(
                item.projection_hash
                for item in previous.interface_projections
                + current.interface_projections
            )
        )
        target_protocol_hashes = {
            item.contract.protocol_schema_hash
            for item in current.interface_projections
            if item.source_ref.domain is ArtifactDomain.PROTOCOL
        }
        expected_protocol_hash = next(iter(target_protocol_hashes), None)
        if (
            assessment.from_snapshot_hash != previous.snapshot_hash
            or assessment.to_snapshot_hash != current.snapshot_hash
            or assessment.from_hardware_revision_id
            != previous.snapshot.hardware_revision_id
            or assessment.to_hardware_revision_id
            != current.snapshot.hardware_revision_id
            or assessment.interface_projection_hashes != projection_hashes
            or assessment.target_protocol_schema_hash != expected_protocol_hash
        ):
            raise IntegrityConflictError(
                "change assessment does not bind its stored connector snapshots"
            )
        self._insert_immutable(
            connection,
            "INSERT INTO change_assessments VALUES(?, ?, ?, ?, ?, ?)",
            (
                record.analysis_hash,
                record.project_id,
                record.from_snapshot_id,
                record.to_snapshot_id,
                _canonical_model(record),
                _utc_text(record.stored_at),
            ),
        )

    def get_change_assessment(self, analysis_hash: str) -> StoredChangeImpactAssessment:
        with self._reading() as connection:
            row = connection.execute(
                "SELECT * FROM change_assessments WHERE analysis_hash = ?",
                (analysis_hash,),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("change assessment not found")
        return self._change_assessment_from_row(row)

    def list_change_assessments(
        self, project_id: str
    ) -> tuple[StoredChangeImpactAssessment, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT * FROM change_assessments
                WHERE project_id = ? ORDER BY stored_at, analysis_hash
                """,
                (project_id,),
            ).fetchall()
        return tuple(self._change_assessment_from_row(row) for row in rows)

    def store_change_preview(
        self, record: StoredChangeImpactPreview, *, expected_project_version: int
    ) -> int:
        return self._project_write(
            record.project_id,
            expected_project_version,
            record.stored_at,
            lambda connection: self._insert_change_preview(connection, record),
        )

    def _insert_change_preview(
        self, connection: sqlite3.Connection, record: StoredChangeImpactPreview
    ) -> None:
        record = StoredChangeImpactPreview.model_validate(
            record.model_dump(mode="python")
        )
        baseline_row = connection.execute(
            """
            SELECT * FROM connector_snapshots
            WHERE project_id = ? AND snapshot_id = ?
            """,
            (record.project_id, record.baseline_snapshot_id),
        ).fetchone()
        if baseline_row is None:
            raise RecordNotFoundError("change preview baseline snapshot not found")
        baseline = self._connector_snapshot_from_row(baseline_row)
        if baseline.snapshot_hash != record.baseline_snapshot_hash:
            raise IntegrityConflictError(
                "change preview does not bind its stored baseline snapshot"
            )
        self._insert_immutable(
            connection,
            "INSERT INTO change_previews VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.preview_hash,
                record.project_id,
                record.scenario_id,
                record.scenario_hash,
                record.baseline_snapshot_id,
                record.baseline_snapshot_hash,
                _canonical_model(record),
                _utc_text(record.preview.generated_at),
                _utc_text(record.stored_at),
            ),
        )

    def get_change_preview(self, preview_hash: str) -> StoredChangeImpactPreview:
        with self._reading() as connection:
            row = connection.execute(
                "SELECT * FROM change_previews WHERE preview_hash = ?",
                (preview_hash,),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("change preview not found")
        return self._change_preview_from_row(row)

    def list_change_previews(
        self, project_id: str
    ) -> tuple[StoredChangeImpactPreview, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT * FROM change_previews
                WHERE project_id = ? ORDER BY generated_at, preview_hash
                """,
                (project_id,),
            ).fetchall()
        return tuple(self._change_preview_from_row(row) for row in rows)

    def store_external_evidence_plan(
        self, record: StoredExternalEvidencePlan, *, expected_project_version: int
    ) -> int:
        return self._project_write(
            record.project_id,
            expected_project_version,
            record.stored_at,
            lambda connection: self._insert_external_evidence_plan(connection, record),
        )

    def _insert_external_evidence_plan(
        self, connection: sqlite3.Connection, record: StoredExternalEvidencePlan
    ) -> None:
        record = StoredExternalEvidencePlan.model_validate(
            record.model_dump(mode="python")
        )
        baseline_row = connection.execute(
            """
            SELECT * FROM connector_snapshots
            WHERE project_id = ? AND snapshot_id = ?
            """,
            (record.project_id, record.baseline_snapshot_id),
        ).fetchone()
        if baseline_row is None:
            raise RecordNotFoundError(
                "external evidence plan baseline snapshot not found"
            )
        baseline = self._connector_snapshot_from_row(baseline_row)
        if baseline.snapshot_hash != record.baseline_snapshot_hash:
            raise IntegrityConflictError(
                "external evidence plan does not bind its stored baseline snapshot"
            )
        self._insert_immutable(
            connection,
            "INSERT INTO external_evidence_plans VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.plan_hash,
                record.project_id,
                record.scenario_id,
                record.scenario_hash,
                record.baseline_snapshot_id,
                record.baseline_snapshot_hash,
                _canonical_model(record),
                _utc_text(record.plan.generated_at),
                _utc_text(record.stored_at),
            ),
        )

    def get_external_evidence_plan(self, plan_hash: str) -> StoredExternalEvidencePlan:
        with self._reading() as connection:
            row = connection.execute(
                "SELECT * FROM external_evidence_plans WHERE plan_hash = ?",
                (plan_hash,),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("external evidence plan not found")
        return self._external_evidence_plan_from_row(row)

    def list_external_evidence_plans(
        self, project_id: str
    ) -> tuple[StoredExternalEvidencePlan, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT * FROM external_evidence_plans
                WHERE project_id = ? ORDER BY generated_at, plan_hash
                """,
                (project_id,),
            ).fetchall()
        return tuple(self._external_evidence_plan_from_row(row) for row in rows)

    def store_external_evidence_plan_verification(
        self,
        record: StoredExternalEvidencePlanVerification,
        *,
        expected_project_version: int,
    ) -> int:
        return self._project_write(
            record.project_id,
            expected_project_version,
            record.stored_at,
            lambda connection: self._insert_external_evidence_plan_verification(
                connection, record
            ),
        )

    def _insert_external_evidence_plan_verification(
        self,
        connection: sqlite3.Connection,
        record: StoredExternalEvidencePlanVerification,
    ) -> None:
        record = StoredExternalEvidencePlanVerification.model_validate(
            record.model_dump(mode="python")
        )
        plan_row = connection.execute(
            "SELECT * FROM external_evidence_plans WHERE plan_hash = ?",
            (record.plan_hash,),
        ).fetchone()
        assessment_row = connection.execute(
            "SELECT * FROM change_assessments WHERE analysis_hash = ?",
            (record.actual_change_analysis_hash,),
        ).fetchone()
        if plan_row is None:
            raise RecordNotFoundError("external evidence verification plan not found")
        if assessment_row is None:
            raise RecordNotFoundError(
                "external evidence verification assessment not found"
            )
        plan = self._external_evidence_plan_from_row(plan_row)
        assessment = self._change_assessment_from_row(assessment_row)
        if (
            plan.project_id != record.project_id
            or assessment.project_id != record.project_id
        ):
            raise IntegrityConflictError(
                "external evidence verification inputs belong to another project"
            )
        if (
            plan.baseline_snapshot_id != assessment.assessment.from_snapshot_id
            or plan.baseline_snapshot_hash != assessment.assessment.from_snapshot_hash
        ):
            raise IntegrityConflictError(
                "external evidence plan baseline does not match actual change analysis"
            )
        for binding in record.verification.imported_evidence:
            raw_row = connection.execute(
                """
                SELECT * FROM release_evidence
                WHERE project_id = ? AND evidence_id = ?
                """,
                (record.project_id, binding.evidence.evidence_id),
            ).fetchone()
            if raw_row is None:
                raise RecordNotFoundError(
                    "external evidence verification import not found"
                )
            raw = self._release_evidence_from_row(raw_row)
            if (
                raw.evidence_hash != binding.evidence_hash
                or raw.evidence != binding.evidence
                or raw.change_analysis_hash != record.actual_change_analysis_hash
            ):
                raise IntegrityConflictError(
                    "external evidence verification does not bind stored evidence"
                )
        self._insert_immutable(
            connection,
            """
            INSERT INTO external_evidence_plan_verifications
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.verification_hash,
                record.project_id,
                record.plan_hash,
                record.actual_change_analysis_hash,
                _canonical_model(record),
                _utc_text(record.verification.verified_at),
                _utc_text(record.stored_at),
            ),
        )

    def get_external_evidence_plan_verification(
        self, verification_hash: str
    ) -> StoredExternalEvidencePlanVerification:
        with self._reading() as connection:
            row = connection.execute(
                """
                SELECT * FROM external_evidence_plan_verifications
                WHERE verification_hash = ?
                """,
                (verification_hash,),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("external evidence plan verification not found")
        return self._external_evidence_plan_verification_from_row(row)

    def list_external_evidence_plan_verifications(
        self, project_id: str
    ) -> tuple[StoredExternalEvidencePlanVerification, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT * FROM external_evidence_plan_verifications
                WHERE project_id = ? ORDER BY verified_at, verification_hash
                """,
                (project_id,),
            ).fetchall()
        return tuple(
            self._external_evidence_plan_verification_from_row(row) for row in rows
        )

    def store_plan_verification(
        self, record: StoredPlanVerification, *, expected_project_version: int
    ) -> int:
        return self._project_write(
            record.project_id,
            expected_project_version,
            record.stored_at,
            lambda connection: self._insert_plan_verification(connection, record),
        )

    def _insert_plan_verification(
        self, connection: sqlite3.Connection, record: StoredPlanVerification
    ) -> None:
        record = StoredPlanVerification.model_validate(record.model_dump(mode="python"))
        preview_row = connection.execute(
            "SELECT * FROM change_previews WHERE preview_hash = ?",
            (record.preview_hash,),
        ).fetchone()
        assessment_row = connection.execute(
            "SELECT * FROM change_assessments WHERE analysis_hash = ?",
            (record.actual_change_analysis_hash,),
        ).fetchone()
        if preview_row is None:
            raise RecordNotFoundError("plan verification preview not found")
        if assessment_row is None:
            raise RecordNotFoundError("plan verification assessment not found")
        preview = self._change_preview_from_row(preview_row)
        assessment = self._change_assessment_from_row(assessment_row)
        if (
            preview.project_id != record.project_id
            or preview.scenario_id != record.scenario_id
            or assessment.project_id != record.project_id
        ):
            raise IntegrityConflictError(
                "plan verification does not bind stored preview and assessment"
            )
        self._insert_immutable(
            connection,
            "INSERT INTO plan_verifications VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.verification_hash,
                record.project_id,
                record.scenario_id,
                record.preview_hash,
                record.actual_change_analysis_hash,
                _canonical_model(record),
                _utc_text(record.verification.verified_at),
                _utc_text(record.stored_at),
            ),
        )

    def get_plan_verification(self, verification_hash: str) -> StoredPlanVerification:
        with self._reading() as connection:
            row = connection.execute(
                "SELECT * FROM plan_verifications WHERE verification_hash = ?",
                (verification_hash,),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("plan verification not found")
        return self._plan_verification_from_row(row)

    def list_plan_verifications(
        self, project_id: str
    ) -> tuple[StoredPlanVerification, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT * FROM plan_verifications
                WHERE project_id = ? ORDER BY verified_at, verification_hash
                """,
                (project_id,),
            ).fetchall()
        return tuple(self._plan_verification_from_row(row) for row in rows)

    def store_design_proposal(
        self, record: StoredDesignProposalSet, *, expected_project_version: int
    ) -> int:
        return self._project_write(
            record.project_id,
            expected_project_version,
            record.stored_at,
            lambda connection: self._insert_design_proposal(connection, record),
        )

    def _insert_design_proposal(
        self, connection: sqlite3.Connection, record: StoredDesignProposalSet
    ) -> None:
        record = StoredDesignProposalSet.model_validate(
            record.model_dump(mode="python")
        )
        preview_row = connection.execute(
            "SELECT * FROM change_previews WHERE preview_hash = ?",
            (record.preview_hash,),
        ).fetchone()
        if preview_row is None:
            raise RecordNotFoundError("design proposal preview not found")
        preview = self._change_preview_from_row(preview_row)
        if (
            preview.project_id != record.project_id
            or preview.preview_hash != record.preview_hash
            or preview.baseline_snapshot_id != record.proposal.baseline_snapshot_id
            or preview.baseline_snapshot_hash != record.proposal.baseline_snapshot_hash
        ):
            raise IntegrityConflictError(
                "design proposal does not bind its stored preview"
            )
        self._insert_immutable(
            connection,
            "INSERT INTO design_proposals VALUES(?, ?, ?, ?, ?)",
            (
                record.proposal_hash,
                record.project_id,
                record.preview_hash,
                _canonical_model(record),
                _utc_text(record.stored_at),
            ),
        )

    def get_design_proposal(self, proposal_hash: str) -> StoredDesignProposalSet:
        with self._reading() as connection:
            row = connection.execute(
                "SELECT * FROM design_proposals WHERE proposal_hash = ?",
                (proposal_hash,),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("design proposal not found")
        return self._design_proposal_from_row(row)

    def list_design_proposals(
        self, project_id: str
    ) -> tuple[StoredDesignProposalSet, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT * FROM design_proposals
                WHERE project_id = ? ORDER BY stored_at, proposal_hash
                """,
                (project_id,),
            ).fetchall()
        return tuple(self._design_proposal_from_row(row) for row in rows)

    def store_design_candidate(
        self, record: StoredDesignCandidate, *, expected_project_version: int
    ) -> int:
        return self._project_write(
            record.project_id,
            expected_project_version,
            record.stored_at,
            lambda connection: self._insert_design_candidate(connection, record),
        )

    def _insert_design_candidate(
        self, connection: sqlite3.Connection, record: StoredDesignCandidate
    ) -> None:
        record = StoredDesignCandidate.model_validate(record.model_dump(mode="python"))
        proposal_row = connection.execute(
            "SELECT * FROM design_proposals WHERE proposal_hash = ?",
            (record.candidate.proposal_hash,),
        ).fetchone()
        if proposal_row is None:
            raise RecordNotFoundError("design candidate proposal not found")
        proposal = self._design_proposal_from_row(proposal_row)
        if (
            proposal.project_id != record.project_id
            or proposal.proposal_hash != record.candidate.proposal_hash
            or proposal.proposal.baseline_snapshot_hash
            != record.candidate.baseline_snapshot_hash
        ):
            raise IntegrityConflictError(
                "design candidate does not bind its stored proposal baseline"
            )
        self._insert_immutable(
            connection,
            "INSERT INTO design_candidates VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.candidate_hash,
                record.project_id,
                record.session_id,
                record.candidate.candidate_id,
                record.candidate.revision,
                record.candidate.proposal_hash,
                _canonical_model(record),
                _utc_text(record.candidate.confirmed_at),
                _utc_text(record.stored_at),
            ),
        )

    def get_design_candidate(self, candidate_hash: str) -> StoredDesignCandidate:
        with self._reading() as connection:
            row = connection.execute(
                "SELECT * FROM design_candidates WHERE candidate_hash = ?",
                (candidate_hash,),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("design candidate not found")
        return self._design_candidate_from_row(row)

    def list_design_candidates(
        self, project_id: str
    ) -> tuple[StoredDesignCandidate, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT * FROM design_candidates
                WHERE project_id = ? ORDER BY confirmed_at, candidate_id, revision
                """,
                (project_id,),
            ).fetchall()
        return tuple(self._design_candidate_from_row(row) for row in rows)

    def store_simulation_binding(
        self, record: StoredSimulationBinding, *, expected_project_version: int
    ) -> int:
        return self._project_write(
            record.project_id,
            expected_project_version,
            record.stored_at,
            lambda connection: self._insert_simulation_binding(connection, record),
        )

    def _insert_simulation_binding(
        self, connection: sqlite3.Connection, record: StoredSimulationBinding
    ) -> None:
        record = StoredSimulationBinding.model_validate(
            record.model_dump(mode="python")
        )
        candidate_row = connection.execute(
            "SELECT * FROM design_candidates WHERE candidate_hash = ?",
            (record.candidate_hash,),
        ).fetchone()
        if candidate_row is None:
            raise RecordNotFoundError("simulation design candidate not found")
        candidate = self._design_candidate_from_row(candidate_row)
        if (
            candidate.project_id != record.project_id
            or candidate.session_id != record.session_id
        ):
            raise IntegrityConflictError(
                "simulation candidate belongs to another project or session"
            )
        self._insert_immutable(
            connection,
            "INSERT INTO simulation_bindings VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.simulation_hash,
                record.project_id,
                record.session_id,
                record.simulation.simulation_id,
                record.candidate_hash,
                _canonical_model(record),
                _utc_text(record.simulation.created_at),
                _utc_text(record.stored_at),
            ),
        )

    def get_simulation_binding(self, simulation_hash: str) -> StoredSimulationBinding:
        with self._reading() as connection:
            row = connection.execute(
                "SELECT * FROM simulation_bindings WHERE simulation_hash = ?",
                (simulation_hash,),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("simulation binding not found")
        return self._simulation_binding_from_row(row)

    def list_simulation_bindings(
        self, project_id: str
    ) -> tuple[StoredSimulationBinding, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT * FROM simulation_bindings
                WHERE project_id = ? ORDER BY created_at, simulation_id
                """,
                (project_id,),
            ).fetchall()
        return tuple(self._simulation_binding_from_row(row) for row in rows)

    def store_evidence_claim(
        self, record: StoredEvidenceClaim, *, expected_project_version: int
    ) -> int:
        return self._project_write(
            record.project_id,
            expected_project_version,
            record.stored_at,
            lambda connection: self._insert_evidence_claim(connection, record),
        )

    def _insert_evidence_claim(
        self, connection: sqlite3.Connection, record: StoredEvidenceClaim
    ) -> None:
        record = StoredEvidenceClaim.model_validate(record.model_dump(mode="python"))
        self._insert_immutable(
            connection,
            "INSERT INTO conversational_evidence_claims VALUES(?, ?, ?, ?, ?, ?)",
            (
                record.claim_hash,
                record.project_id,
                record.session_id,
                record.claim.claim_id,
                _canonical_model(record),
                _utc_text(record.stored_at),
            ),
        )

    def get_evidence_claim(self, claim_hash: str) -> StoredEvidenceClaim:
        with self._reading() as connection:
            row = connection.execute(
                "SELECT * FROM conversational_evidence_claims WHERE claim_hash = ?",
                (claim_hash,),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("conversational evidence claim not found")
        return self._evidence_claim_from_row(row)

    def list_evidence_claims(self, project_id: str) -> tuple[StoredEvidenceClaim, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT * FROM conversational_evidence_claims
                WHERE project_id = ? ORDER BY stored_at, claim_id
                """,
                (project_id,),
            ).fetchall()
        return tuple(self._evidence_claim_from_row(row) for row in rows)

    def append_design_state_transition(
        self,
        record: StoredDesignStateTransition,
        *,
        expected_project_version: int,
    ) -> int:
        return self._project_write(
            record.project_id,
            expected_project_version,
            record.stored_at,
            lambda connection: self._append_design_state_transition(connection, record),
        )

    def _append_design_state_transition(
        self, connection: sqlite3.Connection, record: StoredDesignStateTransition
    ) -> None:
        record = StoredDesignStateTransition.model_validate(
            record.model_dump(mode="python")
        )
        latest_row = connection.execute(
            """
            SELECT * FROM design_state_transitions
            WHERE project_id = ? AND session_id = ?
            ORDER BY sequence DESC LIMIT 1
            """,
            (record.project_id, record.session_id),
        ).fetchone()
        if latest_row is None:
            if record.sequence != 1 or record.transition.from_state.value != "idea":
                raise IntegrityConflictError(
                    "first design transition must begin at IDEA with sequence 1"
                )
        else:
            latest = self._design_state_transition_from_row(latest_row)
            if (
                record.sequence != latest.sequence + 1
                or record.transition.from_state is not latest.transition.to_state
                or record.transition.occurred_at < latest.transition.occurred_at
            ):
                raise IntegrityConflictError(
                    "design transition history must be contiguous and monotonic"
                )
        if record.transition.candidate_hash is not None:
            row = connection.execute(
                """
                SELECT * FROM design_candidates
                WHERE project_id = ? AND candidate_hash = ?
                """,
                (record.project_id, record.transition.candidate_hash),
            ).fetchone()
            if row is None:
                raise RecordNotFoundError("design transition candidate not found")
            candidate = self._design_candidate_from_row(row)
            if candidate.session_id != record.session_id:
                raise IntegrityConflictError(
                    "design transition candidate belongs to another session"
                )
        if record.transition.simulation_hash is not None:
            row = connection.execute(
                """
                SELECT * FROM simulation_bindings
                WHERE project_id = ? AND simulation_hash = ?
                """,
                (record.project_id, record.transition.simulation_hash),
            ).fetchone()
            if row is None:
                raise RecordNotFoundError("design transition simulation not found")
            simulation = self._simulation_binding_from_row(row)
            if simulation.session_id != record.session_id:
                raise IntegrityConflictError(
                    "design transition simulation belongs to another session"
                )
            if (
                record.transition.candidate_hash is not None
                and simulation.candidate_hash != record.transition.candidate_hash
            ):
                raise IntegrityConflictError(
                    "design transition simulation must bind the selected candidate"
                )
        if record.transition.to_state.value == "verified":
            for evidence_ref in record.transition.evidence_refs:
                ref_kind, separator, evidence_id = evidence_ref.partition(":")
                if ref_kind != "release-evidence" or not separator or not evidence_id:
                    raise IntegrityConflictError(
                        "verified transition evidence reference is unsupported"
                    )
                evidence_row = connection.execute(
                    """
                    SELECT evidence_id FROM release_evidence
                    WHERE project_id = ? AND evidence_id = ?
                    """,
                    (record.project_id, evidence_id),
                ).fetchone()
                if evidence_row is None:
                    raise RecordNotFoundError(
                        "verified transition release evidence not found"
                    )
        self._insert_immutable(
            connection,
            "INSERT INTO design_state_transitions VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.project_id,
                record.session_id,
                record.sequence,
                record.transition.from_state.value,
                record.transition.to_state.value,
                record.transition.candidate_hash,
                record.transition.simulation_hash,
                _canonical_model(record),
                _utc_text(record.transition.occurred_at),
                _utc_text(record.stored_at),
            ),
        )

    def list_design_state_transitions(
        self, project_id: str, session_id: str | None = None
    ) -> tuple[StoredDesignStateTransition, ...]:
        query = (
            "SELECT * FROM design_state_transitions WHERE project_id = ? "
            "ORDER BY session_id, sequence"
        )
        parameters: tuple[object, ...] = (project_id,)
        if session_id is not None:
            query = (
                "SELECT * FROM design_state_transitions "
                "WHERE project_id = ? AND session_id = ? ORDER BY sequence"
            )
            parameters = (project_id, session_id)
        with self._reading() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(self._design_state_transition_from_row(row) for row in rows)

    def store_release_diagnosis(
        self, record: StoredReleaseDiagnosis, *, expected_project_version: int
    ) -> int:
        return self._project_write(
            record.project_id,
            expected_project_version,
            record.stored_at,
            lambda connection: self._insert_release_diagnosis(connection, record),
        )

    def _insert_release_diagnosis(
        self, connection: sqlite3.Connection, record: StoredReleaseDiagnosis
    ) -> None:
        record = StoredReleaseDiagnosis.model_validate(record.model_dump(mode="python"))
        decision_row = connection.execute(
            "SELECT * FROM release_decisions WHERE decision_hash = ?",
            (record.decision_hash,),
        ).fetchone()
        if decision_row is None:
            raise RecordNotFoundError("release diagnosis decision not found")
        decision = self._release_decision_from_row(decision_row)
        if (
            decision.project_id != record.project_id
            or decision.decision_hash != record.decision_hash
            or canonical_sha256(decision.decision)
            != canonical_sha256(record.diagnosis.decision)
        ):
            raise IntegrityConflictError(
                "release diagnosis does not bind its stored decision"
            )
        self._insert_immutable(
            connection,
            "INSERT INTO release_diagnoses VALUES(?, ?, ?, ?, ?)",
            (
                record.diagnosis_hash,
                record.project_id,
                record.decision_hash,
                _canonical_model(record),
                _utc_text(record.stored_at),
            ),
        )

    def get_release_diagnosis(self, diagnosis_hash: str) -> StoredReleaseDiagnosis:
        with self._reading() as connection:
            row = connection.execute(
                "SELECT * FROM release_diagnoses WHERE diagnosis_hash = ?",
                (diagnosis_hash,),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("release diagnosis not found")
        return self._release_diagnosis_from_row(row)

    def list_release_diagnoses(
        self, project_id: str
    ) -> tuple[StoredReleaseDiagnosis, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT * FROM release_diagnoses
                WHERE project_id = ? ORDER BY stored_at, diagnosis_hash
                """,
                (project_id,),
            ).fetchall()
        return tuple(self._release_diagnosis_from_row(row) for row in rows)

    def store_resolution_plan(
        self, record: StoredResolutionPlan, *, expected_project_version: int
    ) -> int:
        return self._project_write(
            record.project_id,
            expected_project_version,
            record.stored_at,
            lambda connection: self._insert_resolution_plan(connection, record),
        )

    def _insert_resolution_plan(
        self, connection: sqlite3.Connection, record: StoredResolutionPlan
    ) -> None:
        record = StoredResolutionPlan.model_validate(record.model_dump(mode="python"))
        diagnosis_row = connection.execute(
            "SELECT * FROM release_diagnoses WHERE diagnosis_hash = ?",
            (record.diagnosis_hash,),
        ).fetchone()
        if diagnosis_row is None:
            raise RecordNotFoundError("resolution plan diagnosis not found")
        diagnosis = self._release_diagnosis_from_row(diagnosis_row)
        matching = tuple(
            proposal
            for proposal_set in diagnosis.fix_proposal_sets
            for proposal in proposal_set.proposals
            if proposal.fix_hash == record.fix_proposal_hash
        )
        if (
            diagnosis.project_id != record.project_id
            or len(matching) != 1
            or canonical_sha256(matching[0])
            != canonical_sha256(record.selection.selected_fix)
        ):
            raise IntegrityConflictError(
                "resolution plan does not bind an exact stored fix proposal"
            )
        self._insert_immutable(
            connection,
            "INSERT INTO resolution_plans VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                record.plan_hash,
                record.project_id,
                record.diagnosis_hash,
                record.fix_proposal_hash,
                _canonical_model(record),
                _utc_text(record.selection.selected_at),
                _utc_text(record.stored_at),
            ),
        )

    def get_resolution_plan(self, plan_hash: str) -> StoredResolutionPlan:
        with self._reading() as connection:
            row = connection.execute(
                "SELECT * FROM resolution_plans WHERE plan_hash = ?",
                (plan_hash,),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("resolution plan not found")
        return self._resolution_plan_from_row(row)

    def list_resolution_plans(
        self, project_id: str
    ) -> tuple[StoredResolutionPlan, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT * FROM resolution_plans
                WHERE project_id = ? ORDER BY selected_at, plan_hash
                """,
                (project_id,),
            ).fetchall()
        return tuple(self._resolution_plan_from_row(row) for row in rows)

    def store_release_evidence(
        self, record: StoredRawReleaseEvidence, *, expected_project_version: int
    ) -> int:
        return self._project_write(
            record.project_id,
            expected_project_version,
            record.stored_at,
            lambda connection: self._insert_release_evidence(connection, record),
        )

    def _insert_release_evidence(
        self, connection: sqlite3.Connection, record: StoredRawReleaseEvidence
    ) -> None:
        record = StoredRawReleaseEvidence.model_validate(
            record.model_dump(mode="python")
        )
        _safe_id(record.evidence_id, "evidence_id")
        assessment_row = connection.execute(
            "SELECT * FROM change_assessments WHERE analysis_hash = ?",
            (record.change_analysis_hash,),
        ).fetchone()
        if assessment_row is None:
            raise RecordNotFoundError("release evidence assessment not found")
        assessment = self._change_assessment_from_row(assessment_row)
        snapshot_row = connection.execute(
            """
            SELECT * FROM connector_snapshots
            WHERE project_id = ? AND snapshot_id = ?
            """,
            (record.project_id, assessment.to_snapshot_id),
        ).fetchone()
        if snapshot_row is None:
            raise CorruptRecordError("release evidence target snapshot is missing")
        snapshot = self._connector_snapshot_from_row(snapshot_row)
        if (
            assessment.project_id != record.project_id
            or assessment.assessment.to_snapshot_hash != record.snapshot_hash
            or assessment.assessment.to_hardware_revision_id
            != record.hardware_revision_id
            or snapshot.snapshot_hash != record.snapshot_hash
        ):
            raise IntegrityConflictError(
                "release evidence does not bind its stored change assessment"
            )
        if isinstance(record.evidence, StoredCostEvaluation):
            policy_row = connection.execute(
                "SELECT * FROM release_policies WHERE project_id = ?",
                (record.project_id,),
            ).fetchone()
            if policy_row is None:
                raise CorruptRecordError("release cost policy is missing")
            policy = self._release_policy_from_row(policy_row).policy
            if not cost_evidence_matches_policy(record.evidence, policy):
                raise IntegrityConflictError(
                    "release cost evidence does not bind the stored budget policy"
                )
            target_bom_hashes = {
                item.content_hash
                for item in snapshot.snapshot.artifacts
                if item.domain is ArtifactDomain.BOM
            }
            if record.evidence.bom_artifact_hash not in target_bom_hashes:
                raise IntegrityConflictError(
                    "release cost evidence does not bind the target BOM"
                )
        occurred_at = (
            record.evidence.completed_at
            if isinstance(record.evidence, FirmwareBuildEvidence)
            else record.evidence.recorded_at
            if isinstance(record.evidence, TestExecutionEvidence)
            else record.evidence.evaluated_at
        )
        self._insert_immutable(
            connection,
            "INSERT INTO release_evidence VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.evidence_id,
                record.project_id,
                record.evidence_kind.value,
                record.change_analysis_hash,
                record.snapshot_hash,
                record.hardware_revision_id,
                record.evidence_hash,
                _canonical_model(record),
                _utc_text(occurred_at),
                _utc_text(record.stored_at),
            ),
        )

    def get_release_evidence(
        self, project_id: str, evidence_id: str
    ) -> StoredRawReleaseEvidence:
        with self._reading() as connection:
            row = connection.execute(
                """
                SELECT * FROM release_evidence
                WHERE project_id = ? AND evidence_id = ?
                """,
                (project_id, evidence_id),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("release evidence not found")
        return self._release_evidence_from_row(row)

    def list_release_evidence(
        self, project_id: str, *, analysis_hash: str | None = None
    ) -> tuple[StoredRawReleaseEvidence, ...]:
        with self._reading() as connection:
            if analysis_hash is None:
                rows = connection.execute(
                    """
                    SELECT * FROM release_evidence
                    WHERE project_id = ? ORDER BY occurred_at, evidence_id
                    """,
                    (project_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM release_evidence
                    WHERE project_id = ? AND change_analysis_hash = ?
                    ORDER BY occurred_at, evidence_id
                    """,
                    (project_id, analysis_hash),
                ).fetchall()
        return tuple(self._release_evidence_from_row(row) for row in rows)

    def append_release_decision(
        self, record: StoredReleaseDecision, *, expected_project_version: int
    ) -> int:
        return self._project_write(
            record.project_id,
            expected_project_version,
            record.stored_at,
            lambda connection: self._append_release_decision(connection, record),
        )

    def _append_release_decision(
        self, connection: sqlite3.Connection, record: StoredReleaseDecision
    ) -> None:
        record = StoredReleaseDecision.model_validate(record.model_dump(mode="python"))
        policy_row = connection.execute(
            "SELECT * FROM release_policies WHERE project_id = ?",
            (record.project_id,),
        ).fetchone()
        if policy_row is None:
            raise CorruptRecordError("release decision policy is missing")
        stored_policy = self._release_policy_from_row(policy_row)
        if record.decision.policy_hash != stored_policy.policy_hash:
            raise IntegrityConflictError(
                "release decision does not bind the stored release policy"
            )
        latest_row = connection.execute(
            """
            SELECT * FROM release_decisions
            WHERE project_id = ? ORDER BY sequence DESC LIMIT 1
            """,
            (record.project_id,),
        ).fetchone()
        if latest_row is None:
            if record.sequence != 1 or record.previous_decision_hash is not None:
                raise IntegrityConflictError(
                    "release decision history must start at sequence one"
                )
        else:
            latest = self._release_decision_from_row(latest_row)
            if (
                record.sequence != latest.sequence + 1
                or record.previous_decision_hash != latest.decision_hash
                or record.decision.report.evaluated_at
                < latest.decision.report.evaluated_at
            ):
                raise IntegrityConflictError(
                    "release decision history must be append-only and monotonic"
                )
        assessment_row = connection.execute(
            "SELECT * FROM change_assessments WHERE analysis_hash = ?",
            (record.decision.report.change_analysis_hash,),
        ).fetchone()
        if assessment_row is None:
            raise RecordNotFoundError("release decision assessment not found")
        assessment = self._change_assessment_from_row(assessment_row)
        snapshot_row = connection.execute(
            """
            SELECT * FROM connector_snapshots
            WHERE project_id = ? AND snapshot_id = ?
            """,
            (record.project_id, assessment.to_snapshot_id),
        ).fetchone()
        if snapshot_row is None:
            raise CorruptRecordError("release decision target snapshot is missing")
        snapshot = self._connector_snapshot_from_row(snapshot_row)
        if (
            assessment.assessment != record.decision.change_assessment
            or snapshot.snapshot != record.decision.target_snapshot
        ):
            raise IntegrityConflictError(
                "release decision does not reproduce stored target inputs"
            )
        raw_by_id: dict[str, StoredRawReleaseEvidence] = {}
        referenced_ids = set(record.decision.selected_evidence_ids)
        referenced_ids.update(
            item.evidence_id for item in record.decision.rejected_evidence
        )
        for evidence_id in referenced_ids:
            row = connection.execute(
                """
                SELECT * FROM release_evidence
                WHERE project_id = ? AND evidence_id = ?
                """,
                (record.project_id, evidence_id),
            ).fetchone()
            if row is None:
                raise RecordNotFoundError("release decision raw evidence not found")
            raw = self._release_evidence_from_row(row)
            if (
                raw.project_id != record.project_id
                or raw.change_analysis_hash != assessment.analysis_hash
            ):
                raise IntegrityConflictError(
                    "release decision evidence belongs to another target"
                )
            raw_by_id[evidence_id] = raw
        if record.decision.cost_evaluation is not None:
            raw_cost = raw_by_id.get(record.decision.cost_evaluation.evidence_id)
            if (
                raw_cost is None
                or record.decision.cost_evaluation_hash != raw_cost.evidence_hash
            ):
                raise IntegrityConflictError(
                    "release decision cost provenance is not stored raw evidence"
                )
        if record.decision.selected_firmware_build is not None:
            raw_build = raw_by_id.get(
                record.decision.selected_firmware_build.evidence_id
            )
            if (
                raw_build is None
                or canonical_sha256(record.decision.selected_firmware_build)
                != raw_build.evidence_hash
            ):
                raise IntegrityConflictError(
                    "release decision build provenance is not stored raw evidence"
                )
        for result in record.decision.selected_test_results:
            raw_test = raw_by_id.get(result.evidence_id)
            if raw_test is None or canonical_sha256(result) != raw_test.evidence_hash:
                raise IntegrityConflictError(
                    "release decision test provenance is not stored raw evidence"
                )
        self._insert_immutable(
            connection,
            "INSERT INTO release_decisions "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.project_id,
                record.sequence,
                record.previous_decision_hash,
                record.decision_hash,
                record.report_id,
                record.decision.report.change_analysis_hash,
                record.decision.target_snapshot.snapshot_id,
                record.decision.report.snapshot_hash,
                record.decision.report.hardware_revision_id,
                record.decision.report.status.value,
                _canonical_model(record),
                _utc_text(record.decision.report.evaluated_at),
                _utc_text(record.stored_at),
            ),
        )

    def get_release_decision(self, decision_hash: str) -> StoredReleaseDecision:
        with self._reading() as connection:
            row = connection.execute(
                "SELECT * FROM release_decisions WHERE decision_hash = ?",
                (decision_hash,),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError("release decision not found")
        return self._release_decision_from_row(row)

    def list_release_decisions(
        self, project_id: str
    ) -> tuple[StoredReleaseDecision, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT * FROM release_decisions
                WHERE project_id = ? ORDER BY sequence
                """,
                (project_id,),
            ).fetchall()
        return tuple(self._release_decision_from_row(row) for row in rows)

    def put_idempotency(self, record: IdempotencyRecord) -> str:
        def put(connection: sqlite3.Connection) -> str:
            existing = self._load_idempotency(connection, record)
            if existing is not None:
                if existing.request_hash != record.request_hash:
                    raise IdempotencyConflictError(
                        "idempotency key was reused for a different request"
                    )
                return existing.response_json
            project = connection.execute(
                "SELECT 1 FROM projects WHERE project_id = ?",
                (record.project_id,),
            ).fetchone()
            if project is None:
                raise RecordNotFoundError("project not found")
            self._insert_idempotency(connection, record)
            return record.response_json

        return self._write(put)

    def find_idempotency(
        self,
        operation: str,
        project_id: str,
        local_installation_id: str,
        key: str,
        request_hash: str,
    ) -> str | None:
        with self._reading() as connection:
            row = connection.execute(
                """
                SELECT * FROM idempotency_records
                WHERE operation = ? AND project_id = ?
                  AND local_installation_id = ? AND key = ?
                """,
                (operation, project_id, local_installation_id, key),
            ).fetchone()
        if row is None:
            return None
        stored = self._idempotency_from_row(row)
        if stored.request_hash != request_hash:
            raise IdempotencyConflictError(
                "idempotency key was reused for a different request"
            )
        return stored.response_json

    def transact_idempotently(
        self,
        idempotency: IdempotencyRecord,
        *,
        expected_project_version: int | None,
        updated_at: datetime,
        apply: Callable[[AtomicProjectWrite], None],
    ) -> IdempotentWriteResult:
        """Commit one project mutation, version change, and replay record together."""

        def transact(connection: sqlite3.Connection) -> IdempotentWriteResult:
            existing = self._load_idempotency(connection, idempotency)
            if existing is not None:
                if existing.request_hash != idempotency.request_hash:
                    raise IdempotencyConflictError(
                        "idempotency key was reused for a different request"
                    )
                row = connection.execute(
                    "SELECT version FROM projects WHERE project_id = ?",
                    (idempotency.project_id,),
                ).fetchone()
                return IdempotentWriteResult(
                    response_json=existing.response_json,
                    project_version=None if row is None else int(row["version"]),
                    replayed=True,
                )

            prior: sqlite3.Row | None = None
            if expected_project_version is None:
                existing_project = connection.execute(
                    "SELECT 1 FROM projects WHERE project_id = ?",
                    (idempotency.project_id,),
                ).fetchone()
                if existing_project is not None:
                    raise VersionConflictError("project already exists")
            else:
                prior = self._require_project_version(
                    connection,
                    idempotency.project_id,
                    expected_project_version,
                )

            atomic = AtomicProjectWrite(self, connection, idempotency.project_id)
            apply(atomic)
            if expected_project_version is None:
                if not atomic.created or atomic.deleted:
                    raise IntegrityConflictError(
                        "a project-creation transaction must create its project"
                    )
                project_version: int | None = 1
            elif atomic.created:
                raise IntegrityConflictError(
                    "an existing-project transaction cannot create its project"
                )
            elif atomic.deleted:
                project_version = None
            else:
                assert prior is not None
                timestamp = _utc_text(updated_at)
                if timestamp < str(prior["updated_at"]):
                    raise IntegrityConflictError(
                        "project timestamp cannot move backwards"
                    )
                cursor = connection.execute(
                    """
                    UPDATE projects SET version = version + 1, updated_at = ?
                    WHERE project_id = ? AND version = ?
                    """,
                    (
                        timestamp,
                        idempotency.project_id,
                        expected_project_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise VersionConflictError("project version changed during write")
                project_version = expected_project_version + 1

            self._insert_idempotency(connection, idempotency)
            return IdempotentWriteResult(
                response_json=idempotency.response_json,
                project_version=project_version,
                replayed=False,
            )

        return self._write(transact)

    def _load_idempotency(
        self,
        connection: sqlite3.Connection,
        requested: IdempotencyRecord,
    ) -> IdempotencyRecord | None:
        row = connection.execute(
            """
            SELECT * FROM idempotency_records
            WHERE operation = ? AND project_id = ?
                AND local_installation_id = ? AND key = ?
            """,
            (
                requested.operation,
                requested.project_id,
                requested.local_installation_id,
                requested.key,
            ),
        ).fetchone()
        if row is None:
            return None
        stored = self._idempotency_from_row(row)
        self._require_binding(
            stored.operation == requested.operation
            and stored.project_id == requested.project_id
            and stored.local_installation_id == requested.local_installation_id
            and stored.key == requested.key,
            "idempotency row identity does not match its key",
        )
        return stored

    def _idempotency_from_row(self, row: sqlite3.Row) -> IdempotencyRecord:
        return self._validate(
            IdempotencyRecord,
            {
                "operation": row["operation"],
                "project_id": row["project_id"],
                "local_installation_id": row["local_installation_id"],
                "key": row["key"],
                "request_hash": row["request_hash"],
                "response_json": row["response_json"],
                "created_at": row["created_at"],
            },
        )

    @staticmethod
    def _insert_idempotency(
        connection: sqlite3.Connection, record: IdempotencyRecord
    ) -> None:
        SQLiteEvidenceStore._insert_immutable(
            connection,
            "INSERT INTO idempotency_records VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                record.operation,
                record.project_id,
                record.local_installation_id,
                record.key,
                record.request_hash,
                record.response_json,
                _utc_text(record.created_at),
            ),
        )

    def list_interrupted_runs(self) -> tuple[InterruptedRun, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT e.*, p.version AS project_version
                FROM run_events e
                JOIN projects p ON p.project_id = e.project_id
                JOIN (
                    SELECT run_id, MAX(sequence) AS latest_sequence
                    FROM run_events GROUP BY run_id
                ) latest ON latest.run_id = e.run_id
                    AND latest.latest_sequence = e.sequence
                ORDER BY e.project_id, e.run_id
                """
            ).fetchall()
        interrupted: list[InterruptedRun] = []
        for row in rows:
            event = self._event_from_row(row)
            if event.status is not RunLifecycleStatus.RUNNING:
                continue
            interrupted.append(
                self._validate(
                    InterruptedRun,
                    {
                        "project_id": event.project_id,
                        "project_version": row["project_version"],
                        "run_id": event.run_id,
                        "preparation_id": event.preparation_id,
                        "prepare_hash": event.prepare_hash,
                        "latest_sequence": event.sequence,
                        "latest_status": event.status,
                    },
                )
            )
        return tuple(interrupted)

    def list_active_runs(self, project_id: str) -> tuple[StoredRun, ...]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT e.* FROM run_events e
                JOIN (
                    SELECT run_id, MAX(sequence) AS latest_sequence
                    FROM run_events GROUP BY run_id
                ) latest ON latest.run_id = e.run_id
                    AND latest.latest_sequence = e.sequence
                WHERE e.project_id = ?
                ORDER BY e.run_id
                """,
                (project_id,),
            ).fetchall()
            active: list[StoredRun] = []
            for row in rows:
                event = self._event_from_row(row)
                if event.status in {
                    RunLifecycleStatus.QUEUED,
                    RunLifecycleStatus.RUNNING,
                }:
                    active.append(self._load_run(connection, event.run_id))
        return tuple(active)

    def export_project(self, project_id: str) -> EvidenceExport:
        entries: list[dict[str, object]] = []
        with self._reading() as connection:
            project_row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if project_row is None:
                raise RecordNotFoundError("project not found")
            project = self._validate(
                ProjectRecord,
                {
                    "project_id": project_row["project_id"],
                    "name": project_row["name"],
                    "version": project_row["version"],
                    "created_at": project_row["created_at"],
                    "updated_at": project_row["updated_at"],
                },
            )
            self._add_export_entry(entries, "project.json", project)
            self._add_table_entries(
                entries,
                connection,
                self._spec_from_row,
                "SELECT * FROM specs "
                "WHERE project_id = ? ORDER BY spec_id, spec_version",
                project_id,
                lambda row: f"specs/{row['spec_id']}-v{row['spec_version']}.json",
            )
            self._add_table_entries(
                entries,
                connection,
                self._spec_from_row,
                "SELECT * FROM approved_specs "
                "WHERE project_id = ? ORDER BY spec_id, spec_version",
                project_id,
                lambda row: (
                    f"specs/{row['spec_id']}-v{row['spec_version']}-approved.json"
                ),
            )
            self._add_table_entries(
                entries,
                connection,
                self._spec_event_from_row,
                "SELECT * FROM spec_state_events "
                "WHERE project_id = ? ORDER BY spec_id, spec_version, sequence",
                project_id,
                lambda row: (
                    "spec-events/"
                    f"{row['spec_id']}-v{row['spec_version']}-{row['sequence']}.json"
                ),
            )
            self._add_table_entries(
                entries,
                connection,
                self._revision_from_row,
                "SELECT * FROM revisions WHERE project_id = ? ORDER BY revision_number",
                project_id,
                lambda row: f"revisions/{row['revision_id']}.json",
            )
            self._add_table_entries(
                entries,
                connection,
                self._approval_from_row,
                "SELECT * FROM approvals WHERE project_id = ? ORDER BY approval_id",
                project_id,
                lambda row: f"approvals/{row['approval_id']}.json",
            )
            self._add_table_entries(
                entries,
                connection,
                self._preparation_from_row,
                "SELECT * FROM preparations "
                "WHERE project_id = ? ORDER BY preparation_id",
                project_id,
                lambda row: f"preparations/{row['preparation_id']}.json",
            )
            run_ids = connection.execute(
                """
                SELECT DISTINCT run_id FROM run_events
                WHERE project_id = ? ORDER BY run_id
                """,
                (project_id,),
            ).fetchall()
            for row in run_ids:
                run = self._load_run_for_export(connection, row["run_id"])
                self._add_export_entry(entries, f"runs/{row['run_id']}.json", run)
            self._add_table_entries(
                entries,
                connection,
                lambda row: self._evidence_from_row(row, project_id),
                "SELECT * FROM evidence_records "
                "WHERE project_id = ? ORDER BY evidence_id",
                project_id,
                lambda row: f"evidence/{row['evidence_id']}.json",
            )
            self._add_table_entries(
                entries,
                connection,
                self._cost_evaluation_from_row,
                "SELECT * FROM cost_evaluations "
                "WHERE project_id = ? ORDER BY evaluated_at, evidence_id",
                project_id,
                lambda row: f"cost/{row['evidence_id']}.json",
            )
            self._add_table_entries(
                entries,
                connection,
                self._release_policy_from_row,
                "SELECT * FROM release_policies WHERE project_id = ?",
                project_id,
                lambda _row: "release-policy.json",
            )
            self._add_table_entries(
                entries,
                connection,
                self._connector_snapshot_from_row,
                "SELECT * FROM connector_snapshots "
                "WHERE project_id = ? ORDER BY captured_at, snapshot_id",
                project_id,
                lambda row: f"connector-snapshots/{row['snapshot_id']}.json",
            )
            self._add_table_entries(
                entries,
                connection,
                self._change_assessment_from_row,
                "SELECT * FROM change_assessments "
                "WHERE project_id = ? ORDER BY stored_at, analysis_hash",
                project_id,
                lambda row: f"change-assessments/{row['analysis_hash']}.json",
            )
            self._add_table_entries(
                entries,
                connection,
                self._change_preview_from_row,
                "SELECT * FROM change_previews "
                "WHERE project_id = ? ORDER BY generated_at, preview_hash",
                project_id,
                lambda row: f"change-previews/{row['preview_hash']}.json",
            )
            self._add_table_entries(
                entries,
                connection,
                self._external_evidence_plan_from_row,
                "SELECT * FROM external_evidence_plans "
                "WHERE project_id = ? ORDER BY generated_at, plan_hash",
                project_id,
                lambda row: f"external-evidence-plans/{row['plan_hash']}.json",
            )
            self._add_table_entries(
                entries,
                connection,
                self._external_evidence_plan_verification_from_row,
                "SELECT * FROM external_evidence_plan_verifications "
                "WHERE project_id = ? ORDER BY verified_at, verification_hash",
                project_id,
                lambda row: (
                    "external-evidence-plan-verifications/"
                    f"{row['verification_hash']}.json"
                ),
            )
            self._add_table_entries(
                entries,
                connection,
                self._plan_verification_from_row,
                "SELECT * FROM plan_verifications "
                "WHERE project_id = ? ORDER BY verified_at, verification_hash",
                project_id,
                lambda row: f"plan-verifications/{row['verification_hash']}.json",
            )
            self._add_table_entries(
                entries,
                connection,
                self._design_proposal_from_row,
                "SELECT * FROM design_proposals "
                "WHERE project_id = ? ORDER BY stored_at, proposal_hash",
                project_id,
                lambda row: f"design-proposals/{row['proposal_hash']}.json",
            )
            self._add_table_entries(
                entries,
                connection,
                self._design_candidate_from_row,
                "SELECT * FROM design_candidates "
                "WHERE project_id = ? ORDER BY confirmed_at, candidate_hash",
                project_id,
                lambda row: f"design-candidates/{row['candidate_hash']}.json",
            )
            self._add_table_entries(
                entries,
                connection,
                self._simulation_binding_from_row,
                "SELECT * FROM simulation_bindings "
                "WHERE project_id = ? ORDER BY created_at, simulation_hash",
                project_id,
                lambda row: f"simulation-bindings/{row['simulation_hash']}.json",
            )
            self._add_table_entries(
                entries,
                connection,
                self._evidence_claim_from_row,
                "SELECT * FROM conversational_evidence_claims "
                "WHERE project_id = ? ORDER BY stored_at, claim_hash",
                project_id,
                lambda row: f"evidence-claims/{row['claim_hash']}.json",
            )
            self._add_table_entries(
                entries,
                connection,
                self._design_state_transition_from_row,
                "SELECT * FROM design_state_transitions "
                "WHERE project_id = ? ORDER BY session_id, sequence",
                project_id,
                lambda row: (
                    f"design-state-transitions/{row['session_id']}"
                    f"-{row['sequence']}.json"
                ),
            )
            self._add_table_entries(
                entries,
                connection,
                self._release_diagnosis_from_row,
                "SELECT * FROM release_diagnoses "
                "WHERE project_id = ? ORDER BY stored_at, diagnosis_hash",
                project_id,
                lambda row: f"release-diagnoses/{row['diagnosis_hash']}.json",
            )
            self._add_table_entries(
                entries,
                connection,
                self._resolution_plan_from_row,
                "SELECT * FROM resolution_plans "
                "WHERE project_id = ? ORDER BY selected_at, plan_hash",
                project_id,
                lambda row: f"resolution-plans/{row['plan_hash']}.json",
            )
            self._add_table_entries(
                entries,
                connection,
                self._release_evidence_from_row,
                "SELECT * FROM release_evidence "
                "WHERE project_id = ? ORDER BY occurred_at, evidence_id",
                project_id,
                lambda row: f"release-evidence/{row['evidence_id']}.json",
            )
            self._add_table_entries(
                entries,
                connection,
                self._release_decision_from_row,
                "SELECT * FROM release_decisions "
                "WHERE project_id = ? ORDER BY sequence",
                project_id,
                lambda row: f"release-decisions/{row['sequence']}.json",
            )
            self._add_table_entries(
                entries,
                connection,
                self._audit_event_from_row,
                "SELECT * FROM audit_events "
                "WHERE project_id = ? ORDER BY occurred_at, event_id",
                project_id,
                lambda row: f"audit-events/{row['event_id']}.json",
            )
            self._add_table_entries(
                entries,
                connection,
                self._knowledge_source_from_row,
                "SELECT * FROM knowledge_sources "
                "WHERE project_id = ? ORDER BY stored_at, source_id",
                project_id,
                lambda row: f"knowledge-sources/{row['source_id']}.json",
            )
            self._add_table_entries(
                entries,
                connection,
                self._runtime_result_from_row,
                "SELECT * FROM conversation_runtime_results "
                "WHERE project_id = ? ORDER BY stored_at, request_id",
                project_id,
                lambda row: f"conversation-runtime/{row['request_id']}.json",
            )
            self._add_table_entries(
                entries,
                connection,
                self._cad_geometry_from_row,
                "SELECT * FROM cad_geometry_assets "
                "WHERE project_id = ? ORDER BY stored_at, asset_id",
                project_id,
                lambda row: f"cad-geometries/{row['asset_id']}.json",
            )
        entries.sort(key=lambda entry: str(entry["path"]))
        manifest = [
            {
                "path": entry["path"],
                "size": entry["size"],
                "sha256": entry["sha256"],
            }
            for entry in entries
        ]
        content = _canonical_bytes(
            {
                "schema_version": "1.0.0",
                "storage_schema_version": self.SCHEMA_VERSION,
                "project_id": project.project_id,
                "project_version": project.version,
                "manifest": manifest,
                "entries": entries,
            }
        )
        return EvidenceExport(
            filename=(f"project-{project.project_id}-v{project.version}.forge.json"),
            project_version=project.version,
            content=content,
        )

    def _load_run_for_export(
        self, connection: sqlite3.Connection, run_id: str
    ) -> StoredRun:
        return self._load_run(connection, run_id)

    def _add_table_entries(
        self,
        entries: list[dict[str, object]],
        connection: sqlite3.Connection,
        from_row: Callable[[sqlite3.Row], ContractModel],
        query: str,
        project_id: str,
        path_for: Callable[[sqlite3.Row], str],
    ) -> None:
        for row in connection.execute(query, (project_id,)).fetchall():
            value = from_row(row)
            self._add_export_entry(entries, path_for(row), value)

    @staticmethod
    def _add_export_entry(
        entries: list[dict[str, object]], path: str, value: ContractModel
    ) -> None:
        path_pattern = (
            r"project\.json|release-policy\.json|"
            r"(?:specs|spec-events|revisions|approvals|preparations|runs|evidence|cost|"
            r"connector-snapshots|change-assessments|change-previews|"
            r"external-evidence-plans|external-evidence-plan-verifications|"
            r"plan-verifications|design-proposals|design-candidates|"
            r"simulation-bindings|evidence-claims|design-state-transitions|"
            r"release-diagnoses|"
            r"resolution-plans|release-evidence|release-decisions|audit-events|"
            r"knowledge-sources|conversation-runtime|cad-geometries)/"
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,160}\.json"
        )
        if re.fullmatch(path_pattern, path) is None:
            raise CorruptRecordError("stored export path is unsafe")
        content = _canonical_model(value).encode("utf-8")
        entries.append(
            {
                "path": path,
                "size": len(content),
                "sha256": _content_hash(content),
                "payload": json.loads(content),
            }
        )

    @staticmethod
    def _insert_immutable(
        connection: sqlite3.Connection,
        query: str,
        parameters: tuple[object, ...],
    ) -> None:
        try:
            connection.execute(query, parameters)
        except sqlite3.IntegrityError as exc:
            raise IntegrityConflictError("immutable record already exists") from exc

    @staticmethod
    def _put_organization(connection: sqlite3.Connection, record: Organization) -> None:
        _safe_id(record.org_id, "org_id")
        connection.execute(
            """
            INSERT INTO organizations VALUES(?, ?, ?)
            ON CONFLICT(org_id) DO UPDATE SET
                payload_json = excluded.payload_json,
                created_at = excluded.created_at
            """,
            (
                record.org_id,
                _canonical_model(record),
                _utc_text(record.created_at),
            ),
        )

    @staticmethod
    def _put_actor(connection: sqlite3.Connection, record: Actor) -> None:
        connection.execute(
            """
            INSERT INTO actors VALUES(?, ?, ?)
            ON CONFLICT(actor_id) DO UPDATE SET
                payload_json = excluded.payload_json,
                active = excluded.active
            """,
            (record.actor_id, _canonical_model(record), int(record.active)),
        )

    @staticmethod
    def _put_membership(connection: sqlite3.Connection, record: Membership) -> None:
        connection.execute(
            """
            INSERT INTO memberships VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(org_id, actor_id) DO UPDATE SET
                role = excluded.role,
                payload_json = excluded.payload_json,
                granted_at = excluded.granted_at,
                active = excluded.active
            """,
            (
                record.org_id,
                record.actor_id,
                record.role.value,
                _canonical_model(record),
                _utc_text(record.granted_at),
                int(record.active),
            ),
        )

    @staticmethod
    def _put_project_access(
        connection: sqlite3.Connection, record: ProjectAccess
    ) -> None:
        connection.execute(
            """
            INSERT INTO project_access VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(org_id, project_id, actor_id) DO UPDATE SET
                payload_json = excluded.payload_json,
                active = excluded.active
            """,
            (
                record.org_id,
                record.project_id,
                record.actor_id,
                _canonical_model(record),
                int(record.active),
            ),
        )

    @staticmethod
    def _append_audit_event(connection: sqlite3.Connection, record: AuditEvent) -> None:
        existing = connection.execute(
            "SELECT * FROM audit_events WHERE event_id = ?", (record.event_id,)
        ).fetchone()
        if existing is not None:
            stored = SQLiteEvidenceStore._audit_event_from_row(existing)
            if stored == record:
                return
            raise IntegrityConflictError("audit event id was reused")
        SQLiteEvidenceStore._insert_immutable(
            connection,
            "INSERT INTO audit_events VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.event_id,
                record.event_hash,
                record.org_id,
                record.actor_id,
                record.project_id,
                record.operation.value,
                record.request_hash,
                record.result_hash,
                int(record.allowed),
                record.reason,
                _canonical_model(record),
                _utc_text(record.occurred_at),
            ),
        )

    @staticmethod
    def _insert_knowledge_source(
        connection: sqlite3.Connection, record: StoredKnowledgeSource
    ) -> None:
        SQLiteEvidenceStore._insert_immutable(
            connection,
            "INSERT INTO knowledge_sources VALUES(?, ?, ?, ?, ?)",
            (
                record.project_id,
                record.source_id,
                record.ingestion.source.source_hash,
                _canonical_model(record),
                _utc_text(record.stored_at),
            ),
        )

    @staticmethod
    def _insert_runtime_result(
        connection: sqlite3.Connection, record: StoredRuntimeResult
    ) -> None:
        SQLiteEvidenceStore._insert_immutable(
            connection,
            "INSERT INTO conversation_runtime_results VALUES(?, ?, ?, ?, ?)",
            (
                record.project_id,
                record.request_id,
                record.runtime.runtime_hash,
                _canonical_model(record),
                _utc_text(record.stored_at),
            ),
        )

    @staticmethod
    def _insert_cad_geometry(
        connection: sqlite3.Connection, record: StoredCADGeometryAsset
    ) -> None:
        SQLiteEvidenceStore._insert_immutable(
            connection,
            "INSERT INTO cad_geometry_assets VALUES(?, ?, ?, ?, ?)",
            (
                record.project_id,
                record.asset_id,
                record.asset.asset_hash,
                _canonical_model(record),
                _utc_text(record.stored_at),
            ),
        )

    def _organization_from_row(self, row: sqlite3.Row) -> Organization:
        value = self._decode(Organization, row["payload_json"])
        self._require_binding(
            row["org_id"] == value.org_id
            and row["created_at"] == _utc_text(value.created_at),
            "organization row does not match its payload",
        )
        return value

    def _actor_from_row(self, row: sqlite3.Row) -> Actor:
        value = self._decode(Actor, row["payload_json"])
        self._require_binding(
            row["actor_id"] == value.actor_id and bool(row["active"]) == value.active,
            "actor row does not match its payload",
        )
        return value

    def _membership_from_row(self, row: sqlite3.Row) -> Membership:
        value = self._decode(Membership, row["payload_json"])
        self._require_binding(
            row["org_id"] == value.org_id
            and row["actor_id"] == value.actor_id
            and row["role"] == value.role.value
            and row["granted_at"] == _utc_text(value.granted_at)
            and bool(row["active"]) == value.active,
            "membership row does not match its payload",
        )
        return value

    def _project_access_from_row(self, row: sqlite3.Row) -> ProjectAccess:
        value = self._decode(ProjectAccess, row["payload_json"])
        self._require_binding(
            row["org_id"] == value.org_id
            and row["project_id"] == value.project_id
            and row["actor_id"] == value.actor_id
            and bool(row["active"]) == value.active,
            "project access row does not match its payload",
        )
        return value

    @staticmethod
    def _audit_event_from_row(row: sqlite3.Row) -> AuditEvent:
        value = SQLiteEvidenceStore._decode(AuditEvent, row["payload_json"])
        SQLiteEvidenceStore._require_binding(
            row["event_id"] == value.event_id
            and row["event_hash"] == value.event_hash
            and row["org_id"] == value.org_id
            and row["actor_id"] == value.actor_id
            and row["project_id"] == value.project_id
            and row["operation"] == value.operation.value
            and row["request_hash"] == value.request_hash
            and row["result_hash"] == value.result_hash
            and bool(row["allowed"]) == value.allowed
            and row["reason"] == value.reason
            and row["occurred_at"] == _utc_text(value.occurred_at),
            "audit event row does not match its payload",
        )
        return value

    def _knowledge_source_from_row(self, row: sqlite3.Row) -> StoredKnowledgeSource:
        value = self._decode(StoredKnowledgeSource, row["payload_json"])
        self._require_binding(
            row["project_id"] == value.project_id
            and row["source_id"] == value.source_id
            and row["source_hash"] == value.ingestion.source.source_hash
            and row["stored_at"] == _utc_text(value.stored_at),
            "knowledge source row does not match its payload",
        )
        return value

    def _runtime_result_from_row(self, row: sqlite3.Row) -> StoredRuntimeResult:
        value = self._decode(StoredRuntimeResult, row["payload_json"])
        self._require_binding(
            row["project_id"] == value.project_id
            and row["request_id"] == value.request_id
            and row["runtime_hash"] == value.runtime.runtime_hash
            and row["stored_at"] == _utc_text(value.stored_at),
            "conversation runtime row does not match its payload",
        )
        return value

    def _cad_geometry_from_row(self, row: sqlite3.Row) -> StoredCADGeometryAsset:
        value = self._decode(StoredCADGeometryAsset, row["payload_json"])
        self._require_binding(
            row["project_id"] == value.project_id
            and row["asset_id"] == value.asset_id
            and row["asset_hash"] == value.asset.asset_hash
            and row["stored_at"] == _utc_text(value.stored_at),
            "CAD geometry row does not match its payload",
        )
        return value

    def _spec_from_row(self, row: sqlite3.Row) -> StoredSpec:
        value = self._decode(StoredSpec, row["payload_json"])
        self._require_binding(
            row["project_id"] == value.project_id
            and row["spec_id"] == value.spec_id
            and int(row["spec_version"]) == value.spec_version
            and row["spec_hash"] == value.spec_hash
            and row["stored_at"] == _utc_text(value.stored_at),
            "spec row does not match its payload",
        )
        return value

    def _approved_spec_from_rows(
        self, approved_row: sqlite3.Row, draft_row: sqlite3.Row
    ) -> StoredSpec:
        draft = self._spec_from_row(draft_row)
        approved = self._spec_from_row(approved_row)
        self._require_approved_snapshot(draft, approved)
        return approved

    def _require_approved_snapshot(
        self, draft: StoredSpec, approved: StoredSpec
    ) -> None:
        expected = draft.spec.model_copy(
            update={
                "status": SpecStatus.APPROVED,
                "approved_at": approved.spec.approved_at,
            }
        )
        self._require_binding(
            draft.spec.status is SpecStatus.DRAFT
            and draft.project_id == approved.project_id
            and draft.spec_id == approved.spec_id
            and draft.spec_version == approved.spec_version
            and expected == approved.spec,
            "approved spec does not preserve its immutable draft",
        )

    def _spec_event_from_row(self, row: sqlite3.Row) -> SpecStateEvent:
        value = self._decode(SpecStateEvent, row["payload_json"])
        self._require_binding(
            row["event_id"] == value.event_id
            and row["project_id"] == value.project_id
            and row["spec_id"] == value.spec_id
            and int(row["spec_version"]) == value.spec_version
            and int(row["sequence"]) == value.sequence
            and row["status"] == value.status.value
            and row["occurred_at"] == _utc_text(value.occurred_at),
            "spec event row does not match its payload",
        )
        return value

    def _cost_evaluation_from_row(self, row: sqlite3.Row) -> StoredCostEvaluation:
        value = self._decode(StoredCostEvaluation, row["payload_json"])
        self._require_binding(
            row["evidence_id"] == value.evidence_id
            and row["project_id"] == value.project_id
            and row["revision_id"] == value.revision_id
            and row["evaluated_at"] == _utc_text(value.evaluated_at),
            "cost evaluation row does not match its payload",
        )
        return value

    def _release_policy_from_row(self, row: sqlite3.Row) -> StoredReleasePolicy:
        value = self._decode(StoredReleasePolicy, row["payload_json"])
        self._require_binding(
            row["project_id"] == value.project_id
            and row["policy_hash"] == value.policy_hash
            and row["stored_at"] == _utc_text(value.stored_at),
            "release policy row does not match its payload",
        )
        return value

    def _connector_snapshot_from_row(self, row: sqlite3.Row) -> StoredConnectorSnapshot:
        value = self._decode(StoredConnectorSnapshot, row["payload_json"])
        self._require_binding(
            row["project_id"] == value.project_id
            and row["snapshot_id"] == value.snapshot_id
            and row["snapshot_hash"] == value.snapshot_hash
            and row["hardware_revision_id"] == value.snapshot.hardware_revision_id
            and row["captured_at"] == _utc_text(value.snapshot.captured_at)
            and row["stored_at"] == _utc_text(value.stored_at),
            "connector snapshot row does not match its payload",
        )
        return value

    def _change_assessment_from_row(
        self, row: sqlite3.Row
    ) -> StoredChangeImpactAssessment:
        value = self._decode(StoredChangeImpactAssessment, row["payload_json"])
        self._require_binding(
            row["analysis_hash"] == value.analysis_hash
            and row["project_id"] == value.project_id
            and row["from_snapshot_id"] == value.from_snapshot_id
            and row["to_snapshot_id"] == value.to_snapshot_id
            and row["stored_at"] == _utc_text(value.stored_at),
            "change assessment row does not match its payload",
        )
        return value

    def _change_preview_from_row(self, row: sqlite3.Row) -> StoredChangeImpactPreview:
        value = self._decode(StoredChangeImpactPreview, row["payload_json"])
        self._require_binding(
            row["preview_hash"] == value.preview_hash
            and row["project_id"] == value.project_id
            and row["scenario_id"] == value.scenario_id
            and row["scenario_hash"] == value.scenario_hash
            and row["baseline_snapshot_id"] == value.baseline_snapshot_id
            and row["baseline_snapshot_hash"] == value.baseline_snapshot_hash
            and row["generated_at"] == _utc_text(value.preview.generated_at)
            and row["stored_at"] == _utc_text(value.stored_at),
            "change preview row does not match its payload",
        )
        return value

    def _plan_verification_from_row(self, row: sqlite3.Row) -> StoredPlanVerification:
        value = self._decode(StoredPlanVerification, row["payload_json"])
        self._require_binding(
            row["verification_hash"] == value.verification_hash
            and row["project_id"] == value.project_id
            and row["scenario_id"] == value.scenario_id
            and row["preview_hash"] == value.preview_hash
            and row["actual_change_analysis_hash"] == value.actual_change_analysis_hash
            and row["verified_at"] == _utc_text(value.verification.verified_at)
            and row["stored_at"] == _utc_text(value.stored_at),
            "plan verification row does not match its payload",
        )
        return value

    def _external_evidence_plan_from_row(
        self, row: sqlite3.Row
    ) -> StoredExternalEvidencePlan:
        value = self._decode(StoredExternalEvidencePlan, row["payload_json"])
        self._require_binding(
            row["plan_hash"] == value.plan_hash
            and row["project_id"] == value.project_id
            and row["scenario_id"] == value.scenario_id
            and row["scenario_hash"] == value.scenario_hash
            and row["baseline_snapshot_id"] == value.baseline_snapshot_id
            and row["baseline_snapshot_hash"] == value.baseline_snapshot_hash
            and row["generated_at"] == _utc_text(value.plan.generated_at)
            and row["stored_at"] == _utc_text(value.stored_at),
            "external evidence plan row does not match its payload",
        )
        return value

    def _external_evidence_plan_verification_from_row(
        self, row: sqlite3.Row
    ) -> StoredExternalEvidencePlanVerification:
        value = self._decode(
            StoredExternalEvidencePlanVerification, row["payload_json"]
        )
        self._require_binding(
            row["verification_hash"] == value.verification_hash
            and row["project_id"] == value.project_id
            and row["plan_hash"] == value.plan_hash
            and row["actual_change_analysis_hash"] == value.actual_change_analysis_hash
            and row["verified_at"] == _utc_text(value.verification.verified_at)
            and row["stored_at"] == _utc_text(value.stored_at),
            "external evidence plan verification row does not match its payload",
        )
        return value

    def _design_proposal_from_row(self, row: sqlite3.Row) -> StoredDesignProposalSet:
        value = self._decode(StoredDesignProposalSet, row["payload_json"])
        self._require_binding(
            row["proposal_hash"] == value.proposal_hash
            and row["project_id"] == value.project_id
            and row["preview_hash"] == value.preview_hash
            and row["stored_at"] == _utc_text(value.stored_at),
            "design proposal row does not match its payload",
        )
        return value

    def _design_candidate_from_row(self, row: sqlite3.Row) -> StoredDesignCandidate:
        value = self._decode(StoredDesignCandidate, row["payload_json"])
        self._require_binding(
            row["candidate_hash"] == value.candidate_hash
            and row["project_id"] == value.project_id
            and row["session_id"] == value.session_id
            and row["candidate_id"] == value.candidate.candidate_id
            and int(row["revision"]) == value.candidate.revision
            and row["proposal_hash"] == value.candidate.proposal_hash
            and row["confirmed_at"] == _utc_text(value.candidate.confirmed_at)
            and row["stored_at"] == _utc_text(value.stored_at),
            "design candidate row does not match its payload",
        )
        return value

    def _simulation_binding_from_row(self, row: sqlite3.Row) -> StoredSimulationBinding:
        value = self._decode(StoredSimulationBinding, row["payload_json"])
        self._require_binding(
            row["simulation_hash"] == value.simulation_hash
            and row["project_id"] == value.project_id
            and row["session_id"] == value.session_id
            and row["simulation_id"] == value.simulation.simulation_id
            and row["candidate_hash"] == value.candidate_hash
            and row["created_at"] == _utc_text(value.simulation.created_at)
            and row["stored_at"] == _utc_text(value.stored_at),
            "simulation binding row does not match its payload",
        )
        return value

    def _evidence_claim_from_row(self, row: sqlite3.Row) -> StoredEvidenceClaim:
        value = self._decode(StoredEvidenceClaim, row["payload_json"])
        self._require_binding(
            row["claim_hash"] == value.claim_hash
            and row["project_id"] == value.project_id
            and row["session_id"] == value.session_id
            and row["claim_id"] == value.claim.claim_id
            and row["stored_at"] == _utc_text(value.stored_at),
            "evidence claim row does not match its payload",
        )
        return value

    def _design_state_transition_from_row(
        self, row: sqlite3.Row
    ) -> StoredDesignStateTransition:
        value = self._decode(StoredDesignStateTransition, row["payload_json"])
        self._require_binding(
            row["project_id"] == value.project_id
            and row["session_id"] == value.session_id
            and int(row["sequence"]) == value.sequence
            and row["from_state"] == value.transition.from_state.value
            and row["to_state"] == value.transition.to_state.value
            and row["candidate_hash"] == value.transition.candidate_hash
            and row["simulation_hash"] == value.transition.simulation_hash
            and row["occurred_at"] == _utc_text(value.transition.occurred_at)
            and row["stored_at"] == _utc_text(value.stored_at),
            "design state transition row does not match its payload",
        )
        return value

    def _release_diagnosis_from_row(self, row: sqlite3.Row) -> StoredReleaseDiagnosis:
        value = self._decode(StoredReleaseDiagnosis, row["payload_json"])
        self._require_binding(
            row["diagnosis_hash"] == value.diagnosis_hash
            and row["project_id"] == value.project_id
            and row["decision_hash"] == value.decision_hash
            and row["stored_at"] == _utc_text(value.stored_at),
            "release diagnosis row does not match its payload",
        )
        return value

    def _resolution_plan_from_row(self, row: sqlite3.Row) -> StoredResolutionPlan:
        value = self._decode(StoredResolutionPlan, row["payload_json"])
        self._require_binding(
            row["plan_hash"] == value.plan_hash
            and row["project_id"] == value.project_id
            and row["diagnosis_hash"] == value.diagnosis_hash
            and row["fix_proposal_hash"] == value.fix_proposal_hash
            and row["selected_at"] == _utc_text(value.selection.selected_at)
            and row["stored_at"] == _utc_text(value.stored_at),
            "resolution plan row does not match its payload",
        )
        return value

    def _release_evidence_from_row(self, row: sqlite3.Row) -> StoredRawReleaseEvidence:
        value = self._decode(StoredRawReleaseEvidence, row["payload_json"])
        occurred_at = (
            value.evidence.completed_at
            if isinstance(value.evidence, FirmwareBuildEvidence)
            else value.evidence.recorded_at
            if isinstance(value.evidence, TestExecutionEvidence)
            else value.evidence.evaluated_at
        )
        self._require_binding(
            row["evidence_id"] == value.evidence_id
            and row["project_id"] == value.project_id
            and row["evidence_kind"] == value.evidence_kind.value
            and row["change_analysis_hash"] == value.change_analysis_hash
            and row["snapshot_hash"] == value.snapshot_hash
            and row["hardware_revision_id"] == value.hardware_revision_id
            and row["evidence_hash"] == value.evidence_hash
            and row["occurred_at"] == _utc_text(occurred_at)
            and row["stored_at"] == _utc_text(value.stored_at),
            "release evidence row does not match its payload",
        )
        return value

    def _release_decision_from_row(self, row: sqlite3.Row) -> StoredReleaseDecision:
        value = self._decode(StoredReleaseDecision, row["payload_json"])
        decision = value.decision
        self._require_binding(
            row["project_id"] == value.project_id
            and int(row["sequence"]) == value.sequence
            and row["previous_decision_hash"] == value.previous_decision_hash
            and row["decision_hash"] == value.decision_hash
            and row["report_id"] == value.report_id
            and row["change_analysis_hash"] == decision.report.change_analysis_hash
            and row["target_snapshot_id"] == decision.target_snapshot.snapshot_id
            and row["snapshot_hash"] == decision.report.snapshot_hash
            and row["hardware_revision_id"] == decision.report.hardware_revision_id
            and row["status"] == decision.report.status.value
            and row["evaluated_at"] == _utc_text(decision.report.evaluated_at)
            and row["stored_at"] == _utc_text(value.stored_at),
            "release decision row does not match its payload",
        )
        return value

    def _revision_from_row(self, row: sqlite3.Row) -> StoredRevision:
        value = self._decode(StoredRevision, row["payload_json"])
        self._require_binding(
            row["project_id"] == value.project_id
            and row["revision_id"] == value.revision_id
            and int(row["revision_number"]) == value.revision_number
            and row["revision_hash"] == value.revision_hash
            and row["stored_at"] == _utc_text(value.stored_at),
            "revision row does not match its payload",
        )
        return value

    def _approval_from_row(self, row: sqlite3.Row) -> ApprovalRef:
        value = self._decode(ApprovalRef, row["payload_json"])
        self._require_binding(
            row["approval_id"] == value.approval_id
            and row["project_id"] == value.project_id
            and row["subject_kind"] == value.subject_kind.value
            and row["subject_id"] == value.subject_id
            and int(row["subject_version"]) == value.subject_version
            and row["subject_hash"] == value.subject_hash,
            "approval row does not match its payload",
        )
        return value

    def _preparation_from_row(self, row: sqlite3.Row) -> StoredPreparation:
        value = self._decode(StoredPreparation, row["payload_json"])
        preparation = value.preparation
        self._require_binding(
            row["preparation_id"] == preparation.preparation_id
            and row["project_id"] == value.project_id
            and row["prepare_hash"] == preparation.prepare_hash
            and row["stored_at"] == _utc_text(value.stored_at),
            "preparation row does not match its payload",
        )
        return value

    def _event_from_row(self, row: sqlite3.Row) -> RunStateEvent:
        value = self._decode(RunStateEvent, row["payload_json"])
        self._require_binding(
            row["event_id"] == value.event_id
            and row["project_id"] == value.project_id
            and row["run_id"] == value.run_id
            and row["preparation_id"] == value.preparation_id
            and row["prepare_hash"] == value.prepare_hash
            and int(row["sequence"]) == value.sequence
            and row["status"] == value.status.value
            and row["occurred_at"] == _utc_text(value.occurred_at),
            "run event row does not match its payload",
        )
        return value

    def _evidence_from_row(
        self, row: sqlite3.Row, expected_project_id: str
    ) -> EvidenceRecord:
        value = self._decode(EvidenceRecord, row["payload_json"])
        self._require_binding(
            row["evidence_id"] == value.evidence_id
            and row["project_id"] == expected_project_id
            and row["revision_id"] == value.produced_for_revision_id
            and row["recorded_at"] == _utc_text(value.recorded_at),
            "evidence row does not match its payload",
        )
        return value

    @staticmethod
    def _require_binding(condition: bool, message: str) -> None:
        if not condition:
            raise CorruptRecordError(message)

    @staticmethod
    def _validate(model: type[ModelT], value: object) -> ModelT:
        try:
            return model.model_validate(value)
        except (ValidationError, ValueError, TypeError) as exc:
            raise CorruptRecordError(
                "stored record failed contract validation"
            ) from exc

    @classmethod
    def _decode(cls, model: type[ModelT], payload: str) -> ModelT:
        try:
            value = json.loads(payload)
        except (json.JSONDecodeError, TypeError) as exc:
            raise CorruptRecordError("stored record is not valid JSON") from exc
        return cls._validate(model, value)


__all__ = [
    "AtomicProjectWrite",
    "LOCAL_BOOTSTRAP_ACTOR_ID",
    "LOCAL_BOOTSTRAP_ORG_ID",
    "SQLiteEvidenceStore",
]
