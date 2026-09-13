from __future__ import annotations

import hmac
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from threading import Lock
from typing import Literal, Protocol

from pydantic import Field, field_serializer, field_validator, model_validator

from forge_core.hashing import canonical_sha256
from forge_core.immutable_json import freeze_json_mapping, thaw_json
from forge_core.models import ContractModel

EdgeTier = Literal["simulation", "bench", "hil", "physical_device"]
EdgeAdapter = Literal["ros2", "mqtt", "opcua", "hil_artifact"]
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_ZERO_HASH = "sha256:" + "0" * 64
_SIGNATURE_PREFIX = "hmac-sha256:"
_UNSIGNED_SIGNATURE = "unsigned-placeholder"
_SIGNATURE_ALGORITHM = re.compile(r"[a-z0-9][a-z0-9._-]{1,63}")
_FORBIDDEN_PAYLOAD_KEYS = {
    "actuator_command",
    "command",
    "control",
    "device_command",
    "raw_device_command",
    "setpoint",
    "write",
}


class EdgeEvidenceError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value


class EdgeEvidenceEnvelope(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    rig_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    tier: EdgeTier
    adapter: EdgeAdapter
    source_uri: str = Field(min_length=1)
    captured_at: datetime
    sequence: int = Field(ge=1)
    nonce: str = Field(min_length=1)
    candidate_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    firmware_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    artifact_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    payload: Mapping[str, object]
    signature_algorithm: str = Field(min_length=2, max_length=64)
    signature_key_id: str = Field(min_length=1)
    signature: str = Field(min_length=16, max_length=4096)
    envelope_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator(
        "project_id",
        "tenant_id",
        "evidence_id",
        "device_id",
        "rig_id",
        "run_id",
        "nonce",
    )
    @classmethod
    def ids_must_be_safe(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("edge evidence identifiers must be safe")
        return value

    @field_validator("captured_at")
    @classmethod
    def captured_at_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value, "captured_at")

    @field_validator("source_uri")
    @classmethod
    def source_uri_must_be_an_evidence_route(cls, value: str) -> str:
        lowered = value.casefold()
        if re.match(r"^[a-z][a-z0-9+.-]*://", value) is None:
            raise ValueError("source_uri must include an explicit scheme")
        if any(marker in lowered for marker in ("/control", "/command", "/write")):
            raise ValueError("source_uri must point to read-only evidence")
        return value

    @field_validator("signature_algorithm")
    @classmethod
    def signature_algorithm_must_be_safe(cls, value: str) -> str:
        if _SIGNATURE_ALGORITHM.fullmatch(value) is None:
            raise ValueError("edge evidence signature algorithm is invalid")
        return value

    @field_validator("signature")
    @classmethod
    def signature_must_be_header_safe(cls, value: str) -> str:
        if any(character.isspace() for character in value):
            raise ValueError("edge evidence signature is invalid")
        return value

    @field_validator("payload", mode="after")
    @classmethod
    def payload_must_be_deeply_immutable(
        cls, value: Mapping[str, object]
    ) -> Mapping[str, object]:
        return freeze_json_mapping(value)

    @field_serializer("payload")
    def serialize_payload(self, value: Mapping[str, object]) -> object:
        return thaw_json(value)

    @model_validator(mode="after")
    def payload_and_hash_must_be_safe(self) -> EdgeEvidenceEnvelope:
        if _contains_forbidden_control_key(self.payload):
            raise ValueError("edge evidence cannot carry raw device control payloads")
        scheme = self.source_uri.split("://", maxsplit=1)[0].casefold()
        allowed_schemes = {
            "ros2": {"ros2"},
            "mqtt": {"mqtt", "mqtts"},
            "opcua": {"opc.tcp", "https"},
            "hil_artifact": {"file", "https"},
        }
        if scheme not in allowed_schemes[self.adapter]:
            raise ValueError("edge evidence adapter does not match source protocol")
        if self.adapter == "hil_artifact" and self.tier != "hil":
            raise ValueError("HIL artifact adapter requires HIL evidence tier")
        if self.envelope_hash != edge_envelope_hash(
            self.model_copy(update={"envelope_hash": _ZERO_HASH})
        ):
            raise ValueError("edge evidence envelope hash does not match payload")
        return self


class AcceptedEdgeEvidence(ContractModel):
    envelope: EdgeEvidenceEnvelope
    accepted_at: datetime
    acceptance_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("accepted_at")
    @classmethod
    def accepted_at_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value, "accepted_at")

    @model_validator(mode="after")
    def acceptance_hash_must_match_payload(self) -> AcceptedEdgeEvidence:
        if self.acceptance_hash != canonical_sha256(
            self.model_copy(update={"acceptance_hash": _ZERO_HASH})
        ):
            raise ValueError("edge evidence acceptance hash does not match payload")
        return self


def edge_envelope_hash(envelope: EdgeEvidenceEnvelope) -> str:
    return canonical_sha256(
        envelope.model_copy(
            update={
                "signature": _UNSIGNED_SIGNATURE,
                "envelope_hash": _ZERO_HASH,
            }
        )
    )


class DevelopmentHMACVerifier:
    def __init__(self, *, secret: str, key_id: str = "dev-key") -> None:
        if not secret or "\r" in secret or "\n" in secret:
            raise ValueError("development HMAC secret is invalid")
        self._secret = secret.encode("utf-8")
        self.key_id = key_id

    def __repr__(self) -> str:
        return f"DevelopmentHMACVerifier(key_id={self.key_id!r}, secret='<redacted>')"

    def sign(self, envelope: EdgeEvidenceEnvelope) -> str:
        digest = hmac.digest(self._secret, _signature_payload(envelope), "sha256")
        return f"{_SIGNATURE_PREFIX}{digest.hex()}"

    def verify(self, envelope: EdgeEvidenceEnvelope) -> bool:
        return (
            envelope.signature_algorithm == "hmac-sha256-development-only"
            and envelope.signature_key_id == self.key_id
            and hmac.compare_digest(envelope.signature, self.sign(envelope))
        )


class SignatureVerifier(Protocol):
    """Verification boundary for a production PKI/JWS implementation."""

    def verify(self, envelope: EdgeEvidenceEnvelope) -> bool: ...


class ReplayStore(Protocol):
    """Replay boundary; deployments can inject an atomic durable store."""

    def accept_once(self, envelope: EdgeEvidenceEnvelope) -> None: ...


class InMemoryReplayStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._nonces: set[tuple[str, str, str, str]] = set()
        self._last_sequence: dict[tuple[str, str, str], int] = {}

    def accept_once(self, envelope: EdgeEvidenceEnvelope) -> None:
        with self._lock:
            nonce_key = (
                envelope.tenant_id,
                envelope.project_id,
                envelope.device_id,
                envelope.nonce,
            )
            if nonce_key in self._nonces:
                raise EdgeEvidenceError("edge_evidence_replay")
            sequence_key = (
                envelope.tenant_id,
                envelope.project_id,
                envelope.device_id,
            )
            previous = self._last_sequence.get(sequence_key, 0)
            if envelope.sequence <= previous:
                raise EdgeEvidenceError("edge_evidence_sequence_rollback")
            self._nonces.add(nonce_key)
            self._last_sequence[sequence_key] = envelope.sequence


class EdgeEvidenceIngestor:
    def __init__(
        self,
        *,
        verifier: SignatureVerifier,
        replay_store: ReplayStore,
        now: Callable[[], datetime],
    ) -> None:
        self._verifier = verifier
        self._replay_store = replay_store
        self._now = now

    def ingest(self, envelope: EdgeEvidenceEnvelope) -> AcceptedEdgeEvidence:
        accepted_at = _utc(self._now(), "accepted_at")
        if accepted_at < envelope.captured_at - timedelta(seconds=1):
            raise EdgeEvidenceError("edge_evidence_from_future")
        if not self._verifier.verify(envelope):
            raise EdgeEvidenceError("edge_evidence_bad_signature")
        self._replay_store.accept_once(envelope)
        draft = AcceptedEdgeEvidence.model_construct(
            envelope=envelope,
            accepted_at=accepted_at,
            acceptance_hash=_ZERO_HASH,
        )
        return AcceptedEdgeEvidence.model_validate(
            draft.model_copy(
                update={"acceptance_hash": canonical_sha256(draft)}
            ).model_dump()
        )


def create_development_signed_envelope(
    *,
    project_id: str,
    tenant_id: str,
    evidence_id: str,
    device_id: str,
    rig_id: str,
    run_id: str,
    tier: EdgeTier,
    adapter: EdgeAdapter,
    source_uri: str,
    captured_at: datetime,
    sequence: int,
    nonce: str,
    candidate_hash: str,
    firmware_hash: str,
    artifact_hash: str,
    payload: Mapping[str, object],
    verifier: DevelopmentHMACVerifier,
) -> EdgeEvidenceEnvelope:
    draft = EdgeEvidenceEnvelope.model_construct(
        schema_version="1.0.0",
        project_id=project_id,
        tenant_id=tenant_id,
        evidence_id=evidence_id,
        device_id=device_id,
        rig_id=rig_id,
        run_id=run_id,
        tier=tier,
        adapter=adapter,
        source_uri=source_uri,
        captured_at=captured_at,
        sequence=sequence,
        nonce=nonce,
        candidate_hash=candidate_hash,
        firmware_hash=firmware_hash,
        artifact_hash=artifact_hash,
        payload=dict(payload),
        signature_algorithm="hmac-sha256-development-only",
        signature_key_id=verifier.key_id,
        signature=_SIGNATURE_PREFIX + "0" * 64,
        envelope_hash=_ZERO_HASH,
    )
    envelope = EdgeEvidenceEnvelope.model_validate(
        draft.model_copy(
            update={"envelope_hash": edge_envelope_hash(draft)}
        ).model_dump()
    )
    return EdgeEvidenceEnvelope.model_validate(
        envelope.model_copy(update={"signature": verifier.sign(envelope)}).model_dump()
    )


def _signature_payload(envelope: EdgeEvidenceEnvelope) -> bytes:
    return json.dumps(
        envelope.model_dump(mode="json", exclude={"signature", "envelope_hash"}),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _contains_forbidden_control_key(payload: Mapping[str, object]) -> bool:
    pending: list[object] = [payload]
    visited = 0
    while pending:
        current = pending.pop()
        visited += 1
        if visited > 10_000:
            raise ValueError("edge evidence payload is too complex")
        if isinstance(current, Mapping):
            for key, value in current.items():
                if str(key).casefold() in _FORBIDDEN_PAYLOAD_KEYS:
                    return True
                pending.append(value)
        elif isinstance(current, list | tuple):
            pending.extend(current)
    return False


__all__ = [
    "AcceptedEdgeEvidence",
    "DevelopmentHMACVerifier",
    "EdgeAdapter",
    "EdgeEvidenceEnvelope",
    "EdgeEvidenceError",
    "EdgeEvidenceIngestor",
    "InMemoryReplayStore",
    "ReplayStore",
    "SignatureVerifier",
    "create_development_signed_envelope",
    "edge_envelope_hash",
]
