from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from forge_core.hashing import canonical_sha256
from forge_core.models import ContractModel
from forge_core.sqlite_store import SQLiteEvidenceStore


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


class OperationalStatus(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"


class DatabaseHealthReport(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    status: OperationalStatus
    database_name: str = Field(min_length=1)
    observed_schema_version: int = Field(ge=0)
    expected_schema_version: int = Field(ge=1)
    latest_migration_version: int | None = Field(default=None, ge=1)
    integrity_result: str = Field(min_length=1)
    checked_at: datetime
    report_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("checked_at")
    @classmethod
    def checked_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "checked_at")

    @model_validator(mode="after")
    def report_must_be_consistent(self) -> DatabaseHealthReport:
        healthy = (
            self.integrity_result == "ok"
            and self.observed_schema_version == self.expected_schema_version
            and self.latest_migration_version == self.observed_schema_version
        )
        if (self.status is OperationalStatus.READY) != healthy:
            raise ValueError("database health status does not match checks")
        if self.report_hash != _health_hash(
            self.model_copy(update={"report_hash": ""})
        ):
            raise ValueError("database health report hash does not match payload")
        return self


class BackupManifest(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    backup_id: str = Field(min_length=1)
    source_database_name: str = Field(min_length=1)
    backup_file_name: str = Field(min_length=1)
    database_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    storage_schema_version: int = Field(ge=1)
    created_at: datetime
    manifest_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("created_at")
    @classmethod
    def created_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "created_at")

    @model_validator(mode="after")
    def manifest_hash_must_match(self) -> BackupManifest:
        if self.manifest_hash != _backup_hash(
            self.model_copy(update={"manifest_hash": ""})
        ):
            raise ValueError("backup manifest hash does not match payload")
        return self


class RestoreVerification(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    status: OperationalStatus
    backup_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    restored_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    destination_database_name: str = Field(min_length=1)
    checked_at: datetime
    health: DatabaseHealthReport
    verification_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("checked_at")
    @classmethod
    def checked_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "checked_at")

    @model_validator(mode="after")
    def verification_must_match(self) -> RestoreVerification:
        if self.status is not self.health.status:
            raise ValueError("restore status does not match restored database health")
        if self.verification_hash != _restore_hash(
            self.model_copy(update={"verification_hash": ""})
        ):
            raise ValueError("restore verification hash does not match payload")
        return self


def _health_hash(report: DatabaseHealthReport) -> str:
    return canonical_sha256(
        report.model_copy(update={"report_hash": "sha256:" + "0" * 64})
    )


def _backup_hash(manifest: BackupManifest) -> str:
    return canonical_sha256(
        manifest.model_copy(update={"manifest_hash": "sha256:" + "0" * 64})
    )


def _restore_hash(verification: RestoreVerification) -> str:
    return canonical_sha256(
        verification.model_copy(update={"verification_hash": "sha256:" + "0" * 64})
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


class SQLiteOperationsService:
    """Creates verified SQLite snapshots without overwriting operator data."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).resolve()

    @staticmethod
    def _read_connection(path: Path) -> sqlite3.Connection:
        return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)

    @staticmethod
    def _require_new_destination(path: Path, source: Path) -> None:
        resolved = path.resolve()
        if resolved == source or path.exists():
            raise FileExistsError("destination must be a new database path")
        if not path.parent.exists() or not path.parent.is_dir():
            raise FileNotFoundError("destination parent directory does not exist")

    def health(self, *, checked_at: datetime) -> DatabaseHealthReport:
        return self.inspect_database(self.database_path, checked_at=checked_at)

    @staticmethod
    def inspect_database(
        database_path: str | Path, *, checked_at: datetime
    ) -> DatabaseHealthReport:
        path = Path(database_path).resolve()
        observed = 0
        latest: int | None = None
        integrity = "unreadable"
        try:
            with SQLiteOperationsService._read_connection(path) as connection:
                integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
                integrity = ",".join(str(row[0]) for row in integrity_rows) or "empty"
                observed = int(connection.execute("PRAGMA user_version").fetchone()[0])
                row = connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()
                latest = None if row is None or row[0] is None else int(row[0])
        except OSError, sqlite3.DatabaseError:
            pass
        status = (
            OperationalStatus.READY
            if integrity == "ok"
            and observed == SQLiteEvidenceStore.SCHEMA_VERSION
            and latest == observed
            else OperationalStatus.BLOCKED
        )
        draft = DatabaseHealthReport.model_construct(
            schema_version="1.0.0",
            status=status,
            database_name=path.name,
            observed_schema_version=observed,
            expected_schema_version=SQLiteEvidenceStore.SCHEMA_VERSION,
            latest_migration_version=latest,
            integrity_result=integrity,
            checked_at=checked_at,
            report_hash="sha256:" + "0" * 64,
        )
        return DatabaseHealthReport.model_validate(
            draft.model_copy(update={"report_hash": _health_hash(draft)}).model_dump()
        )

    def create_backup(
        self,
        destination: str | Path,
        *,
        backup_id: str,
        created_at: datetime,
    ) -> BackupManifest:
        target = Path(destination).resolve()
        self._require_new_destination(target, self.database_path)
        try:
            with (
                self._read_connection(self.database_path) as source,
                sqlite3.connect(target) as output,
            ):
                source.backup(output)
            health = self.inspect_database(target, checked_at=created_at)
            if health.status is not OperationalStatus.READY:
                raise sqlite3.DatabaseError(
                    "created backup failed integrity validation"
                )
            draft = BackupManifest.model_construct(
                schema_version="1.0.0",
                backup_id=backup_id,
                source_database_name=self.database_path.name,
                backup_file_name=target.name,
                database_sha256=_file_sha256(target),
                size_bytes=target.stat().st_size,
                storage_schema_version=health.observed_schema_version,
                created_at=created_at,
                manifest_hash="sha256:" + "0" * 64,
            )
            return BackupManifest.model_validate(
                draft.model_copy(
                    update={"manifest_hash": _backup_hash(draft)}
                ).model_dump()
            )
        except Exception:
            if target.exists():
                target.unlink()
            raise

    @staticmethod
    def verify_backup(
        backup_path: str | Path,
        *,
        checked_at: datetime,
        expected_sha256: str | None = None,
    ) -> DatabaseHealthReport:
        path = Path(backup_path).resolve()
        health = SQLiteOperationsService.inspect_database(path, checked_at=checked_at)
        if expected_sha256 is not None and (
            not path.is_file() or _file_sha256(path) != expected_sha256
        ):
            blocked = health.model_copy(
                update={
                    "status": OperationalStatus.BLOCKED,
                    "integrity_result": "sha256_mismatch",
                    "report_hash": "sha256:" + "0" * 64,
                }
            )
            return DatabaseHealthReport.model_validate(
                blocked.model_copy(
                    update={"report_hash": _health_hash(blocked)}
                ).model_dump()
            )
        return health

    @staticmethod
    def restore_to_new_database(
        backup_path: str | Path,
        destination: str | Path,
        *,
        checked_at: datetime,
    ) -> RestoreVerification:
        source_path = Path(backup_path).resolve()
        target = Path(destination).resolve()
        SQLiteOperationsService._require_new_destination(target, source_path)
        backup_health = SQLiteOperationsService.inspect_database(
            source_path, checked_at=checked_at
        )
        if backup_health.status is not OperationalStatus.READY:
            raise sqlite3.DatabaseError("backup failed integrity validation")
        try:
            with (
                SQLiteOperationsService._read_connection(source_path) as source,
                sqlite3.connect(target) as output,
            ):
                source.backup(output)
            restored_health = SQLiteOperationsService.inspect_database(
                target, checked_at=checked_at
            )
            draft = RestoreVerification.model_construct(
                schema_version="1.0.0",
                status=restored_health.status,
                backup_sha256=_file_sha256(source_path),
                restored_sha256=_file_sha256(target),
                destination_database_name=target.name,
                checked_at=checked_at,
                health=restored_health,
                verification_hash="sha256:" + "0" * 64,
            )
            return RestoreVerification.model_validate(
                draft.model_copy(
                    update={"verification_hash": _restore_hash(draft)}
                ).model_dump()
            )
        except Exception:
            if target.exists():
                target.unlink()
            raise
