from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Literal, Protocol, cast
from urllib.parse import quote

from pydantic import Field, field_serializer, field_validator, model_validator

from forge_core.models import ContractModel

MAX_CAE_REQUEST_BYTES = 512_000
MAX_CAE_RESPONSE_BYTES = 2_000_000
DEFAULT_CAE_TIMEOUT_SECONDS = 20.0
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SAFE_FUNCTION = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}")
_HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"

CAEProvider = Literal["simscale", "matlab-production-server", "ansys"]
CAEOperation = Literal["submit", "start", "status", "result", "execute"]


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
    project_id: str = Field(min_length=1)
    candidate_hash: str = Field(pattern=_HASH_PATTERN)
    approved_by: str = Field(min_length=1)
    approved_at: datetime

    @field_validator("approval_id", "project_id")
    @classmethod
    def ids_must_be_safe(cls, value: str) -> str:
        return _safe_id(value, "approval identifiers")

    @field_validator("approved_at")
    @classmethod
    def approved_at_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value, "approved_at")


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
        return MappingProxyType(dict(value))

    @field_serializer("payload")
    def serialize_payload(self, value: Mapping[str, object]) -> dict[str, object]:
        return dict(value)

    @model_validator(mode="after")
    def approval_must_match_candidate(self) -> CAESubmitRequest:
        if self.approval.project_id != self.project_id:
            raise ValueError("CAE approval project does not match request")
        if self.approval.candidate_hash != self.candidate_hash:
            raise ValueError("CAE approval candidate does not match request")
        return self


class CAERunRef(ContractModel):
    provider: CAEProvider
    project_id: str = Field(min_length=1)
    simulation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    candidate_hash: str = Field(pattern=_HASH_PATTERN)
    scenario_hash: str = Field(pattern=_HASH_PATTERN)
    submitted_at: datetime
    evidence: CAEEvidenceMetadata

    @field_validator("project_id", "simulation_id", "run_id")
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
    payload: Mapping[str, object]
    evidence: CAEEvidenceMetadata

    @field_validator("payload", mode="after")
    @classmethod
    def freeze_payload(cls, value: Mapping[str, object]) -> Mapping[str, object]:
        return MappingProxyType(dict(value))

    @field_serializer("payload")
    def serialize_payload(self, value: Mapping[str, object]) -> dict[str, object]:
        return dict(value)


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
        transport: CAEHTTPTransport,
        clock: Callable[[], datetime],
        timeout_seconds: float = DEFAULT_CAE_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = _base_url(base_url)
        self._api_key = _secret(api_key, "SimScale API key")
        self._transport = transport
        self._clock = clock
        self._timeout_seconds = _timeout(timeout_seconds)

    def submit_run(self, request: CAESubmitRequest) -> CAERunRef:
        path = (
            f"/v1/projects/{_q(request.project_id)}"
            f"/simulations/{_q(request.simulation_id)}/runs"
        )
        body = _json_body(
            {
                "candidate_hash": request.candidate_hash,
                "scenario_hash": request.scenario_hash,
                "payload": dict(request.payload),
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
        return CAERunRef(
            provider="simscale",
            project_id=request.project_id,
            simulation_id=request.simulation_id,
            run_id=run_id,
            candidate_hash=request.candidate_hash,
            scenario_hash=request.scenario_hash,
            submitted_at=evidence.collected_at,
            evidence=evidence,
        )

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
        if approval.candidate_hash != run.candidate_hash:
            raise ValueError("CAE approval candidate does not match run")
        safe_key = _safe_id(idempotency_key, "idempotency key")
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
        return CAEReadResult(
            provider="simscale",
            operation="start",
            project_id=run.project_id,
            simulation_id=run.simulation_id,
            run_id=run.run_id,
            payload=decoded,
            evidence=evidence,
        )

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
        payload: Mapping[str, object],
    ) -> CAEReadResult:
        safe_function = _safe_function(function_name)
        if safe_function not in self._allowlisted_functions:
            raise ValueError("MATLAB function is not allowlisted for FORGE")
        body = _json_body(dict(payload))
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
        return CAEReadResult(
            provider="matlab-production-server",
            operation="execute",
            project_id=_safe_id(project_id, "project id"),
            simulation_id=_safe_id(simulation_id, "simulation id"),
            run_id=_safe_id(run_id, "run id"),
            payload=decoded,
            evidence=evidence,
        )


def ansys_connector_status() -> AnsysUnavailableConnector:
    return AnsysUnavailableConnector()


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
    "CAEConnectorError",
    "CAEEvidenceMetadata",
    "CAEHTTPTransport",
    "CAEReadResult",
    "CAERunRef",
    "CAESubmitRequest",
    "MATLABProductionServerClient",
    "RedactedHeaders",
    "SimScaleCAEClient",
    "ansys_connector_status",
]
