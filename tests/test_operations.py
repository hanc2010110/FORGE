from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from forge_core.operations import OperationalStatus, SQLiteOperationsService
from forge_core.persistence import ProjectRecord
from forge_core.sqlite_store import SQLiteEvidenceStore

NOW = datetime(2026, 9, 3, 13, tzinfo=UTC)


class SQLiteOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "forge.db"
        self.store = SQLiteEvidenceStore(self.database)
        self.store.create_project(
            ProjectRecord(
                project_id="robot-arm",
                name="Robot Arm",
                version=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        self.operations = SQLiteOperationsService(self.database)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_health_backup_verify_and_non_destructive_restore(self) -> None:
        health = self.operations.health(checked_at=NOW)
        self.assertEqual(health.status, OperationalStatus.READY)

        backup = self.root / "forge.backup.db"
        manifest = self.operations.create_backup(
            backup, backup_id="backup-20260903", created_at=NOW
        )
        self.assertTrue(backup.is_file())
        self.assertEqual(manifest.database_sha256[:7], "sha256:")
        self.assertEqual(
            self.operations.verify_backup(
                backup,
                checked_at=NOW,
                expected_sha256=manifest.database_sha256,
            ).status,
            OperationalStatus.READY,
        )

        restored = self.root / "restored.db"
        verification = self.operations.restore_to_new_database(
            backup, restored, checked_at=NOW
        )
        self.assertEqual(verification.status, OperationalStatus.READY)
        self.assertEqual(
            SQLiteEvidenceStore(restored).get_project("robot-arm").name, "Robot Arm"
        )

    def test_never_overwrites_backup_or_restore_destination(self) -> None:
        backup = self.root / "existing.db"
        backup.write_bytes(b"operator-owned")
        with self.assertRaises(FileExistsError):
            self.operations.create_backup(
                backup, backup_id="backup-existing", created_at=NOW
            )
        self.assertEqual(backup.read_bytes(), b"operator-owned")

        valid_backup = self.root / "valid.db"
        self.operations.create_backup(
            valid_backup, backup_id="backup-valid", created_at=NOW
        )
        with self.assertRaises(FileExistsError):
            self.operations.restore_to_new_database(
                valid_backup, self.database, checked_at=NOW
            )

    def test_corrupt_and_schema_mismatched_databases_fail_closed(self) -> None:
        corrupt = self.root / "corrupt.db"
        corrupt.write_bytes(b"not a sqlite database")
        self.assertEqual(
            self.operations.inspect_database(corrupt, checked_at=NOW).status,
            OperationalStatus.BLOCKED,
        )

        old = self.root / "old.db"
        with sqlite3.connect(old) as connection:
            connection.execute("CREATE TABLE schema_migrations(version INTEGER)")
            connection.execute("INSERT INTO schema_migrations VALUES(1)")
            connection.execute("PRAGMA user_version = 1")
        self.assertEqual(
            self.operations.inspect_database(old, checked_at=NOW).status,
            OperationalStatus.BLOCKED,
        )

    def test_backup_hash_mismatch_and_contract_tamper_are_detected(self) -> None:
        backup = self.root / "valid.db"
        manifest = self.operations.create_backup(
            backup, backup_id="backup-valid", created_at=NOW
        )
        mismatch = self.operations.verify_backup(
            backup,
            checked_at=NOW,
            expected_sha256="sha256:" + "0" * 64,
        )
        self.assertEqual(mismatch.status, OperationalStatus.BLOCKED)
        with self.assertRaises(ValidationError):
            manifest.model_copy(update={"size_bytes": 1}).model_validate(
                manifest.model_copy(update={"size_bytes": 1}).model_dump()
            )


if __name__ == "__main__":
    unittest.main()
