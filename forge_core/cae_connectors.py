from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Literal, Protocol, cast
from urllib.parse import quote

from pydantic import Field, field_serializer, field_validator, model_validator

from forge_core.immutable_json import freeze_json_mapping, thaw_json
from forge_core.models import ContractModel

MAX_CAE_REQUEST_BYTES = 512_000
MAX_CAE_RESPONSE_BYTES = 2_000_000
DEFAULT_CAE_TIMEOUT_SECONDS = 20.0
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SAFE_FUNCTION = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}")
_HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"

CAEProvider = Literal["simscale", "matlab-production-server", "ansys"]
CAEOperation = Literal["submit", "start", "status", "result", "execute"]
CAEApprovalOperation = Literal["submit_and_start", "execute"]
CAEApprovalPhase = Literal["submit", "start", "execute"]


class RedactedHeaders(dict[str, str]):
    def __repr__(self) -> str:
        secret_names = {"authorization", "x-api-key"}
        redacted = {
            key: "<redacted>"
            if key.casefold() in secret_names or "token" in key.casefold()
            else value
            for key, value in self.items()
        }
        return repr(redacted)


class CAEConnectorError(RuntimeError):
    def __init__(self, code: str, public_message: str, *, status: int = 502) -> None:
        super().__init__(code)
        self.code = code
        self.public_message = public_message
        self.status = status


class CAEHTTPTransport(Protocol):
    def request(
        self,
        method: Literal["GET", "POST"],
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> tuple[int, dict[str, str], bytes]: ...


class CAEApprovalRef(ContractModel):
    approval_id: str = Field(min_length=1)
    provider: Literal["simscale", "matlab-production-server"]
    operation: CAEApprovalOperation
    project_id: str = Field(min_length=1)
    simulation_id: str = Field(min_length=1)
    candidate_hash: str = Field(pattern=_HASH_PATTERN)
    scenario_hash: str = Field(pattern=_HASH_PATTERN)
    execution_key: str = Field(min_length=8, max_length=128)
    request_hash: str = Field(pattern=_HASH_PATTERN)
    approved_by: str = Field(min_length=1)
    approved_at: datetime

    @field_validator("approval_id", "project_id", "simulation_id", "execution_key")
    @classmethod
    def ids_must_be_safe(cls, value: str) -> str:
        return _safe_id(value, "approval identifiers")

    @field_validator("approved_at")
    @classmethod
    def approved_at_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value, "approved_at")


class CAEApprovalVerifier(Protocol):
    def begin_once(
        self, approval: CAEApprovalRef, *, phase: CAEApprovalPhase
    ) -> Mapping[str, object] | None: ...

    def complete_once(
        self,
        approval: CAEApprovalRef,
        *,
        phase: CAEApprovalPhase,
        result: ContractModel,
    ) -> None: ...

    def assert_consumed(
        self, approval: CAEApprovalRef, *, phase: CAEApprovalPhase
    ) -> Mapping[str, object]: ...


class InMemoryCAEApprovalStore:
    """Process-local trusted approval registry for pilot deployments and tests."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._registered: dict[str, CAEApprovalRef] = {}
        self._phases: dict[tuple[str, CAEApprovalPhase], dict[str, object] | None] = {}

    def register(self, approval: CAEApprovalRef) -> None:
        detached = CAEApprovalRef.model_validate(approval.model_dump(mode="python"))
        with self._lock:
            existing = self._registered.get(detached.approval_id)
            if existing is not None and existing != detached:
                raise ValueError(
                    "CAE approval ID is already bound to another execution"
                )
            self._registered[detached.approval_id] = detached

    def begin_once(
        self, approval: CAEApprovalRef, *, phase: CAEApprovalPhase
    ) -> Mapping[str, object] | None:
        with self._lock:
            self._assert_registered(approval)
            key = (approval.approval_id, phase)
            if key not in self._phases:
                self._phases[key] = None
                return None
            cached = self._phases[key]
            return None if cached is None else dict(cached)

    def complete_once(
        self,
        approval: CAEApprovalRef,
        *,
        phase: CAEApprovalPhase,
        result: ContractModel,
    ) -> None:
        payload = result.model_dump(mode="json")
        with self._lock:
            self._assert_registered(approval)
            key = (approval.approval_id, phase)
            if key not in self._phases:
                raise ValueError("CAE approval phase has not begun")
            existing = self._phases[key]
            if existing is not None and existing != payload:
                raise ValueError("CAE approval phase is bound to another result")
            self._phases[key] = payload

    def assert_consumed(
        self, approval: CAEApprovalRef, *, phase: CAEApprovalPhase
    ) -> Mapping[str, object]:
        with self._lock:
            self._assert_registered(approval)
            key = (approval.approval_id, phase)
            if key not in self._phases:
                raise ValueError("CAE approval phase has not begun")
            result = self._phases[key]
            if result is None:
                raise ValueError("CAE approval phase has not completed")
            return dict(result)

    def _assert_registered(self, approval: CAEApprovalRef) -> None:
        registered = self._registered.get(approval.approval_id)
        if registered is None or registered != approval:
            raise ValueError("CAE approval is not registered by the trusted authority")


class SQLiteCAEApprovalStore:
    """Durable CAE approval and provider-result ledger for restart-safe retries."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS cae_approvals(
                    approval_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    execution_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cae_execution_phases(
                    provider TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    execution_key TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    approval_id TEXT NOT NULL REFERENCES cae_approvals(approval_id),
                    status TEXT NOT NULL CHECK(status IN ('prepared', 'completed')),
                    result_json TEXT,
                    result_hash TEXT,
                    provider_response_hash TEXT,
                    receipt_hash TEXT,
                    receipt_json TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(provider, request_hash, execution_key, phase)
                );
                """
            )

    def register(self, approval: CAEApprovalRef) -> None:
        detached = CAEApprovalRef.model_validate(approval.model_dump(mode="python"))
        payload = _canonical_model_json(detached)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_json FROM cae_approvals WHERE approval_id = ?",
                (detached.approval_id,),
            ).fetchone()
            if row is not None and str(row["payload_json"]) != payload:
                raise ValueError(
                    "CAE approval ID is already bound to another execution"
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO cae_approvals(
                    approval_id, provider, request_hash, execution_key, payload_json
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    detached.approval_id,
                    detached.provider,
                    detached.request_hash,
                    detached.execution_key,
                    payload,
                ),
            )
            connection.commit()

    def begin_once(
        self, approval: CAEApprovalRef, *, phase: CAEApprovalPhase
    ) -> Mapping[str, object] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_registered(connection, approval)
            row = connection.execute(
                """
                SELECT approval_id, status, result_json, result_hash,
                    provider_response_hash, receipt_hash, receipt_json
                FROM cae_execution_phases
                WHERE provider = ? AND request_hash = ?
                    AND execution_key = ? AND phase = ?
                """,
                (
                    approval.provider,
                    approval.request_hash,
                    approval.execution_key,
                    phase,
                ),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO cae_execution_phases(
                        provider, request_hash, execution_key, phase, approval_id,
                        status, result_json, result_hash, provider_response_hash,
                        receipt_json, updated_at
                    ) VALUES(?, ?, ?, ?, ?, 'prepared', NULL, NULL, NULL, NULL, ?)
                    """,
                    (
                        approval.provider,
                        approval.request_hash,
                        approval.execution_key,
                        phase,
                        approval.approval_id,
                        _utc_text(self._clock()),
                    ),
                )
                connection.commit()
                return None
            if row["approval_id"] != approval.approval_id:
                raise ValueError("CAE execution key is bound to another approval")
            connection.commit()
            if row["status"] != "completed":
                return None
            return _validated_completed_payload(row, approval=approval, phase=phase)

    def complete_once(
        self,
        approval: CAEApprovalRef,
        *,
        phase: CAEApprovalPhase,
        result: ContractModel,
    ) -> None:
        result_json = _canonical_model_json(result)
        result_hash = f"sha256:{hashlib.sha256(result_json.encode()).hexdigest()}"
        provider_response_hash = _result_provider_response_hash(result)
        completed_at = self._clock()
        receipt = {
            "approval_id": approval.approval_id,
            "provider": approval.provider,
            "request_hash": approval.request_hash,
            "execution_key": approval.execution_key,
            "phase": phase,
            "result_hash": result_hash,
            "provider_response_hash": provider_response_hash,
            "completed_at": _utc_text(completed_at),
        }
        receipt_json = json.dumps(
            receipt,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        receipt_hash = f"sha256:{hashlib.sha256(receipt_json.encode()).hexdigest()}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_registered(connection, approval)
            row = connection.execute(
                """
                SELECT status, result_json, result_hash, provider_response_hash,
                    receipt_hash, receipt_json
                FROM cae_execution_phases
                WHERE provider = ? AND request_hash = ?
                    AND execution_key = ? AND phase = ?
                """,
                (
                    approval.provider,
                    approval.request_hash,
                    approval.execution_key,
                    phase,
                ),
            ).fetchone()
            if row is None:
                raise ValueError("CAE approval phase has not begun")
            if row["status"] == "completed":
                if (
                    row["result_hash"] != result_hash
                    or row["result_json"] != result_json
                ):
                    raise ValueError("CAE approval phase is bound to another result")
                _validated_completed_payload(row, approval=approval, phase=phase)
                connection.commit()
                return
            connection.execute(
                """
                UPDATE cae_execution_phases
                SET status = 'completed', result_json = ?, result_hash = ?,
                    provider_response_hash = ?, receipt_hash = ?, receipt_json = ?,
                    updated_at = ?
                WHERE provider = ? AND request_hash = ?
                    AND execution_key = ? AND phase = ?
                """,
                (
                    result_json,
                    result_hash,
                    provider_response_hash,
                    receipt_hash,
                    receipt_json,
                    _utc_text(completed_at),
                    approval.provider,
                    approval.request_hash,
                    approval.execution_key,
                    phase,
                ),
            )
            connection.commit()

    def assert_consumed(
        self, approval: CAEApprovalRef, *, phase: CAEApprovalPhase
    ) -> Mapping[str, object]:
        with self._connect() as connection:
            self._assert_registered(connection, approval)
            row = connection.execute(
                """
                SELECT status, result_json, result_hash, provider_response_hash,
                    receipt_hash, receipt_json
                FROM cae_execution_phases
                WHERE provider = ? AND request_hash = ?
                    AND execution_key = ? AND phase = ?
                """,
                (
                    approval.provider,
                    approval.request_hash,
                    approval.execution_key,
                    phase,
                ),
            ).fetchone()
        if row is None:
            raise ValueError("CAE approval phase has not begun")
        if row["status"] != "completed":
            raise ValueError("CAE approval phase has not completed")
        return _validated_completed_payload(row, approval=approval, phase=phase)

    @staticmethod
    def _assert_registered(
        connection: sqlite3.Connection, approval: CAEApprovalRef
    ) -> None:
        row = connection.execute(
            "SELECT payload_json FROM cae_approvals WHERE approval_id = ?",
            (approval.approval_id,),
        ).fetchone()
        if row is None or str(row["payload_json"]) != _canonical_model_json(approval):
            raise ValueError("CAE approval is not registered by the trusted authority")


def _validated_completed_payload(
    row: sqlite3.Row,
    *,
    approval: CAEApprovalRef,
    phase: CAEApprovalPhase,
) -> dict[str, object]:
    result_json = row["result_json"]
    if not isinstance(result_json, str):
        raise ValueError("completed CAE execution is missing its result")
    decoded = json.loads(result_json)
    if not isinstance(decoded, dict):
        raise ValueError("stored CAE result must be a JSON object")
    expected_result_hash = f"sha256:{hashlib.sha256(result_json.encode()).hexdigest()}"
    if row["result_hash"] != expected_result_hash:
        raise ValueError("stored CAE result hash does not reproduce")
    provider_response_hash = _decoded_provider_response_hash(decoded)
    if row["provider_response_hash"] != provider_response_hash:
        raise ValueError("stored CAE provider receipt does not match result")
    receipt_json = row["receipt_json"]
    if not isinstance(receipt_json, str):
        raise ValueError("completed CAE execution is missing its receipt")
    expected_receipt_hash = (
        f"sha256:{hashlib.sha256(receipt_json.encode()).hexdigest()}"
    )
    if row["receipt_hash"] != expected_receipt_hash:
        raise ValueError("stored CAE execution receipt hash does not reproduce")
    decoded_receipt = json.loads(receipt_json)
    if not isinstance(decoded_receipt, dict):
        raise ValueError("stored CAE execution receipt must be a JSON object")
    expected_receipt_fields = {
        "approval_id": approval.approval_id,
        "provider": approval.provider,
        "request_hash": approval.request_hash,
        "execution_key": approval.execution_key,
        "phase": phase,
        "result_hash": expected_result_hash,
        "provider_response_hash": provider_response_hash,
    }
    if any(
        decoded_receipt.get(key) != value
        for key, value in expected_receipt_fields.items()
    ) or not isinstance(decoded_receipt.get("completed_at"), str):
        raise ValueError("stored CAE execution receipt bindings do not match")
    return cast(dict[str, object], decoded)


class CAEEvidenceMetadata(ContractModel):
    provider: CAEProvider
    operation: CAEOperation
    source_url: str = Field(min_length=1)
    collected_at: datetime
    request_hash: str = Field(pattern=_HASH_PATTERN)
    response_hash: str = Field(pattern=_HASH_PATTERN)
    connector_version: str = "1.0.0"

    @field_validator("collected_at")
    @classmethod
    def collected_at_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value, "collected_at")


class CAESubmitRequest(ContractModel):
    project_id: str = Field(min_length=1)
    simulation_id: str = Field(min_length=1)
    candidate_hash: str = Field(pattern=_HASH_PATTERN)
    scenario_hash: str = Field(pattern=_HASH_PATTERN)
    idempotency_key: str = Field(min_length=8, max_length=128)
    approval: CAEApprovalRef
    payload: Mapping[str, object]

    @field_validator("project_id", "simulation_id", "idempotency_key")
    @classmethod
    def ids_must_be_safe(cls, value: str) -> str:
        return _safe_id(value, "simulation identifiers")

    @field_validator("payload", mode="after")
    @classmethod
    def freeze_payload(cls, value: Mapping[str, object]) -> Mapping[str, object]:
        _json_body(value)
        return freeze_json_mapping(value)

    @field_serializer("payload")
    def serialize_payload(self, value: Mapping[str, object]) -> dict[str, object]:
        return cast(dict[str, object], thaw_json(value))

    @model_validator(mode="after")
    def approval_must_cover_exact_request(self) -> CAESubmitRequest:
        if self.approval.provider != "simscale":
            raise ValueError("CAE approval provider does not match request")
        if self.approval.operation != "submit_and_start":
            raise ValueError("CAE approval operation does not match request")
        if self.approval.project_id != self.project_id:
            raise ValueError("CAE approval project does not match request")
        if self.approval.simulation_id != self.simulation_id:
            raise ValueError("CAE approval simulation does not match request")
        if self.approval.candidate_hash != self.candidate_hash:
            raise ValueError("CAE approval candidate does not match request")
        if self.approval.scenario_hash != self.scenario_hash:
            raise ValueError("CAE approval scenario does not match request")
        if self.approval.execution_key != self.idempotency_key:
            raise ValueError("CAE approval execution key does not match request")
        expected_hash = cae_execution_request_hash(
            provider="simscale",
            operation="submit_and_start",
            project_id=self.project_id,
            simulation_id=self.simulation_id,
            candidate_hash=self.candidate_hash,
            scenario_hash=self.scenario_hash,
            execution_key=self.idempotency_key,
            target=self.simulation_id,
            payload=self.payload,
        )
        if self.approval.request_hash != expected_hash:
            raise ValueError("CAE approval does not cover exact request hash")
        return self


class CAERunRef(ContractModel):
    provider: CAEProvider
    project_id: str = Field(min_length=1)
    simulation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    candidate_hash: str = Field(pattern=_HASH_PATTERN)
    scenario_hash: str = Field(pattern=_HASH_PATTERN)
    approval_id: str = Field(min_length=1)
    approval_request_hash: str = Field(pattern=_HASH_PATTERN)
    execution_key: str = Field(min_length=8, max_length=128)
    submitted_at: datetime
    evidence: CAEEvidenceMetadata

    @field_validator("project_id", "simulation_id", "run_id", "execution_key")
    @classmethod
    def ids_must_be_safe(cls, value: str) -> str:
        return _safe_id(value, "run identifiers")

    @field_validator("submitted_at")
    @classmethod
    def submitted_at_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value, "submitted_at")


class CAEReadResult(ContractModel):
    provider: CAEProvider
    operation: CAEOperation
    project_id: str = Field(min_length=1)
    simulation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    candidate_hash: str = Field(pattern=_HASH_PATTERN)
    scenario_hash: str = Field(pattern=_HASH_PATTERN)
    approval_request_hash: str = Field(pattern=_HASH_PATTERN)
    payload: Mapping[str, object]
    evidence: CAEEvidenceMetadata

    @field_validator("payload", mode="after")
    @classmethod
    def freeze_payload(cls, value: Mapping[str, object]) -> Mapping[str, object]:
        return freeze_json_mapping(value)

    @field_serializer("payload")
    def serialize_payload(self, value: Mapping[str, object]) -> dict[str, object]:
        return cast(dict[str, object], thaw_json(value))


class AnsysUnavailableConnector(ContractModel):
    provider: Literal["ansys"] = "ansys"
    status: Literal["adapter_required"] = "adapter_required"
    reason: str = (
        "Ansys product APIs and endpoints are version, license, and deployment "
        "specific; FORGE requires a site-specific adapter before it can collect "
        "Ansys CAE evidence."
    )
    required_inputs: tuple[str, ...] = (
        "product_or_platform",
        "version",
        "base_url",
        "authentication_method",
        "permitted_read_endpoints",
    )


class SimScaleCAEClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        approval_verifier: CAEApprovalVerifier,
        transport: CAEHTTPTransport,
        clock: Callable[[], datetime],
        timeout_seconds: float = DEFAULT_CAE_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = _base_url(base_url)
        self._api_key = _secret(api_key, "SimScale API key")
        self._approval_verifier = approval_verifier
        self._transport = transport
        self._clock = clock
        self._timeout_seconds = _timeout(timeout_seconds)

    def submit_run(self, request: CAESubmitRequest) -> CAERunRef:
        request = CAESubmitRequest.model_validate(request.model_dump(mode="python"))
        cached = self._approval_verifier.begin_once(request.approval, phase="submit")
        if cached is not None:
            return CAERunRef.model_validate(cached)
        path = (
            f"/v1/projects/{_q(request.project_id)}"
            f"/simulations/{_q(request.simulation_id)}/runs"
        )
        body = _json_body(
            {
                "candidate_hash": request.candidate_hash,
                "scenario_hash": request.scenario_hash,
                "payload": thaw_json(request.payload),
            }
        )
        decoded, evidence = self._send(
            "POST",
            path,
            headers={"Idempotency-Key": request.idempotency_key},
            body=body,
            operation="submit",
        )
        run_id = _extract_string(decoded, ("runId", "jobId", "id"))
        run = CAERunRef(
            provider="simscale",
            project_id=request.project_id,
            simulation_id=request.simulation_id,
            run_id=run_id,
            candidate_hash=request.candidate_hash,
            scenario_hash=request.scenario_hash,
            approval_id=request.approval.approval_id,
            approval_request_hash=request.approval.request_hash,
            execution_key=request.idempotency_key,
            submitted_at=evidence.collected_at,
            evidence=evidence,
        )
        self._approval_verifier.complete_once(
            request.approval, phase="submit", result=run
        )
        return run

    def start_run(
        self,
        run: CAERunRef,
        *,
        approval: CAEApprovalRef,
        idempotency_key: str,
    ) -> CAEReadResult:
        if run.provider != "simscale":
            raise ValueError("SimScale client can only start SimScale run references")
        if approval.project_id != run.project_id:
            raise ValueError("CAE approval project does not match run")
        if approval.provider != "simscale" or approval.operation != "submit_and_start":
            raise ValueError("CAE approval operation does not match run")
        if approval.simulation_id != run.simulation_id:
            raise ValueError("CAE approval simulation does not match run")
        if approval.candidate_hash != run.candidate_hash:
            raise ValueError("CAE approval candidate does not match run")
        if approval.scenario_hash != run.scenario_hash:
            raise ValueError("CAE approval scenario does not match run")
        if approval.approval_id != run.approval_id:
            raise ValueError("CAE approval identity does not match run")
        if approval.request_hash != run.approval_request_hash:
            raise ValueError("CAE approval request hash does not match run")
        safe_key = _safe_id(idempotency_key, "idempotency key")
        if approval.execution_key != safe_key or run.execution_key != safe_key:
            raise ValueError("CAE start must use the approved execution key")
        completed_submit = CAERunRef.model_validate(
            self._approval_verifier.assert_consumed(approval, phase="submit")
        )
        if completed_submit != run:
            raise ValueError("CAE run does not match the completed submit result")
        run = completed_submit
        cached = self._approval_verifier.begin_once(approval, phase="start")
        if cached is not None:
            return CAEReadResult.model_validate(cached)
        path = (
            f"/v1/projects/{_q(run.project_id)}"
            f"/simulations/{_q(run.simulation_id)}/runs/{_q(run.run_id)}/start"
        )
        decoded, evidence = self._send(
            "POST",
            path,
            headers={"Idempotency-Key": safe_key},
            body=b"{}",
            operation="start",
        )
        result = CAEReadResult(
            provider="simscale",
            operation="start",
            project_id=run.project_id,
            simulation_id=run.simulation_id,
            run_id=run.run_id,
            candidate_hash=run.candidate_hash,
            scenario_hash=run.scenario_hash,
            approval_request_hash=run.approval_request_hash,
            payload=decoded,
            evidence=evidence,
        )
        self._approval_verifier.complete_once(approval, phase="start", result=result)
        return result

    def get_status(self, run: CAERunRef) -> CAEReadResult:
        return self._read_run(run, operation="status", suffix="")

    def get_result(self, run: CAERunRef) -> CAEReadResult:
        return self._read_run(run, operation="result", suffix="/results")

    def _read_run(
        self, run: CAERunRef, *, operation: Literal["status", "result"], suffix: str
    ) -> CAEReadResult:
        if run.provider != "simscale":
            raise ValueError("SimScale client can only read SimScale run references")
        path = (
            f"/v1/projects/{_q(run.project_id)}"
            f"/simulations/{_q(run.simulation_id)}/runs/{_q(run.run_id)}{suffix}"
        )
        decoded, evidence = self._send(
            "GET", path, headers={}, body=None, operation=operation
        )
        return CAEReadResult(
            provider="simscale",
            operation=operation,
            project_id=run.project_id,
            simulation_id=run.simulation_id,
            run_id=run.run_id,
            candidate_hash=run.candidate_hash,
            scenario_hash=run.scenario_hash,
            approval_request_hash=run.approval_request_hash,
            payload=decoded,
            evidence=evidence,
        )

    def _send(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        operation: CAEOperation,
    ) -> tuple[dict[str, object], CAEEvidenceMetadata]:
        merged = RedactedHeaders(
            {
                "X-API-KEY": self._api_key,
                "Accept": "application/json",
                **dict(headers),
            }
        )
        if body is not None:
            merged["Content-Type"] = "application/json"
        url = f"{self._base_url}{path}"
        status, _response_headers, raw = self._transport.request(
            method,
            url,
            headers=merged,
            body=body,
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=MAX_CAE_RESPONSE_BYTES,
        )
        decoded = _decode(status, raw)
        evidence = _evidence(
            provider="simscale",
            operation=operation,
            source_url=url,
            request_body=body,
            response_body=raw,
            collected_at=_utc(self._clock(), "collected_at"),
        )
        return decoded, evidence


class MATLABProductionServerClient:
    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        application: str,
        allowlisted_functions: tuple[str, ...],
        approval_verifier: CAEApprovalVerifier,
        transport: CAEHTTPTransport,
        clock: Callable[[], datetime],
        timeout_seconds: float = DEFAULT_CAE_TIMEOUT_SECONDS,
    ) -> None:
        if not allowlisted_functions:
            raise ValueError("MATLAB adapter requires an allowlist")
        self._base_url = _base_url(base_url)
        self._bearer_token = _secret(bearer_token, "MATLAB bearer token")
        self._application = _safe_id(application, "MATLAB application")
        self._allowlisted_functions = frozenset(
            _safe_function(item) for item in allowlisted_functions
        )
        self._approval_verifier = approval_verifier
        self._transport = transport
        self._clock = clock
        self._timeout_seconds = _timeout(timeout_seconds)

    def execute_allowlisted_function(
        self,
        *,
        function_name: str,
        project_id: str,
        simulation_id: str,
        run_id: str,
        candidate_hash: str,
        scenario_hash: str,
        idempotency_key: str,
        approval: CAEApprovalRef,
        payload: Mapping[str, object],
    ) -> CAEReadResult:
        safe_function = _safe_function(function_name)
        if safe_function not in self._allowlisted_functions:
            raise ValueError("MATLAB function is not allowlisted for FORGE")
        safe_project = _safe_id(project_id, "project id")
        safe_simulation = _safe_id(simulation_id, "simulation id")
        safe_run = _safe_id(run_id, "run id")
        safe_key = _safe_id(idempotency_key, "idempotency key")
        expected_hash = cae_execution_request_hash(
            provider="matlab-production-server",
            operation="execute",
            project_id=safe_project,
            simulation_id=safe_simulation,
            candidate_hash=candidate_hash,
            scenario_hash=scenario_hash,
            execution_key=safe_key,
            target=f"{self._application}:{safe_function}:{safe_run}",
            payload=payload,
        )
        if approval.provider != "matlab-production-server":
            raise ValueError("CAE approval provider does not match request")
        if approval.operation != "execute":
            raise ValueError("CAE approval operation does not match request")
        if approval.project_id != safe_project:
            raise ValueError("CAE approval project does not match request")
        if approval.simulation_id != safe_simulation:
            raise ValueError("CAE approval simulation does not match request")
        if approval.candidate_hash != candidate_hash:
            raise ValueError("CAE approval candidate does not match request")
        if approval.scenario_hash != scenario_hash:
            raise ValueError("CAE approval scenario does not match request")
        if approval.execution_key != safe_key:
            raise ValueError("CAE approval execution key does not match request")
        if approval.request_hash != expected_hash:
            raise ValueError("CAE approval does not cover exact request hash")
        cached = self._approval_verifier.begin_once(approval, phase="execute")
        if cached is not None:
            return CAEReadResult.model_validate(cached)
        body = _json_body(cast(Mapping[str, object], thaw_json(payload)))
        path = f"/{_q(self._application)}/{_q(safe_function)}"
        url = f"{self._base_url}{path}"
        status, _headers, raw = self._transport.request(
            "POST",
            url,
            headers=RedactedHeaders(
                {
                    "Authorization": f"Bearer {self._bearer_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Idempotency-Key": safe_key,
                }
            ),
            body=body,
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=MAX_CAE_RESPONSE_BYTES,
        )
        decoded = _decode(status, raw)
        evidence = _evidence(
            provider="matlab-production-server",
            operation="execute",
            source_url=url,
            request_body=body,
            response_body=raw,
            collected_at=_utc(self._clock(), "collected_at"),
        )
        result = CAEReadResult(
            provider="matlab-production-server",
            operation="execute",
            project_id=safe_project,
            simulation_id=safe_simulation,
            run_id=safe_run,
            candidate_hash=candidate_hash,
            scenario_hash=scenario_hash,
            approval_request_hash=approval.request_hash,
            payload=decoded,
            evidence=evidence,
        )
        self._approval_verifier.complete_once(approval, phase="execute", result=result)
        return result


def ansys_connector_status() -> AnsysUnavailableConnector:
    return AnsysUnavailableConnector()


def cae_execution_request_hash(
    *,
    provider: Literal["simscale", "matlab-production-server"],
    operation: CAEApprovalOperation,
    project_id: str,
    simulation_id: str,
    candidate_hash: str,
    scenario_hash: str,
    execution_key: str,
    target: str,
    payload: Mapping[str, object],
) -> str:
    """Hash every operator-approved CAE execution input except transport secrets."""
    raw = _json_body(
        {
            "provider": provider,
            "operation": operation,
            "project_id": project_id,
            "simulation_id": simulation_id,
            "candidate_hash": candidate_hash,
            "scenario_hash": scenario_hash,
            "execution_key": execution_key,
            "target": target,
            "payload": thaw_json(payload),
        }
    )
    return _sha(raw)


def _canonical_model_json(value: ContractModel) -> str:
    return json.dumps(
        value.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _utc_text(value: datetime) -> str:
    return (
        _utc(value, "CAE ledger timestamp")
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _result_provider_response_hash(result: ContractModel) -> str:
    payload = result.model_dump(mode="json")
    return _decoded_provider_response_hash(payload)


def _decoded_provider_response_hash(payload: Mapping[str, object]) -> str:
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("CAE result is missing provider evidence")
    response_hash = evidence.get("response_hash")
    if (
        not isinstance(response_hash, str)
        or re.fullmatch(_HASH_PATTERN, response_hash) is None
    ):
        raise ValueError("CAE result is missing provider response hash")
    return response_hash


def _base_url(value: str) -> str:
    trimmed = value.rstrip("/")
    if not trimmed.startswith("https://") or "\r" in trimmed or "\n" in trimmed:
        raise ValueError("CAE connector base URL must be https")
    return trimmed


def _secret(value: str, name: str) -> str:
    if not value or "\r" in value or "\n" in value:
        raise ValueError(f"{name} boundary is invalid")
    return value


def _timeout(value: float) -> float:
    if value <= 0 or value > 60:
        raise ValueError("CAE timeout is outside supported bounds")
    return value


def _safe_id(value: str, name: str) -> str:
    if _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a safe identifier")
    return value


def _safe_function(value: str) -> str:
    if _SAFE_FUNCTION.fullmatch(value) is None:
        raise ValueError("MATLAB function name must be a safe allowlisted identifier")
    return value


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value


def _q(value: str) -> str:
    return quote(value, safe="")


def _sha(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _json_body(payload: Mapping[str, object]) -> bytes:
    raw = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(raw) > MAX_CAE_REQUEST_BYTES:
        raise ValueError("CAE request exceeds byte bound")
    return raw


def _decode(status: int, raw: bytes) -> dict[str, object]:
    if len(raw) > MAX_CAE_RESPONSE_BYTES:
        raise CAEConnectorError(
            "cae_response_too_large",
            "CAE provider returned more data than FORGE accepts.",
        )
    if status in {401, 403}:
        raise CAEConnectorError(
            "cae_unauthorized",
            "CAE provider credentials were rejected.",
            status=status,
        )
    if status < 200 or status >= 300:
        raise CAEConnectorError(
            f"cae_http_{status}",
            "CAE provider returned an error.",
            status=502,
        )
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CAEConnectorError(
            "cae_invalid_response",
            "CAE provider returned invalid JSON.",
        ) from exc
    if not isinstance(decoded, dict):
        raise CAEConnectorError(
            "cae_invalid_response",
            "CAE provider response must be a JSON object.",
        )
    return cast(dict[str, object], decoded)


def _extract_string(payload: Mapping[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return _safe_id(value, "provider run id")
    raise CAEConnectorError(
        "cae_missing_run_id",
        "CAE provider did not return a usable run identifier.",
    )


def _evidence(
    *,
    provider: CAEProvider,
    operation: CAEOperation,
    source_url: str,
    request_body: bytes | None,
    response_body: bytes,
    collected_at: datetime,
) -> CAEEvidenceMetadata:
    return CAEEvidenceMetadata(
        provider=provider,
        operation=operation,
        source_url=source_url,
        collected_at=collected_at,
        request_hash=_sha(request_body or b""),
        response_hash=_sha(response_body),
    )


__all__ = [
    "AnsysUnavailableConnector",
    "CAEApprovalRef",
    "CAEApprovalPhase",
    "CAEApprovalVerifier",
    "CAEConnectorError",
    "CAEEvidenceMetadata",
    "CAEHTTPTransport",
    "CAEReadResult",
    "CAERunRef",
    "CAESubmitRequest",
    "InMemoryCAEApprovalStore",
    "SQLiteCAEApprovalStore",
    "MATLABProductionServerClient",
    "RedactedHeaders",
    "SimScaleCAEClient",
    "ansys_connector_status",
    "cae_execution_request_hash",
]
