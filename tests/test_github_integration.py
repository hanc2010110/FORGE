from __future__ import annotations

import json
import stat
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError

from forge_core.github_integration import (
    GitHubConnectCommand,
    GitHubCredentialStore,
    GitHubIntegrationError,
    GitHubIntegrationService,
    GitHubIntegrationStatus,
    GitHubMutationResult,
    GitHubRepositoryEvidence,
    OpenSSLRS256Signer,
    UrllibGitHubTransport,
)
from forge_core.hashing import canonical_sha256
from forge_core.integration_hub import IntegrationHub
from forge_core.loopback_api import APIResponse, LoopbackAPI
from forge_core.release_service import ReleaseIntegrationService
from forge_core.sqlite_store import SQLiteEvidenceStore
from tests.test_release_service import registry

NOW = datetime(2026, 9, 5, 2, 0, tzinfo=UTC)
HEAD_SHA = "a" * 40
PR_HEAD_SHA = "b" * 40
PR_BASE_SHA = "c" * 40
PEM = "-----BEGIN PRIVATE KEY-----\nZmFrZS1rZXk=\n-----END PRIVATE KEY-----\n"


class FakeSigner:
    def sign(self, message: bytes, private_key_path: Path) -> bytes:
        assert message.count(b".") == 1
        assert private_key_path.read_text(encoding="ascii") == PEM
        return b"signed"


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, object | None]] = []
        self.commit_file_pages: list[list[str]] | None = None
        self.commit_sha_pages: list[str] | None = None
        self.pull_pages: list[list[int]] | None = None
        self.workflow_run_pages: list[list[int]] | None = None
        self.workflow_status: object = "completed"
        self.workflow_conclusion: object | None = "success"
        self.permissions: dict[str, str] = {
            "actions": "read",
            "contents": "read",
            "metadata": "read",
            "pull_requests": "read",
        }

    def request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        token: str,
        payload: object | None = None,
    ) -> object:
        self.calls.append((method, path, token, payload))
        if path == "/app":
            return {"id": 11, "slug": "forge-test"}
        if path == "/app/installations/22":
            return {"id": 22, "account": {"login": "acme"}}
        if path == "/app/installations/22/access_tokens":
            return {
                "token": "installation-token-not-persisted",
                "expires_at": "2026-09-05T03:00:00Z",
                "permissions": self.permissions,
            }
        if path == "/repos/acme/FORGE":
            return {
                "full_name": "acme/FORGE",
                "private": True,
                "default_branch": "main",
            }
        if path.startswith("/repos/acme/FORGE/commits/main?"):
            query = parse_qs(urlsplit(path).query)
            page = int(query["page"][0])
            if self.commit_file_pages is None:
                filenames = ["tests/test_release.py", "forge_core/release.py"]
            elif page <= len(self.commit_file_pages):
                filenames = self.commit_file_pages[page - 1]
            else:
                filenames = []
            sha = (
                self.commit_sha_pages[page - 1]
                if self.commit_sha_pages is not None
                and page <= len(self.commit_sha_pages)
                else HEAD_SHA
            )
            return {
                "sha": sha,
                "html_url": f"https://github.com/acme/FORGE/commit/{HEAD_SHA}",
                "commit": {
                    "message": "Verify release evidence",
                    "committer": {"date": "2026-09-05T01:58:00Z"},
                },
                "files": [{"filename": filename} for filename in filenames],
                "stats": {"total": 999},
            }
        if path.startswith("/repos/acme/FORGE/pulls?"):
            query = parse_qs(urlsplit(path).query)
            page = int(query["page"][0])
            pages = self.pull_pages if self.pull_pages is not None else [[7]]
            numbers = pages[page - 1] if page <= len(pages) else []
            return [
                {
                    "number": number,
                    "title": f"Hardware revision PR {number}",
                    "head": {"sha": PR_HEAD_SHA},
                    "base": {"sha": PR_BASE_SHA},
                    "updated_at": "2026-09-05T01:50:00Z",
                    "html_url": f"https://github.com/acme/FORGE/pull/{number}",
                }
                for number in numbers
            ]
        if path.startswith(f"/repos/acme/FORGE/actions/runs?head_sha={HEAD_SHA}&"):
            query = parse_qs(urlsplit(path).query)
            page = int(query["page"][0])
            pages = (
                self.workflow_run_pages
                if self.workflow_run_pages is not None
                else [[91]]
            )
            run_ids = pages[page - 1] if page <= len(pages) else []
            return {
                "total_count": sum(len(item) for item in pages),
                "workflow_runs": [
                    {
                        "id": run_id,
                        "name": "FORGE verify",
                        "event": "push",
                        "status": self.workflow_status,
                        "conclusion": self.workflow_conclusion,
                        "head_sha": HEAD_SHA,
                        "created_at": "2026-09-05T01:59:00Z",
                        "updated_at": "2026-09-05T02:00:00Z",
                        "html_url": f"https://github.com/acme/FORGE/actions/runs/{run_id}",
                    }
                    for run_id in run_ids
                ],
            }
        raise AssertionError(f"unexpected request: {method} {path}")


class FakeGitHubPort:
    def __init__(self) -> None:
        self.commands: list[GitHubConnectCommand] = []
        self.sync_count = 0

    def status(self) -> dict[str, object]:
        return {"available": True, "configured": False}

    def test_connection(self, command: GitHubConnectCommand) -> dict[str, object]:
        self.commands.append(command)
        return {"verified": True, "private_key_sha256": "sha256:" + "1" * 64}

    def connect(
        self, command: GitHubConnectCommand, *, idempotency_key: str
    ) -> GitHubMutationResult:
        self.commands.append(command)
        return GitHubMutationResult(
            data={"connected": True, "repository_full_name": "acme/FORGE"}
        )

    def sync(self, *, idempotency_key: str) -> GitHubMutationResult:
        self.sync_count += 1
        return GitHubMutationResult(
            data={"head_sha": HEAD_SHA, "evidence_hash": "sha256:" + "2" * 64}
        )


def command(**updates: object) -> GitHubConnectCommand:
    values: dict[str, object] = {
        "app_id": 11,
        "installation_id": 22,
        "owner": "acme",
        "repository": "FORGE",
        "private_key_filename": "forge-test.pem",
        "private_key_pem": PEM,
    }
    values.update(updates)
    return GitHubConnectCommand.model_validate(values)


class GitHubIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "github"
        self.store = GitHubCredentialStore(self.root)
        self.transport = FakeTransport()
        self.service = GitHubIntegrationService(
            self.store,
            transport=self.transport,
            signer=FakeSigner(),
            clock=lambda: NOW,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def connect(
        self,
        value: GitHubConnectCommand | None = None,
        *,
        key: str = "connect-key",
    ) -> GitHubMutationResult:
        return self.service.connect(value or command(), idempotency_key=key)

    def sync(self, *, key: str = "sync-key") -> GitHubMutationResult:
        return self.service.sync(idempotency_key=key)

    def restart_service(self) -> None:
        self.store = GitHubCredentialStore(self.root)
        self.service = GitHubIntegrationService(
            self.store,
            transport=self.transport,
            signer=FakeSigner(),
            clock=lambda: NOW,
        )

    def test_guided_test_connect_status_and_exact_head_sync(self) -> None:
        self.assertEqual(
            self.service.status(),
            {
                "available": True,
                "configured": False,
                "app_id": None,
                "installation_id": None,
                "repository_full_name": None,
                "private_key_filename": None,
                "private_key_sha256": None,
                "connected_at": None,
                "latest_evidence": None,
                "latest_evidence_state": "missing",
                "latest_evidence_age_seconds": None,
            },
        )
        tested = self.service.test_connection(command())
        self.assertEqual(tested["repository_full_name"], "acme/FORGE")
        self.assertFalse((self.root / "github.json").exists())

        connected = self.connect()
        self.assertEqual(connected.data["app_slug"], "forge-test")
        status_payload = self.service.status()
        self.assertTrue(status_payload["configured"])
        self.assertEqual(status_payload["repository_full_name"], "acme/FORGE")
        self.assertNotIn("private_key_pem", json.dumps(status_payload))

        evidence = GitHubRepositoryEvidence.model_validate(self.sync().data)
        self.assertEqual(evidence.head_sha, HEAD_SHA)
        self.assertEqual(
            evidence.changed_files,
            ("forge_core/release.py", "tests/test_release.py"),
        )
        self.assertEqual(evidence.workflow_runs[0].head_sha, evidence.head_sha)
        self.assertEqual(evidence.workflow_runs[0].conclusion, "success")
        self.assertEqual(evidence.total_changed_files, 2)
        current_status = GitHubIntegrationStatus.model_validate(self.service.status())
        assert current_status.latest_evidence is not None
        self.assertEqual(current_status.latest_evidence_state, "fresh")
        self.assertEqual(current_status.latest_evidence_age_seconds, 0)
        self.assertEqual(
            current_status.latest_evidence.evidence_hash, evidence.evidence_hash
        )
        persisted = json.dumps(
            json.loads((self.root / "github.json").read_text(encoding="utf-8"))
        )
        self.assertNotIn("installation-token-not-persisted", persisted)
        self.assertNotIn("BEGIN PRIVATE KEY", persisted)
        key_files = tuple((self.root / "keys").glob("*.pem"))
        self.assertEqual(len(key_files), 1)
        self.assertEqual(stat.S_IMODE(key_files[0].stat().st_mode), 0o600)

    def test_insufficient_permissions_and_identity_mismatch_fail_closed(self) -> None:
        self.transport.permissions = {"contents": "read"}
        with self.assertRaisesRegex(
            GitHubIntegrationError, "github_permissions_insufficient"
        ):
            self.connect()
        self.assertFalse((self.root / "github.json").exists())
        self.assertEqual(tuple((self.root / "keys").iterdir()), ())

        self.transport.permissions["actions"] = "read"
        self.transport.permissions["pull_requests"] = "read"
        with self.assertRaisesRegex(GitHubIntegrationError, "github_app_mismatch"):
            self.service.test_connection(command(app_id=12))

    def test_stored_key_and_evidence_tampering_are_rejected(self) -> None:
        self.connect()
        settings = self.store.load()
        assert settings is not None
        key_path = self.root / "keys" / settings.private_key_file
        key_path.write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(
            GitHubIntegrationError, "github_private_key_changed"
        ):
            self.service.status()

        key_path.write_text(PEM, encoding="ascii")
        evidence = GitHubRepositoryEvidence.model_validate(self.sync().data)
        latest = self.root / "evidence" / "latest.json"
        value = json.loads(latest.read_text(encoding="utf-8"))
        value["head_sha"] = "d" * 40
        latest.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(GitHubIntegrationError, "github_evidence_invalid"):
            self.service.status()
        self.assertTrue(
            (self.root / "evidence" / f"{evidence.evidence_hash[7:]}.json").is_file()
        )

    def test_status_hides_evidence_from_a_previous_connection(self) -> None:
        self.connect()
        self.sync()
        staged_key, fingerprint = self.store.stage_key(PEM)
        self.store.commit(
            command(repository="FORGE-next"),
            staged_key,
            fingerprint,
            connected_at=NOW,
        )

        status = GitHubIntegrationStatus.model_validate(self.service.status())

        self.assertEqual(status.repository_full_name, "acme/FORGE-next")
        self.assertIsNone(status.latest_evidence)
        self.assertEqual(status.latest_evidence_state, "missing")

    def test_sync_collects_every_changed_file_across_commit_pages(self) -> None:
        first_page = [f"hardware/part-{index:03d}.step" for index in range(100)]
        self.transport.commit_file_pages = [first_page, ["firmware/main.c"]]
        self.connect()

        evidence = GitHubRepositoryEvidence.model_validate(self.sync().data)

        self.assertEqual(evidence.total_changed_files, 101)
        self.assertEqual(len(evidence.changed_files), 101)
        self.assertIn("firmware/main.c", evidence.changed_files)
        self.assertIn(
            "/repos/acme/FORGE/commits/main?per_page=100&page=2",
            evidence.source_endpoints,
        )

    def test_sync_collects_every_pull_request_and_workflow_run_page(self) -> None:
        self.transport.pull_pages = [list(range(1, 101)), [101]]
        self.transport.workflow_run_pages = [list(range(1, 101)), [101]]
        self.connect()

        evidence = GitHubRepositoryEvidence.model_validate(self.sync().data)

        self.assertEqual(len(evidence.open_pull_requests), 101)
        self.assertEqual(len(evidence.workflow_runs), 101)
        self.assertIn(
            "/repos/acme/FORGE/pulls?state=open&per_page=100&page=2",
            evidence.source_endpoints,
        )
        self.assertIn(
            f"/repos/acme/FORGE/actions/runs?head_sha={HEAD_SHA}&per_page=100&page=2",
            evidence.source_endpoints,
        )

    def test_sync_fails_closed_when_pull_requests_exceed_collection_limit(
        self,
    ) -> None:
        self.transport.pull_pages = [
            list(range(page * 100 + 1, page * 100 + 101)) for page in range(30)
        ]
        self.connect()

        with self.assertRaisesRegex(
            GitHubIntegrationError, "github_pull_requests_incomplete"
        ):
            self.sync()

        self.assertIsNone(self.store.latest_evidence())

    def test_sync_fails_closed_when_workflow_runs_exceed_collection_limit(
        self,
    ) -> None:
        self.transport.workflow_run_pages = [
            list(range(page * 100 + 1, page * 100 + 101)) for page in range(10)
        ] + [[1_001]]
        self.connect()

        with self.assertRaisesRegex(
            GitHubIntegrationError, "github_workflow_runs_incomplete"
        ):
            self.sync()

        self.assertIsNone(self.store.latest_evidence())

    def test_sync_accepts_exactly_one_thousand_workflow_runs(self) -> None:
        self.transport.workflow_run_pages = [
            list(range(page * 100 + 1, page * 100 + 101)) for page in range(10)
        ]
        self.connect()

        evidence = GitHubRepositoryEvidence.model_validate(self.sync().data)

        self.assertEqual(len(evidence.workflow_runs), 1_000)
        self.assertIn(
            f"/repos/acme/FORGE/actions/runs?head_sha={HEAD_SHA}&per_page=100&page=10",
            evidence.source_endpoints,
        )

    def test_sync_fails_closed_when_changed_files_cannot_be_proven_complete(
        self,
    ) -> None:
        self.transport.commit_file_pages = [
            [f"page-{page}/file-{index}.txt" for index in range(100)]
            for page in range(30)
        ]
        self.connect()

        with self.assertRaisesRegex(
            GitHubIntegrationError, "github_changed_files_incomplete"
        ):
            self.sync()

        self.assertIsNone(self.store.latest_evidence())

    def test_sync_fails_closed_when_head_changes_between_file_pages(self) -> None:
        self.transport.commit_file_pages = [
            [f"hardware/file-{index}.step" for index in range(100)],
            ["firmware/main.c"],
        ]
        self.transport.commit_sha_pages = [HEAD_SHA, "d" * 40]
        self.connect()

        with self.assertRaisesRegex(
            GitHubIntegrationError, "github_changed_files_incomplete"
        ):
            self.sync()

    def test_status_marks_old_evidence_stale(self) -> None:
        self.connect()
        self.sync()
        later_service = GitHubIntegrationService(
            self.store,
            transport=self.transport,
            signer=FakeSigner(),
            clock=lambda: NOW + timedelta(hours=1),
        )

        status = GitHubIntegrationStatus.model_validate(later_service.status())

        self.assertEqual(status.latest_evidence_state, "stale")
        self.assertEqual(status.latest_evidence_age_seconds, 3600)

        rollback_service = GitHubIntegrationService(
            self.store,
            transport=self.transport,
            signer=FakeSigner(),
            clock=lambda: NOW - timedelta(seconds=1),
        )
        rollback_status = GitHubIntegrationStatus.model_validate(
            rollback_service.status()
        )
        self.assertEqual(rollback_status.latest_evidence_state, "stale")
        self.assertEqual(rollback_status.latest_evidence_age_seconds, 0)

    def test_mutations_replay_exact_results_and_reject_key_reuse(self) -> None:
        first_connect = self.connect(key="stable-connect")
        calls_after_connect = len(self.transport.calls)
        replayed_connect = self.connect(key="stable-connect")

        self.assertFalse(first_connect.replayed)
        self.assertTrue(replayed_connect.replayed)
        self.assertEqual(replayed_connect.data, first_connect.data)
        self.assertEqual(len(self.transport.calls), calls_after_connect)
        with self.assertRaisesRegex(
            GitHubIntegrationError, "github_idempotency_conflict"
        ):
            self.connect(command(repository="FORGE-next"), key="stable-connect")

        first_sync = self.sync(key="stable-sync")
        calls_after_sync = len(self.transport.calls)
        replayed_sync = self.sync(key="stable-sync")

        self.assertFalse(first_sync.replayed)
        self.assertTrue(replayed_sync.replayed)
        self.assertEqual(replayed_sync.data, first_sync.data)
        self.assertEqual(len(self.transport.calls), calls_after_sync)

    def test_connect_recovers_prepared_result_without_repeating_github_calls(
        self,
    ) -> None:
        with (
            patch.object(
                self.store, "commit", side_effect=OSError("simulated settings failure")
            ),
            self.assertRaisesRegex(OSError, "simulated settings failure"),
        ):
            self.connect(key="recover-connect")

        calls_after_failure = len(self.transport.calls)
        prepared = self.store.find_idempotency(
            "connect", "recover-connect", canonical_sha256(command())
        )
        assert prepared is not None
        self.assertEqual(prepared.state, "prepared")

        self.restart_service()
        recovered = self.connect(key="recover-connect")

        self.assertTrue(recovered.replayed)
        self.assertEqual(len(self.transport.calls), calls_after_failure)
        settings = self.store.load()
        assert settings is not None
        self.assertEqual(settings.repository, "FORGE")
        completed = self.store.find_idempotency(
            "connect", "recover-connect", canonical_sha256(command())
        )
        assert completed is not None
        self.assertEqual(completed.state, "completed")

    def test_connect_recovers_after_settings_commit_before_completion_marker(
        self,
    ) -> None:
        with (
            patch.object(
                self.store,
                "complete_idempotency",
                side_effect=OSError("simulated completion-marker failure"),
            ),
            self.assertRaisesRegex(OSError, "simulated completion-marker failure"),
        ):
            self.connect(key="recover-committed-connect")

        calls_after_failure = len(self.transport.calls)
        settings = self.store.load()
        assert settings is not None
        self.assertEqual(settings.repository, "FORGE")

        self.restart_service()
        recovered = self.connect(key="recover-committed-connect")

        self.assertTrue(recovered.replayed)
        self.assertEqual(len(self.transport.calls), calls_after_failure)
        self.assertEqual(len(tuple((self.root / "keys").glob("*.pem"))), 1)

    def test_sync_recovers_after_evidence_commit_without_repeating_github_calls(
        self,
    ) -> None:
        self.connect()
        with (
            patch.object(
                self.store,
                "complete_idempotency",
                side_effect=OSError("simulated completion-marker failure"),
            ),
            self.assertRaisesRegex(OSError, "simulated completion-marker failure"),
        ):
            self.sync(key="recover-sync")

        calls_after_failure = len(self.transport.calls)
        persisted = self.store.latest_evidence()
        assert persisted is not None

        self.restart_service()
        recovered = self.sync(key="recover-sync")

        self.assertTrue(recovered.replayed)
        self.assertEqual(recovered.data, persisted.model_dump(mode="json"))
        self.assertEqual(len(self.transport.calls), calls_after_failure)

    def test_service_startup_removes_orphaned_staged_keys(self) -> None:
        orphan, _fingerprint = self.store.stage_key(PEM)
        self.assertTrue(orphan.is_file())

        GitHubIntegrationService(
            self.store,
            transport=self.transport,
            signer=FakeSigner(),
            clock=lambda: NOW,
        )

        self.assertFalse(orphan.exists())

    def test_non_string_workflow_conclusion_is_rejected(self) -> None:
        self.transport.workflow_conclusion = {"unexpected": True}
        self.connect()

        with self.assertRaisesRegex(GitHubIntegrationError, "github_invalid_response"):
            self.sync()

    def test_null_workflow_conclusion_is_preserved(self) -> None:
        self.transport.workflow_conclusion = None
        self.connect()

        evidence = GitHubRepositoryEvidence.model_validate(self.sync().data)

        self.assertIsNone(evidence.workflow_runs[0].conclusion)

    def test_unsupported_workflow_status_and_conclusion_are_rejected(self) -> None:
        self.connect()
        self.transport.workflow_status = "unknown_status"
        with self.assertRaisesRegex(GitHubIntegrationError, "github_invalid_response"):
            self.sync(key="bad-status")

        self.transport.workflow_status = "completed"
        self.transport.workflow_conclusion = "unknown_conclusion"
        with self.assertRaisesRegex(GitHubIntegrationError, "github_invalid_response"):
            self.sync(key="bad-conclusion")

    def test_models_reject_paths_encrypted_keys_and_evidence_rebinding(self) -> None:
        for update in (
            {"owner": "../acme"},
            {"repository": "bad/name"},
            {"private_key_filename": "../key.pem"},
            {
                "private_key_pem": (
                    "-----BEGIN ENCRYPTED PRIVATE KEY-----\nx\n"
                    "-----END ENCRYPTED PRIVATE KEY-----"
                )
            },
        ):
            with self.subTest(update=update), self.assertRaises(ValidationError):
                command(**update)

        self.connect()
        evidence = GitHubRepositoryEvidence.model_validate(self.sync().data)
        tampered = evidence.model_dump(mode="python")
        tampered["workflow_runs"][0]["head_sha"] = "e" * 40
        with self.assertRaises(ValidationError):
            GitHubRepositoryEvidence.model_validate(tampered)

    def test_missing_and_corrupt_configuration_fail_safely(self) -> None:
        with self.assertRaisesRegex(GitHubIntegrationError, "github_not_configured"):
            self.sync()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "github.json").write_text("not-json", encoding="utf-8")
        with self.assertRaisesRegex(
            GitHubIntegrationError, "github_configuration_invalid"
        ):
            self.service.status()

    def test_signer_and_transport_validate_local_security_boundaries(self) -> None:
        signer = OpenSSLRS256Signer("/definitely/missing/openssl")
        key = Path(self.temporary.name) / "key.pem"
        key.write_text(PEM, encoding="ascii")
        with self.assertRaisesRegex(
            GitHubIntegrationError, "private_key_signing_failed"
        ):
            signer.sign(b"message", key)

        transport = UrllibGitHubTransport()
        for path, token in (("https://evil.test", "token"), ("/app", "bad\ntoken")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                transport.request("GET", path, token=token)
        with self.assertRaises(ValueError):
            UrllibGitHubTransport(timeout_seconds=0)
        with self.assertRaises(ValueError):
            GitHubIntegrationService(self.store, evidence_freshness_seconds=0)

    def test_transport_rejects_malformed_content_length(self) -> None:
        class Response:
            headers = {"Content-Length": "not-a-number"}

            def __enter__(self) -> Response:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int) -> bytes:
                return b"{}"

        class Opener:
            def open(self, *_args: object, **_kwargs: object) -> Response:
                return Response()

        transport = UrllibGitHubTransport()
        cast(Any, transport)._opener = Opener()

        with self.assertRaisesRegex(GitHubIntegrationError, "github_invalid_response"):
            transport.request("GET", "/app", token="token")


class GitHubLoopbackAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SQLiteEvidenceStore(Path(self.temporary.name) / "forge.db")
        self.port = FakeGitHubPort()
        self.api = LoopbackAPI(
            ReleaseIntegrationService(self.store, registry(), clock=lambda: NOW),
            port=43127,
            csrf_secret=b"test-secret",
            local_installation_id="installation-1",
            nonce_factory=lambda: "a" * 64,
            github_integration=self.port,
            integration_hub=IntegrationHub(status_providers={"github": self.port}),
        )
        session = self.api.handle(
            "GET", "/api/v1/session", [("Host", self.api.expected_host)]
        )
        self.csrf = str(session.json()["csrf_token"])

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def headers(
        self,
        body: bytes,
        *,
        csrf: bool = True,
        idempotency: bool = True,
    ) -> list[tuple[str, str]]:
        values = [
            ("Host", self.api.expected_host),
            ("Origin", self.api.expected_origin),
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
        ]
        if csrf:
            values.extend(
                [
                    ("Cookie", f"forge_csrf={self.csrf}"),
                    ("X-FORGE-CSRF", self.csrf),
                ]
            )
        if idempotency:
            values.append(("Idempotency-Key", "github-test-key"))
        return values

    def post(
        self,
        path: str,
        payload: object,
        *,
        csrf: bool = True,
        idempotency: bool = True,
    ) -> APIResponse:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return self.api.handle(
            "POST",
            path,
            self.headers(body, csrf=csrf, idempotency=idempotency),
            body,
        )

    def test_settings_routes_require_csrf_and_never_echo_private_key(self) -> None:
        catalog_response = self.api.handle(
            "GET", "/api/v1/integrations", [("Host", self.api.expected_host)]
        )
        self.assertEqual(catalog_response.status, 200)
        self.assertGreater(len(catalog_response.json()["data"]), 20)
        status_response = self.api.handle(
            "GET",
            "/api/v1/integrations/github",
            [("Host", self.api.expected_host)],
        )
        self.assertEqual(status_response.status, 200)
        self.assertFalse(status_response.json()["data"]["configured"])

        payload = command().model_dump(mode="json")
        denied = self.post("/api/v1/integrations/github/test", payload, csrf=False)
        self.assertEqual(denied.status, 400)
        self.assertEqual(denied.json()["error"]["code"], "missing_header")
        tested = self.post("/api/v1/integrations/github/test", payload)
        self.assertEqual(tested.status, 200)
        self.assertNotIn("BEGIN PRIVATE KEY", tested.body.decode("utf-8"))

        connected = self.post("/api/v1/integrations/github", payload)
        self.assertEqual(connected.status, 201)
        self.assertEqual(connected.headers["Idempotency-Replayed"], "false")
        synced = self.post("/api/v1/integrations/github/sync", {})
        self.assertEqual(synced.status, 201)
        self.assertEqual(synced.headers["Idempotency-Replayed"], "false")
        self.assertEqual(synced.json()["data"]["head_sha"], HEAD_SHA)
        self.assertEqual(self.port.sync_count, 1)
        self.assertEqual(len(self.port.commands), 2)

        invalid_sync = self.post("/api/v1/integrations/github/sync", {"extra": True})
        self.assertEqual(invalid_sync.status, 400)

        missing_key = self.post(
            "/api/v1/integrations/github/sync", {}, idempotency=False
        )
        self.assertEqual(missing_key.status, 400)
        self.assertEqual(missing_key.json()["error"]["code"], "missing_header")

    def test_unavailable_integration_fails_closed(self) -> None:
        api = LoopbackAPI(
            ReleaseIntegrationService(self.store, registry(), clock=lambda: NOW),
            port=43128,
            csrf_secret=b"test-secret",
            local_installation_id="installation-1",
        )
        response = api.handle(
            "GET",
            "/api/v1/integrations/github",
            [("Host", api.expected_host)],
        )
        self.assertEqual(response.status, 503)
        self.assertEqual(response.json()["error"]["code"], "integration_unavailable")


if __name__ == "__main__":
    unittest.main()
