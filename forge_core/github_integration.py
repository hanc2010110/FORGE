from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import quote, urlencode

from pydantic import Field, field_validator, model_validator

from forge_core.hashing import canonical_sha256
from forge_core.models import ContractModel

GITHUB_API_VERSION = "2026-03-10"
GITHUB_FILES_PER_PAGE = 100
MAX_GITHUB_FILE_PAGES = 30
GITHUB_ITEMS_PER_PAGE = 100
MAX_GITHUB_PULL_REQUEST_PAGES = 30
MAX_GITHUB_WORKFLOW_RUN_PAGES = 10
MAX_GITHUB_WORKFLOW_RUN_RESULTS = 1_000
DEFAULT_EVIDENCE_FRESHNESS_SECONDS = 900
MAX_PRIVATE_KEY_BYTES = 32_768
MAX_GITHUB_RESPONSE_BYTES = 2_097_152
MAX_GITHUB_IDEMPOTENCY_RECORD_BYTES = MAX_GITHUB_RESPONSE_BYTES * 2 + 16_384
_NAME_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?")
_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class GitHubWorkflowStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    WAITING = "waiting"
    REQUESTED = "requested"
    PENDING = "pending"


class GitHubWorkflowConclusion(StrEnum):
    ACTION_REQUIRED = "action_required"
    CANCELLED = "cancelled"
    FAILURE = "failure"
    NEUTRAL = "neutral"
    SKIPPED = "skipped"
    STALE = "stale"
    SUCCESS = "success"
    TIMED_OUT = "timed_out"


class GitHubIntegrationError(RuntimeError):
    def __init__(self, code: str, public_message: str, *, status: int = 502) -> None:
        super().__init__(code)
        self.code = code
        self.public_message = public_message
        self.status = status


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value


def _fingerprint(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class GitHubConnectCommand(ContractModel):
    app_id: int = Field(gt=0, le=9_223_372_036_854_775_807)
    installation_id: int = Field(gt=0, le=9_223_372_036_854_775_807)
    owner: str = Field(min_length=1, max_length=100)
    repository: str = Field(min_length=1, max_length=100)
    private_key_filename: str = Field(min_length=1, max_length=255)
    private_key_pem: str = Field(min_length=1, max_length=MAX_PRIVATE_KEY_BYTES)

    @field_validator("owner", "repository")
    @classmethod
    def repository_names_must_be_safe(cls, value: str) -> str:
        if _NAME_PATTERN.fullmatch(value) is None:
            raise ValueError("GitHub owner and repository must be safe names")
        return value

    @field_validator("private_key_filename")
    @classmethod
    def private_key_filename_must_be_basename(cls, value: str) -> str:
        if Path(value).name != value or not value.lower().endswith(".pem"):
            raise ValueError("private key filename must be a .pem basename")
        return value

    @field_validator("private_key_pem")
    @classmethod
    def private_key_must_be_unencrypted_pem(cls, value: str) -> str:
        if "\x00" in value or "ENCRYPTED PRIVATE KEY" in value:
            raise ValueError("private key must be an unencrypted PEM")
        valid = (
            value.startswith("-----BEGIN PRIVATE KEY-----")
            and value.rstrip().endswith("-----END PRIVATE KEY-----")
        ) or (
            value.startswith("-----BEGIN RSA PRIVATE KEY-----")
            and value.rstrip().endswith("-----END RSA PRIVATE KEY-----")
        )
        if not valid:
            raise ValueError("private key must be a supported PEM")
        try:
            value.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("private key PEM must be ASCII") from exc
        return value


class GitHubStoredSettings(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    app_id: int = Field(gt=0)
    installation_id: int = Field(gt=0)
    owner: str
    repository: str
    private_key_filename: str
    private_key_file: str
    private_key_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    connected_at: datetime

    @field_validator("owner", "repository")
    @classmethod
    def stored_names_must_be_safe(cls, value: str) -> str:
        if _NAME_PATTERN.fullmatch(value) is None:
            raise ValueError("stored GitHub name is invalid")
        return value

    @field_validator("private_key_file")
    @classmethod
    def key_file_must_be_basename(cls, value: str) -> str:
        if Path(value).name != value or not value.endswith(".pem"):
            raise ValueError("stored private key file is invalid")
        return value

    @field_validator("connected_at")
    @classmethod
    def connected_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value, "connected_at")


class GitHubConnectionResult(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    app_slug: str = Field(min_length=1)
    repository_full_name: str = Field(min_length=3)
    repository_private: bool
    permissions: tuple[str, ...]
    token_expires_at: datetime
    verified_at: datetime
    private_key_filename: str
    private_key_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("permissions")
    @classmethod
    def permissions_must_be_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("permissions must be unique and sorted")
        return value

    @field_validator("token_expires_at", "verified_at")
    @classmethod
    def timestamps_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value, "GitHub timestamp")


class GitHubPullRequestEvidence(ContractModel):
    number: int = Field(gt=0)
    title: str = Field(max_length=500)
    state: Literal["open"]
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    updated_at: datetime
    html_url: str = Field(pattern=r"^https://github\.com/")

    @field_validator("updated_at")
    @classmethod
    def updated_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value, "updated_at")


class GitHubWorkflowRunEvidence(ContractModel):
    run_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=500)
    event: str = Field(min_length=1, max_length=100)
    status: GitHubWorkflowStatus
    conclusion: GitHubWorkflowConclusion | None = None
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    created_at: datetime
    updated_at: datetime
    html_url: str = Field(pattern=r"^https://github\.com/")

    @field_validator("created_at", "updated_at")
    @classmethod
    def workflow_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value, "workflow timestamp")


class _GitHubEvidencePayload(ContractModel):
    schema_version: Literal["1.0.0"]
    source_system: Literal["github"]
    source_api_version: str
    source_endpoints: tuple[str, ...]
    app_id: int
    installation_id: int
    private_key_sha256: str
    repository_full_name: str
    repository_private: bool
    default_branch: str
    head_sha: str
    head_commit_message: str
    head_commit_at: datetime
    changed_files: tuple[str, ...]
    total_changed_files: int = Field(ge=0)
    open_pull_requests: tuple[GitHubPullRequestEvidence, ...]
    workflow_runs: tuple[GitHubWorkflowRunEvidence, ...]
    collected_at: datetime

    @field_validator("head_commit_at", "collected_at")
    @classmethod
    def evidence_timestamps_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value, "evidence timestamp")


class GitHubRepositoryEvidence(_GitHubEvidencePayload):
    evidence_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def evidence_must_be_canonical_and_hash_bound(self) -> GitHubRepositoryEvidence:
        if self.changed_files != tuple(sorted(set(self.changed_files))):
            raise ValueError("changed files must be unique and sorted")
        if self.total_changed_files != len(self.changed_files):
            raise ValueError("total changed files must equal the complete file set")
        if self.open_pull_requests != tuple(
            sorted(self.open_pull_requests, key=lambda item: item.number)
        ):
            raise ValueError("pull requests must be ordered by number")
        if len({item.number for item in self.open_pull_requests}) != len(
            self.open_pull_requests
        ):
            raise ValueError("pull requests must be unique")
        if self.workflow_runs != tuple(
            sorted(self.workflow_runs, key=lambda item: item.run_id, reverse=True)
        ):
            raise ValueError("workflow runs must be ordered newest first")
        if len({item.run_id for item in self.workflow_runs}) != len(self.workflow_runs):
            raise ValueError("workflow runs must be unique")
        if any(item.head_sha != self.head_sha for item in self.workflow_runs):
            raise ValueError("workflow evidence must bind to the exact head commit")
        payload = _GitHubEvidencePayload.model_validate(
            self.model_dump(mode="python", exclude={"evidence_hash"})
        )
        if self.evidence_hash != canonical_sha256(payload):
            raise ValueError("evidence hash does not reproduce GitHub evidence")
        return self


class GitHubIntegrationStatus(ContractModel):
    available: bool = True
    configured: bool
    app_id: int | None = None
    installation_id: int | None = None
    repository_full_name: str | None = None
    private_key_filename: str | None = None
    private_key_sha256: str | None = None
    connected_at: datetime | None = None
    latest_evidence: GitHubRepositoryEvidence | None = None
    latest_evidence_state: Literal["missing", "fresh", "stale"] = "missing"
    latest_evidence_age_seconds: int | None = Field(default=None, ge=0)


class GitHubMutationResult(ContractModel):
    data: dict[str, object]
    replayed: bool = False


class GitHubIdempotencyRecord(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    operation: Literal["connect", "sync"]
    key_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    request_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    state: Literal["prepared", "completed"] = "completed"
    response_json: str = Field(min_length=2, max_length=MAX_GITHUB_RESPONSE_BYTES)
    created_at: datetime

    @field_validator("response_json")
    @classmethod
    def response_must_be_a_json_object(cls, value: str) -> str:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("idempotency response must be valid JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("idempotency response must be a JSON object")
        return value

    @field_validator("created_at")
    @classmethod
    def created_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value, "created_at")

    def response(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.response_json))


class GitHubTransport(Protocol):
    def request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        token: str,
        payload: Mapping[str, object] | None = None,
    ) -> object: ...


class GitHubIntegrationPort(Protocol):
    def status(self) -> dict[str, object]: ...

    def test_connection(self, command: GitHubConnectCommand) -> dict[str, object]: ...

    def connect(
        self, command: GitHubConnectCommand, *, idempotency_key: str
    ) -> GitHubMutationResult: ...

    def sync(self, *, idempotency_key: str) -> GitHubMutationResult: ...


class RS256Signer(Protocol):
    def sign(self, message: bytes, private_key_path: Path) -> bytes: ...


class OpenSSLRS256Signer:
    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable or shutil.which("openssl") or ""

    def sign(self, message: bytes, private_key_path: Path) -> bytes:
        if not self._executable:
            raise GitHubIntegrationError(
                "openssl_unavailable",
                "OpenSSL is required to use the GitHub App private key.",
                status=503,
            )
        try:
            completed = subprocess.run(
                [
                    self._executable,
                    "dgst",
                    "-sha256",
                    "-sign",
                    str(private_key_path),
                ],
                input=message,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitHubIntegrationError(
                "private_key_signing_failed",
                "The GitHub App private key could not be used.",
                status=422,
            ) from exc
        if completed.returncode != 0 or not completed.stdout:
            raise GitHubIntegrationError(
                "private_key_signing_failed",
                "The GitHub App private key could not be used.",
                status=422,
            )
        return completed.stdout


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class UrllibGitHubTransport:
    def __init__(self, *, timeout_seconds: float = 12.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("GitHub timeout must be positive")
        self._timeout_seconds = timeout_seconds
        self._opener = urllib.request.build_opener(_NoRedirect())

    def request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        token: str,
        payload: Mapping[str, object] | None = None,
    ) -> object:
        if (
            not path.startswith("/")
            or path.startswith("//")
            or "\r" in path
            or "\n" in path
            or not token
            or "\r" in token
            or "\n" in token
        ):
            raise ValueError("GitHub request boundary is invalid")
        body = None
        if payload is not None:
            body = json.dumps(
                payload, allow_nan=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.github.com{path}",
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "FORGE-read-only-integration/1.0",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError as exc:
                        raise GitHubIntegrationError(
                            "github_invalid_response",
                            "GitHub returned an invalid response length.",
                        ) from exc
                    if declared_length < 0:
                        raise GitHubIntegrationError(
                            "github_invalid_response",
                            "GitHub returned an invalid response length.",
                        )
                    if declared_length > MAX_GITHUB_RESPONSE_BYTES:
                        raise GitHubIntegrationError(
                            "github_response_too_large",
                            "GitHub returned more data than FORGE accepts.",
                        )
                raw = response.read(MAX_GITHUB_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise GitHubIntegrationError(
                f"github_http_{exc.code}",
                "GitHub rejected the App credentials, repository, or permissions.",
                status=422 if exc.code in {401, 403, 404, 422} else 502,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GitHubIntegrationError(
                "github_unreachable",
                "GitHub could not be reached from this FORGE instance.",
                status=502,
            ) from exc
        if len(raw) > MAX_GITHUB_RESPONSE_BYTES:
            raise GitHubIntegrationError(
                "github_response_too_large",
                "GitHub returned more data than FORGE accepts.",
            )
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubIntegrationError(
                "github_invalid_response",
                "GitHub returned an invalid response.",
            ) from exc
        if not isinstance(decoded, (dict, list)):
            raise GitHubIntegrationError(
                "github_invalid_response",
                "GitHub returned an invalid response.",
            )
        return decoded


class GitHubCredentialStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._keys = self.root / "keys"
        self._evidence = self.root / "evidence"
        self._idempotency = self.root / "idempotency"
        self._settings = self.root / "github.json"

    def _prepare(self) -> None:
        for directory in (self.root, self._keys, self._evidence, self._idempotency):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)

    @staticmethod
    def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
        raw = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        descriptor, candidate = tempfile.mkstemp(prefix=".forge-", dir=path.parent)
        candidate_path = Path(candidate)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(candidate_path, path)
            path.chmod(0o600)
        finally:
            candidate_path.unlink(missing_ok=True)

    def stage_key(self, pem: str) -> tuple[Path, str]:
        self._prepare()
        raw = pem.encode("ascii", errors="strict")
        fingerprint = _fingerprint(raw)
        descriptor, candidate = tempfile.mkstemp(prefix=".key-", dir=self._keys)
        candidate_path = Path(candidate)
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        return candidate_path, fingerprint

    def discard_staged_key(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    def cleanup_staged_keys(self) -> None:
        """Remove key candidates left by an interrupted local connection attempt."""
        self._prepare()
        for path in self._keys.glob(".key-*"):
            if path.is_file() and not path.is_symlink():
                path.unlink(missing_ok=True)

    def cleanup_unreferenced_keys(self) -> None:
        """Remove committed keys that no durable settings record references."""
        self._prepare()
        settings = self.load(required=False)
        referenced = settings.private_key_file if settings is not None else None
        for path in self._keys.glob("*.pem"):
            if path.name != referenced and path.is_file() and not path.is_symlink():
                path.unlink(missing_ok=True)

    def commit(
        self,
        command: GitHubConnectCommand,
        staged_key: Path,
        fingerprint: str,
        *,
        connected_at: datetime,
    ) -> GitHubStoredSettings:
        self._prepare()
        key_name = f"github-app-{fingerprint.removeprefix('sha256:')}.pem"
        key_path = self._keys / key_name
        os.replace(staged_key, key_path)
        key_path.chmod(0o600)
        settings = GitHubStoredSettings(
            app_id=command.app_id,
            installation_id=command.installation_id,
            owner=command.owner,
            repository=command.repository,
            private_key_filename=command.private_key_filename,
            private_key_file=key_name,
            private_key_sha256=fingerprint,
            connected_at=connected_at,
        )
        previous = self.load(required=False)
        self._atomic_json(self._settings, settings.model_dump(mode="json"))
        if previous is not None and previous.private_key_file != key_name:
            (self._keys / previous.private_key_file).unlink(missing_ok=True)
        return settings

    def load(self, *, required: bool = True) -> GitHubStoredSettings | None:
        if not self._settings.is_file():
            if required:
                raise GitHubIntegrationError(
                    "github_not_configured",
                    "Connect a GitHub App in Settings first.",
                    status=409,
                )
            return None
        try:
            raw = self._settings.read_bytes()
            if len(raw) > 16_384:
                raise ValueError("settings too large")
            return GitHubStoredSettings.model_validate_json(raw)
        except (OSError, ValueError) as exc:
            raise GitHubIntegrationError(
                "github_configuration_invalid",
                "Stored GitHub settings failed integrity validation.",
                status=500,
            ) from exc

    def key_path(self, settings: GitHubStoredSettings) -> Path:
        path = (self._keys / settings.private_key_file).resolve()
        if path.parent != self._keys.resolve() or not path.is_file():
            raise GitHubIntegrationError(
                "github_private_key_missing",
                "The stored GitHub App private key is missing.",
                status=409,
            )
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise GitHubIntegrationError(
                "github_private_key_unreadable",
                "The stored GitHub App private key cannot be read.",
                status=409,
            ) from exc
        if _fingerprint(raw) != settings.private_key_sha256:
            raise GitHubIntegrationError(
                "github_private_key_changed",
                "The stored GitHub App private key failed integrity validation.",
                status=409,
            )
        return path

    def save_evidence(self, evidence: GitHubRepositoryEvidence) -> None:
        self._prepare()
        encoded = json.dumps(
            evidence.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_GITHUB_RESPONSE_BYTES:
            raise GitHubIntegrationError(
                "github_evidence_too_large",
                "Complete GitHub evidence exceeds FORGE's safe storage limit.",
                status=422,
            )
        filename = f"{evidence.evidence_hash.removeprefix('sha256:')}.json"
        destination = self._evidence / filename
        if not destination.exists():
            self._atomic_json(destination, evidence.model_dump(mode="json"))
        self._atomic_json(
            self._evidence / "latest.json", evidence.model_dump(mode="json")
        )

    def latest_evidence(self) -> GitHubRepositoryEvidence | None:
        path = self._evidence / "latest.json"
        if not path.is_file():
            return None
        try:
            raw = path.read_bytes()
            if len(raw) > MAX_GITHUB_RESPONSE_BYTES:
                raise ValueError("evidence too large")
            return GitHubRepositoryEvidence.model_validate_json(raw)
        except (OSError, ValueError) as exc:
            raise GitHubIntegrationError(
                "github_evidence_invalid",
                "Stored GitHub evidence failed integrity validation.",
                status=500,
            ) from exc

    @staticmethod
    def _idempotency_fingerprint(operation: str, key: str) -> str:
        return _fingerprint(f"{operation}\0{key}".encode())

    def _idempotency_path(self, operation: str, key: str) -> Path:
        fingerprint = self._idempotency_fingerprint(operation, key)
        return self._idempotency / f"{fingerprint.removeprefix('sha256:')}.json"

    def find_idempotency(
        self,
        operation: Literal["connect", "sync"],
        key: str,
        request_hash: str,
    ) -> GitHubIdempotencyRecord | None:
        path = self._idempotency_path(operation, key)
        if not path.is_file():
            return None
        try:
            raw = path.read_bytes()
            if len(raw) > MAX_GITHUB_IDEMPOTENCY_RECORD_BYTES:
                raise ValueError("idempotency record too large")
            record = GitHubIdempotencyRecord.model_validate_json(raw)
            if (
                record.operation != operation
                or record.key_sha256 != self._idempotency_fingerprint(operation, key)
            ):
                raise ValueError("idempotency record identity mismatch")
            if record.request_hash != request_hash:
                raise GitHubIntegrationError(
                    "github_idempotency_conflict",
                    "The idempotency key was already used for another request.",
                    status=409,
                )
            response = record.response()
            if operation == "connect":
                GitHubConnectionResult.model_validate(response)
            else:
                GitHubRepositoryEvidence.model_validate(response)
            return record
        except GitHubIntegrationError:
            raise
        except (OSError, ValueError) as exc:
            raise GitHubIntegrationError(
                "github_idempotency_invalid",
                "Stored GitHub idempotency state failed integrity validation.",
                status=500,
            ) from exc

    @staticmethod
    def _idempotency_response_json(response: Mapping[str, object]) -> str:
        response_json = json.dumps(
            response,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(response_json.encode("utf-8")) > MAX_GITHUB_RESPONSE_BYTES:
            raise GitHubIntegrationError(
                "github_idempotency_response_too_large",
                "The GitHub mutation result exceeds FORGE's safe storage limit.",
                status=422,
            )
        return response_json

    def prepare_idempotency(
        self,
        operation: Literal["connect", "sync"],
        key: str,
        request_hash: str,
        response: Mapping[str, object],
        *,
        created_at: datetime,
    ) -> None:
        self._prepare()
        response_json = self._idempotency_response_json(response)
        record = GitHubIdempotencyRecord(
            operation=operation,
            key_sha256=self._idempotency_fingerprint(operation, key),
            request_hash=request_hash,
            state="prepared",
            response_json=response_json,
            created_at=created_at,
        )
        existing = self.find_idempotency(operation, key, request_hash)
        if existing is not None:
            if existing.response_json != response_json:
                raise GitHubIntegrationError(
                    "github_idempotency_invalid",
                    "Stored GitHub idempotency state conflicts with its "
                    "mutation result.",
                    status=500,
                )
            return
        self._atomic_json(
            self._idempotency_path(operation, key), record.model_dump(mode="json")
        )

    def complete_idempotency(
        self,
        operation: Literal["connect", "sync"],
        key: str,
        request_hash: str,
    ) -> None:
        record = self.find_idempotency(operation, key, request_hash)
        if record is None:
            raise GitHubIntegrationError(
                "github_idempotency_invalid",
                "Prepared GitHub idempotency state is missing.",
                status=500,
            )
        if record.state == "completed":
            return
        completed = record.model_copy(update={"state": "completed"})
        self._atomic_json(
            self._idempotency_path(operation, key),
            completed.model_dump(mode="json"),
        )


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise GitHubIntegrationError(
            "github_invalid_response", f"GitHub omitted valid {name} data."
        )
    return cast(dict[str, Any], value)


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise GitHubIntegrationError(
            "github_invalid_response", f"GitHub omitted valid {name} data."
        )
    return value


def _integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise GitHubIntegrationError(
            "github_invalid_response", f"GitHub omitted valid {name} data."
        )
    return value


def _nonnegative_integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise GitHubIntegrationError(
            "github_invalid_response", f"GitHub omitted valid {name} data."
        )
    return value


def _idempotency_key(value: str) -> str:
    if _IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is None:
        raise ValueError("GitHub idempotency key must be opaque and safe")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise GitHubIntegrationError(
            "github_invalid_response", f"GitHub omitted valid {name} data."
        )
    return value


def _timestamp(value: object, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_string(value, name).replace("Z", "+00:00"))
        return _utc(parsed, name)
    except ValueError as exc:
        raise GitHubIntegrationError(
            "github_invalid_response", f"GitHub omitted valid {name} data."
        ) from exc


def _workflow_status(value: object) -> GitHubWorkflowStatus:
    status = _string(value, "workflow status")
    try:
        return GitHubWorkflowStatus(status)
    except ValueError as exc:
        raise GitHubIntegrationError(
            "github_invalid_response",
            "GitHub returned an unsupported workflow status.",
        ) from exc


def _workflow_conclusion(value: object) -> GitHubWorkflowConclusion:
    conclusion = _string(value, "workflow conclusion")
    try:
        return GitHubWorkflowConclusion(conclusion)
    except ValueError as exc:
        raise GitHubIntegrationError(
            "github_invalid_response",
            "GitHub returned an unsupported workflow conclusion.",
        ) from exc


class GitHubIntegrationService:
    def __init__(
        self,
        store: GitHubCredentialStore,
        *,
        transport: GitHubTransport | None = None,
        signer: RS256Signer | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        evidence_freshness_seconds: int = DEFAULT_EVIDENCE_FRESHNESS_SECONDS,
    ) -> None:
        if evidence_freshness_seconds <= 0:
            raise ValueError("GitHub evidence freshness must be positive")
        self._store = store
        self._transport = transport or UrllibGitHubTransport()
        self._signer = signer or OpenSSLRS256Signer()
        self._clock = clock
        self._evidence_freshness_seconds = evidence_freshness_seconds
        self._lock = threading.RLock()
        self._store.cleanup_staged_keys()
        self._store.cleanup_unreferenced_keys()

    def _now(self) -> datetime:
        return _utc(self._clock(), "clock")

    def _jwt(self, app_id: int, key_path: Path) -> str:
        now = self._now()
        header = _b64url(b'{"alg":"RS256","typ":"JWT"}')
        payload = _b64url(
            json.dumps(
                {
                    "exp": int(now.timestamp()) + 540,
                    "iat": int(now.timestamp()) - 60,
                    "iss": str(app_id),
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        )
        signing_input = f"{header}.{payload}".encode("ascii")
        return (
            f"{header}.{payload}.{_b64url(self._signer.sign(signing_input, key_path))}"
        )

    def _authenticate(
        self,
        *,
        app_id: int,
        installation_id: int,
        owner: str,
        repository: str,
        key_path: Path,
        key_filename: str,
        key_fingerprint: str,
    ) -> tuple[GitHubConnectionResult, str, Mapping[str, Any]]:
        jwt = self._jwt(app_id, key_path)
        app = _mapping(self._transport.request("GET", "/app", token=jwt), "App")
        if _integer(app.get("id"), "app ID") != app_id:
            raise GitHubIntegrationError(
                "github_app_mismatch",
                "The private key does not belong to the entered GitHub App ID.",
                status=422,
            )
        installation = _mapping(
            self._transport.request(
                "GET", f"/app/installations/{installation_id}", token=jwt
            ),
            "installation",
        )
        if _integer(installation.get("id"), "installation ID") != installation_id:
            raise GitHubIntegrationError(
                "github_installation_mismatch",
                "The entered GitHub installation does not match the App.",
                status=422,
            )
        token_payload = _mapping(
            self._transport.request(
                "POST",
                f"/app/installations/{installation_id}/access_tokens",
                token=jwt,
                payload={
                    "repositories": [repository],
                    "permissions": {
                        "actions": "read",
                        "contents": "read",
                        "pull_requests": "read",
                    },
                },
            ),
            "access token",
        )
        installation_token = _string(token_payload.get("token"), "access token")
        expires_at = _timestamp(token_payload.get("expires_at"), "token expiry")
        permission_map = _mapping(token_payload.get("permissions"), "permissions")
        permissions = tuple(
            sorted(f"{name}:{level}" for name, level in permission_map.items())
        )
        expected_permissions = {
            "actions:read",
            "contents:read",
            "pull_requests:read",
        }
        if not expected_permissions.issubset(permissions):
            raise GitHubIntegrationError(
                "github_permissions_insufficient",
                "Grant read-only Contents, Pull requests, and Actions permissions.",
                status=422,
            )
        repository_data = _mapping(
            self._transport.request(
                "GET",
                f"/repos/{quote(owner)}/{quote(repository)}",
                token=installation_token,
            ),
            "repository",
        )
        full_name = _string(repository_data.get("full_name"), "repository name")
        if full_name.casefold() != f"{owner}/{repository}".casefold():
            raise GitHubIntegrationError(
                "github_repository_mismatch",
                "The installation token is not bound to the requested repository.",
                status=422,
            )
        result = GitHubConnectionResult(
            app_slug=_string(app.get("slug"), "App slug"),
            repository_full_name=full_name,
            repository_private=_boolean(repository_data.get("private"), "visibility"),
            permissions=permissions,
            token_expires_at=expires_at,
            verified_at=self._now(),
            private_key_filename=key_filename,
            private_key_sha256=key_fingerprint,
        )
        return result, installation_token, repository_data

    def status(self) -> dict[str, object]:
        with self._lock:
            settings = self._store.load(required=False)
            if settings is None:
                return GitHubIntegrationStatus(configured=False).model_dump(mode="json")
            self._store.key_path(settings)
            latest_evidence = self._store.latest_evidence()
            if latest_evidence is not None and (
                latest_evidence.app_id != settings.app_id
                or latest_evidence.installation_id != settings.installation_id
                or latest_evidence.private_key_sha256 != settings.private_key_sha256
                or latest_evidence.repository_full_name.casefold()
                != f"{settings.owner}/{settings.repository}".casefold()
            ):
                latest_evidence = None
            evidence_state: Literal["missing", "fresh", "stale"] = "missing"
            evidence_age_seconds: int | None = None
            if latest_evidence is not None:
                raw_age_seconds = int(
                    (self._now() - latest_evidence.collected_at).total_seconds()
                )
                evidence_age_seconds = max(0, raw_age_seconds)
                evidence_state = (
                    "fresh"
                    if 0 <= raw_age_seconds <= self._evidence_freshness_seconds
                    else "stale"
                )
            return GitHubIntegrationStatus(
                configured=True,
                app_id=settings.app_id,
                installation_id=settings.installation_id,
                repository_full_name=f"{settings.owner}/{settings.repository}",
                private_key_filename=settings.private_key_filename,
                private_key_sha256=settings.private_key_sha256,
                connected_at=settings.connected_at,
                latest_evidence=latest_evidence,
                latest_evidence_state=evidence_state,
                latest_evidence_age_seconds=evidence_age_seconds,
            ).model_dump(mode="json")

    def _commit_with_complete_files(
        self,
        *,
        owner: str,
        repository: str,
        default_branch: str,
        token: str,
    ) -> tuple[Mapping[str, Any], tuple[str, ...], tuple[str, ...]]:
        encoded_branch = quote(default_branch, safe="")
        filenames: list[str] = []
        endpoints: list[str] = []
        first_commit: Mapping[str, Any] | None = None
        head_sha: str | None = None

        for page in range(1, MAX_GITHUB_FILE_PAGES + 1):
            query = urlencode({"per_page": GITHUB_FILES_PER_PAGE, "page": page})
            path = f"/repos/{owner}/{repository}/commits/{encoded_branch}?{query}"
            commit = _mapping(
                self._transport.request("GET", path, token=token),
                "commit",
            )
            page_sha = _string(commit.get("sha"), "head commit SHA")
            if _SHA_PATTERN.fullmatch(page_sha) is None:
                raise GitHubIntegrationError(
                    "github_invalid_response", "GitHub returned an invalid commit SHA."
                )
            if head_sha is None:
                head_sha = page_sha
                first_commit = commit
            elif page_sha != head_sha:
                raise GitHubIntegrationError(
                    "github_changed_files_incomplete",
                    "The head commit changed while FORGE collected changed files. "
                    "Sync again.",
                    status=409,
                )

            raw_files = commit.get("files", [])
            if not isinstance(raw_files, list):
                raise GitHubIntegrationError(
                    "github_invalid_response", "GitHub returned invalid changed files."
                )
            filenames.extend(
                _string(_mapping(item, "changed file").get("filename"), "filename")
                for item in raw_files
            )
            endpoints.append(path)
            if len(raw_files) < GITHUB_FILES_PER_PAGE:
                break
        else:
            raise GitHubIntegrationError(
                "github_changed_files_incomplete",
                "The commit exceeds FORGE's complete changed-file collection limit.",
                status=422,
            )

        if first_commit is None or len(filenames) != len(set(filenames)):
            raise GitHubIntegrationError(
                "github_invalid_response",
                "GitHub returned duplicate or incomplete changed-file data.",
            )
        return first_commit, tuple(sorted(filenames)), tuple(endpoints)

    def _pull_requests_with_complete_collection(
        self, *, owner: str, repository: str, token: str
    ) -> tuple[tuple[GitHubPullRequestEvidence, ...], tuple[str, ...]]:
        pull_requests: list[GitHubPullRequestEvidence] = []
        endpoints: list[str] = []
        for page in range(1, MAX_GITHUB_PULL_REQUEST_PAGES + 1):
            query = urlencode(
                {"state": "open", "per_page": GITHUB_ITEMS_PER_PAGE, "page": page}
            )
            path = f"/repos/{owner}/{repository}/pulls?{query}"
            raw_pulls = self._transport.request("GET", path, token=token)
            if not isinstance(raw_pulls, list):
                raise GitHubIntegrationError(
                    "github_invalid_response",
                    "GitHub returned invalid pull requests.",
                )
            for item in raw_pulls:
                item_map = _mapping(item, "pull request")
                pull_requests.append(
                    GitHubPullRequestEvidence(
                        number=_integer(item_map.get("number"), "pull request number"),
                        title=_string(item_map.get("title"), "pull request title")[
                            :500
                        ],
                        state="open",
                        head_sha=_string(
                            _mapping(item_map.get("head"), "pull request head").get(
                                "sha"
                            ),
                            "pull request head SHA",
                        ),
                        base_sha=_string(
                            _mapping(item_map.get("base"), "pull request base").get(
                                "sha"
                            ),
                            "pull request base SHA",
                        ),
                        updated_at=_timestamp(
                            item_map.get("updated_at"), "pull request timestamp"
                        ),
                        html_url=_string(item_map.get("html_url"), "pull request URL"),
                    )
                )
            endpoints.append(path)
            if len(raw_pulls) < GITHUB_ITEMS_PER_PAGE:
                break
        else:
            raise GitHubIntegrationError(
                "github_pull_requests_incomplete",
                "The repository exceeds FORGE's complete pull-request "
                "collection limit.",
                status=422,
            )

        numbers = [item.number for item in pull_requests]
        if len(numbers) != len(set(numbers)):
            raise GitHubIntegrationError(
                "github_pull_requests_incomplete",
                "Open pull requests changed while FORGE collected them. Sync again.",
                status=409,
            )
        return tuple(sorted(pull_requests, key=lambda item: item.number)), tuple(
            endpoints
        )

    def _workflow_runs_with_complete_collection(
        self,
        *,
        owner: str,
        repository: str,
        head_sha: str,
        token: str,
    ) -> tuple[tuple[GitHubWorkflowRunEvidence, ...], tuple[str, ...]]:
        workflow_runs: list[GitHubWorkflowRunEvidence] = []
        endpoints: list[str] = []
        expected_total: int | None = None
        for page in range(1, MAX_GITHUB_WORKFLOW_RUN_PAGES + 1):
            query = urlencode(
                {
                    "head_sha": head_sha,
                    "per_page": GITHUB_ITEMS_PER_PAGE,
                    "page": page,
                }
            )
            path = f"/repos/{owner}/{repository}/actions/runs?{query}"
            response = _mapping(
                self._transport.request("GET", path, token=token),
                "workflow runs",
            )
            total_count = _nonnegative_integer(
                response.get("total_count"), "workflow run total"
            )
            if expected_total is None:
                expected_total = total_count
                if expected_total > MAX_GITHUB_WORKFLOW_RUN_RESULTS:
                    raise GitHubIntegrationError(
                        "github_workflow_runs_incomplete",
                        "The commit exceeds FORGE's complete workflow-run "
                        "collection limit.",
                        status=422,
                    )
            elif total_count != expected_total:
                raise GitHubIntegrationError(
                    "github_workflow_runs_incomplete",
                    "Workflow runs changed while FORGE collected them. Sync again.",
                    status=409,
                )
            raw_runs = response.get("workflow_runs", [])
            if not isinstance(raw_runs, list):
                raise GitHubIntegrationError(
                    "github_invalid_response",
                    "GitHub returned invalid workflow runs.",
                )
            for item in raw_runs:
                item_map = _mapping(item, "workflow run")
                conclusion_value = item_map.get("conclusion")
                workflow_runs.append(
                    GitHubWorkflowRunEvidence(
                        run_id=_integer(item_map.get("id"), "workflow run ID"),
                        name=_string(item_map.get("name"), "workflow name")[:500],
                        event=_string(item_map.get("event"), "workflow event")[:100],
                        status=_workflow_status(item_map.get("status")),
                        conclusion=(
                            _workflow_conclusion(conclusion_value)
                            if conclusion_value is not None
                            else None
                        ),
                        head_sha=_string(item_map.get("head_sha"), "workflow head SHA"),
                        created_at=_timestamp(
                            item_map.get("created_at"),
                            "workflow creation timestamp",
                        ),
                        updated_at=_timestamp(
                            item_map.get("updated_at"),
                            "workflow update timestamp",
                        ),
                        html_url=_string(item_map.get("html_url"), "workflow URL"),
                    )
                )
            endpoints.append(path)
            if len(workflow_runs) == expected_total:
                break
            if len(raw_runs) < GITHUB_ITEMS_PER_PAGE:
                break
        else:
            raise GitHubIntegrationError(
                "github_workflow_runs_incomplete",
                "The commit exceeds FORGE's complete workflow-run collection limit.",
                status=422,
            )

        run_ids = [item.run_id for item in workflow_runs]
        if (
            expected_total is None
            or len(workflow_runs) != expected_total
            or len(run_ids) != len(set(run_ids))
        ):
            raise GitHubIntegrationError(
                "github_workflow_runs_incomplete",
                "GitHub returned incomplete workflow-run data.",
                status=409,
            )
        return tuple(
            sorted(workflow_runs, key=lambda item: item.run_id, reverse=True)
        ), tuple(endpoints)

    @staticmethod
    def _invalid_idempotency() -> GitHubIntegrationError:
        return GitHubIntegrationError(
            "github_idempotency_invalid",
            "Stored GitHub idempotency state failed integrity validation.",
            status=500,
        )

    def _replay_connect(
        self,
        command: GitHubConnectCommand,
        key: str,
        request_hash: str,
        record: GitHubIdempotencyRecord,
    ) -> GitHubMutationResult:
        data = record.response()
        result = GitHubConnectionResult.model_validate(data)
        fingerprint = _fingerprint(command.private_key_pem.encode("ascii"))
        if (
            result.repository_full_name.casefold()
            != f"{command.owner}/{command.repository}".casefold()
            or result.private_key_filename != command.private_key_filename
            or result.private_key_sha256 != fingerprint
            or not {
                "actions:read",
                "contents:read",
                "pull_requests:read",
            }.issubset(result.permissions)
        ):
            raise self._invalid_idempotency()
        if record.state == "prepared":
            staged, staged_fingerprint = self._store.stage_key(command.private_key_pem)
            try:
                if staged_fingerprint != fingerprint:
                    raise self._invalid_idempotency()
                self._store.commit(
                    command,
                    staged,
                    fingerprint,
                    connected_at=result.verified_at,
                )
                self._store.complete_idempotency("connect", key, request_hash)
            finally:
                self._store.discard_staged_key(staged)
        return GitHubMutationResult(data=data, replayed=True)

    def _replay_sync(
        self,
        settings: GitHubStoredSettings,
        key: str,
        request_hash: str,
        record: GitHubIdempotencyRecord,
    ) -> GitHubMutationResult:
        data = record.response()
        evidence = GitHubRepositoryEvidence.model_validate(data)
        if (
            evidence.app_id != settings.app_id
            or evidence.installation_id != settings.installation_id
            or evidence.private_key_sha256 != settings.private_key_sha256
            or evidence.repository_full_name.casefold()
            != f"{settings.owner}/{settings.repository}".casefold()
        ):
            raise self._invalid_idempotency()
        if record.state == "prepared":
            self._store.save_evidence(evidence)
            self._store.complete_idempotency("sync", key, request_hash)
        return GitHubMutationResult(data=data, replayed=True)

    def test_connection(self, command: GitHubConnectCommand) -> dict[str, object]:
        with self._lock:
            staged, fingerprint = self._store.stage_key(command.private_key_pem)
            try:
                result, _token, _repository = self._authenticate(
                    app_id=command.app_id,
                    installation_id=command.installation_id,
                    owner=command.owner,
                    repository=command.repository,
                    key_path=staged,
                    key_filename=command.private_key_filename,
                    key_fingerprint=fingerprint,
                )
                return result.model_dump(mode="json")
            finally:
                self._store.discard_staged_key(staged)

    def connect(
        self, command: GitHubConnectCommand, *, idempotency_key: str
    ) -> GitHubMutationResult:
        with self._lock:
            key = _idempotency_key(idempotency_key)
            request_hash = canonical_sha256(command)
            replay = self._store.find_idempotency("connect", key, request_hash)
            if replay is not None:
                return self._replay_connect(command, key, request_hash, replay)
            staged, fingerprint = self._store.stage_key(command.private_key_pem)
            try:
                result, _token, _repository = self._authenticate(
                    app_id=command.app_id,
                    installation_id=command.installation_id,
                    owner=command.owner,
                    repository=command.repository,
                    key_path=staged,
                    key_filename=command.private_key_filename,
                    key_fingerprint=fingerprint,
                )
                data = result.model_dump(mode="json")
                self._store.prepare_idempotency(
                    "connect",
                    key,
                    request_hash,
                    data,
                    created_at=self._now(),
                )
                self._store.commit(
                    command, staged, fingerprint, connected_at=result.verified_at
                )
                self._store.complete_idempotency("connect", key, request_hash)
                return GitHubMutationResult(data=data)
            finally:
                self._store.discard_staged_key(staged)

    def sync(self, *, idempotency_key: str) -> GitHubMutationResult:
        with self._lock:
            key = _idempotency_key(idempotency_key)
            settings = self._store.load()
            assert settings is not None
            key_path = self._store.key_path(settings)
            request_hash = canonical_sha256(settings)
            replay = self._store.find_idempotency("sync", key, request_hash)
            if replay is not None:
                return self._replay_sync(settings, key, request_hash, replay)
            connection, token, repository_data = self._authenticate(
                app_id=settings.app_id,
                installation_id=settings.installation_id,
                owner=settings.owner,
                repository=settings.repository,
                key_path=key_path,
                key_filename=settings.private_key_filename,
                key_fingerprint=settings.private_key_sha256,
            )
            owner = quote(settings.owner, safe="")
            repository = quote(settings.repository, safe="")
            default_branch = _string(
                repository_data.get("default_branch"), "default branch"
            )
            commit, changed_files, commit_paths = self._commit_with_complete_files(
                owner=owner,
                repository=repository,
                default_branch=default_branch,
                token=token,
            )
            head_sha = _string(commit.get("sha"), "head commit SHA")
            if _SHA_PATTERN.fullmatch(head_sha) is None:
                raise GitHubIntegrationError(
                    "github_invalid_response", "GitHub returned an invalid commit SHA."
                )
            commit_detail = _mapping(commit.get("commit"), "commit")
            committer = _mapping(commit_detail.get("committer"), "commit timestamp")
            head_commit_at = _timestamp(committer.get("date"), "commit timestamp")
            pull_requests, pull_paths = self._pull_requests_with_complete_collection(
                owner=owner,
                repository=repository,
                token=token,
            )
            workflow_runs, run_paths = self._workflow_runs_with_complete_collection(
                owner=owner,
                repository=repository,
                head_sha=head_sha,
                token=token,
            )
            endpoints = (
                "/app",
                f"/app/installations/{settings.installation_id}",
                f"/repos/{settings.owner}/{settings.repository}",
                *commit_paths,
                *pull_paths,
                *run_paths,
            )
            payload = _GitHubEvidencePayload(
                schema_version="1.0.0",
                source_system="github",
                source_api_version=GITHUB_API_VERSION,
                source_endpoints=endpoints,
                app_id=settings.app_id,
                installation_id=settings.installation_id,
                private_key_sha256=settings.private_key_sha256,
                repository_full_name=connection.repository_full_name,
                repository_private=connection.repository_private,
                default_branch=default_branch,
                head_sha=head_sha,
                head_commit_message=_string(
                    commit_detail.get("message"), "commit message"
                )[:2000],
                head_commit_at=head_commit_at,
                changed_files=changed_files,
                total_changed_files=len(changed_files),
                open_pull_requests=pull_requests,
                workflow_runs=workflow_runs,
                collected_at=self._now(),
            )
            evidence = GitHubRepositoryEvidence(
                **payload.model_dump(mode="python"),
                evidence_hash=canonical_sha256(payload),
            )
            data = evidence.model_dump(mode="json")
            self._store.prepare_idempotency(
                "sync",
                key,
                request_hash,
                data,
                created_at=self._now(),
            )
            self._store.save_evidence(evidence)
            self._store.complete_idempotency("sync", key, request_hash)
            return GitHubMutationResult(data=data)


__all__ = [
    "DEFAULT_EVIDENCE_FRESHNESS_SECONDS",
    "GITHUB_API_VERSION",
    "GITHUB_FILES_PER_PAGE",
    "GITHUB_ITEMS_PER_PAGE",
    "MAX_GITHUB_FILE_PAGES",
    "MAX_GITHUB_PULL_REQUEST_PAGES",
    "MAX_GITHUB_WORKFLOW_RUN_PAGES",
    "MAX_GITHUB_WORKFLOW_RUN_RESULTS",
    "GitHubConnectCommand",
    "GitHubConnectionResult",
    "GitHubCredentialStore",
    "GitHubIntegrationError",
    "GitHubIntegrationPort",
    "GitHubIntegrationService",
    "GitHubIntegrationStatus",
    "GitHubIdempotencyRecord",
    "GitHubMutationResult",
    "GitHubRepositoryEvidence",
    "GitHubWorkflowConclusion",
    "GitHubWorkflowStatus",
    "GitHubTransport",
    "OpenSSLRS256Signer",
    "RS256Signer",
    "UrllibGitHubTransport",
]
