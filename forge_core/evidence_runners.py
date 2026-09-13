from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Literal, cast

from pydantic import Field, field_serializer, field_validator, model_validator

from forge_core.hashing import canonical_sha256
from forge_core.immutable_json import freeze_json_mapping, thaw_json
from forge_core.models import ContractModel

EvidenceRunnerTier = Literal["simulation", "bench", "hil", "physical_device"]
EvidenceOutputKind = Literal["json", "text"]
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SAFE_EXECUTABLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
_ZERO_HASH = "sha256:" + "0" * 64
ProcessRunner = Callable[[tuple[str, ...], float], tuple[int, bytes, bytes]]


def _sha(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _canonical_mapping_sha(payload: Mapping[str, object]) -> str:
    raw = json.dumps(
        payload, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return _sha(raw)


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value


class RegisteredEvidenceCommand(ContractModel):
    command_id: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    tier: EvidenceRunnerTier
    argv: tuple[str, ...] = Field(min_length=1, max_length=32)
    output_kind: EvidenceOutputKind
    timeout_seconds: float = Field(default=30, gt=0, le=600)

    @field_validator("command_id", "adapter_id")
    @classmethod
    def ids_must_be_safe(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("evidence command identifiers must be safe")
        return value

    @field_validator("argv")
    @classmethod
    def argv_must_be_allowlist_safe(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        executable = value[0]
        blocked = {"python", "python3", "sh", "bash", "zsh", "osascript", "curl"}
        if executable in blocked or _SAFE_EXECUTABLE.fullmatch(executable) is None:
            raise ValueError("evidence command executable is not allowlist safe")
        if any("\x00" in item or "\r" in item or "\n" in item for item in value):
            raise ValueError("evidence command argv contains unsafe text")
        return value


class EvidenceRunRequest(ContractModel):
    run_id: str = Field(min_length=1)
    command_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    candidate_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    scenario_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    requested_at: datetime

    @field_validator("run_id", "command_id", "project_id")
    @classmethod
    def ids_must_be_safe(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("evidence run identifiers must be safe")
        return value

    @field_validator("requested_at")
    @classmethod
    def requested_at_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value, "requested_at")


class EvidenceRunResult(ContractModel):
    schema_version: str = "1.0.0"
    request: EvidenceRunRequest
    command_id: str
    adapter_id: str
    tier: EvidenceRunnerTier
    argv_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    exit_code: int
    stdout_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    stderr_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    interpreted_payload: Mapping[str, object] | None = None
    completed_at: datetime
    result_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("interpreted_payload", mode="after")
    @classmethod
    def freeze_interpreted_payload(
        cls, value: Mapping[str, object] | None
    ) -> Mapping[str, object] | None:
        return None if value is None else freeze_json_mapping(value)

    @field_serializer("interpreted_payload")
    def serialize_interpreted_payload(
        self, value: Mapping[str, object] | None
    ) -> dict[str, object] | None:
        if value is None:
            return None
        return cast(dict[str, object], thaw_json(value))

    @field_validator("completed_at")
    @classmethod
    def completed_at_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value, "completed_at")

    @model_validator(mode="after")
    def result_hash_must_match_payload(self) -> EvidenceRunResult:
        if self.result_hash != canonical_sha256(
            self.model_copy(update={"result_hash": _ZERO_HASH})
        ):
            raise ValueError("evidence run result hash does not match payload")
        return self


class CommandEvidenceRunner:
    def __init__(
        self,
        *,
        commands: tuple[RegisteredEvidenceCommand, ...],
        process_runner: ProcessRunner,
        clock: Callable[[], datetime],
    ) -> None:
        ids = [item.command_id for item in commands]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence commands must be unique")
        self._commands = {item.command_id: item for item in commands}
        self._process_runner = process_runner
        self._clock = clock

    def run(self, request: EvidenceRunRequest) -> EvidenceRunResult:
        try:
            command = self._commands[request.command_id]
        except KeyError as exc:
            raise KeyError("unknown evidence command") from exc
        exit_code, stdout, stderr = self._process_runner(
            command.argv, command.timeout_seconds
        )
        payload = _interpret(command.output_kind, stdout)
        completed_at = _utc(self._clock(), "completed_at")
        if completed_at < request.requested_at - timedelta(seconds=1):
            raise ValueError("evidence completion cannot precede request")
        draft = EvidenceRunResult.model_construct(
            schema_version="1.0.0",
            request=request,
            command_id=command.command_id,
            adapter_id=command.adapter_id,
            tier=command.tier,
            argv_hash=_canonical_mapping_sha({"argv": command.argv}),
            exit_code=exit_code,
            stdout_sha256=_sha(stdout),
            stderr_sha256=_sha(stderr),
            interpreted_payload=payload,
            completed_at=completed_at,
            result_hash=_ZERO_HASH,
        )
        return EvidenceRunResult.model_validate(
            draft.model_copy(
                update={
                    "result_hash": canonical_sha256(
                        draft.model_copy(update={"result_hash": _ZERO_HASH})
                    )
                }
            ).model_dump()
        )


def _interpret(kind: EvidenceOutputKind, stdout: bytes) -> Mapping[str, object] | None:
    if kind == "text":
        return None
    try:
        decoded = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("JSON evidence command returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("JSON evidence command must return an object")
    return decoded


__all__ = [
    "CommandEvidenceRunner",
    "EvidenceRunRequest",
    "EvidenceRunResult",
    "EvidenceRunnerTier",
    "RegisteredEvidenceCommand",
]
