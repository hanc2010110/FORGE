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

from forge_core.design import (
    ArtifactKind,
    EvidenceClass,
    EvidenceRecord,
    dependency_hash,
)
from forge_core.hashing import canonical_sha256
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

ModelT = TypeVar("ModelT", bound=ContractModel)
ResultT = TypeVar("ResultT")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


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

    SCHEMA_VERSION = 2
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
                PRAGMA user_version = 2;
                COMMIT;
                """
            )
        except sqlite3.Error as exc:
            with suppress(sqlite3.Error):
                connection.rollback()
            raise MigrationError("failed to initialize SQLite schema") from exc

    def _migrate(self, connection: sqlite3.Connection, version: int) -> None:
        if version != 1:
            raise MigrationError(f"no migration path from schema {version}")
        try:
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
        except sqlite3.Error as exc:
            with suppress(sqlite3.Error):
                connection.rollback()
            raise MigrationError("failed to migrate SQLite schema") from exc

    def create_project(self, project: ProjectRecord) -> ProjectRecord:
        def insert(connection: sqlite3.Connection) -> ProjectRecord:
            self._insert_project(connection, project)
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
            r"project\.json|"
            r"(?:specs|spec-events|revisions|approvals|preparations|runs|evidence|cost)/"
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


__all__ = ["AtomicProjectWrite", "SQLiteEvidenceStore"]
