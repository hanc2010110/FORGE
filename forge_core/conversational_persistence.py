from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field, field_validator, model_validator

from forge_core.conversational_design import (
    DesignCandidateSnapshot,
    DesignConversationState,
    DesignParameter,
    DesignStateTransition,
    EvidenceClaim,
    SimulationBinding,
)
from forge_core.hashing import canonical_sha256
from forge_core.models import ContractModel


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


class _DesignCandidateApprovalHashMaterial(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    approval_id: str
    project_id: str
    request_hash: str
    nonce_hash: str
    approved_by: str
    approved_at: datetime


class _DesignCandidateApprovalReceiptHashMaterial(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    approval_id: str
    approval_hash: str
    project_id: str
    request_hash: str
    candidate_hash: str
    consumed_by: str
    consumed_at: datetime


def design_candidate_approval_hash(
    *,
    approval_id: str,
    project_id: str,
    request_hash: str,
    nonce_hash: str,
    approved_by: str,
    approved_at: datetime,
) -> str:
    return canonical_sha256(
        _DesignCandidateApprovalHashMaterial(
            approval_id=approval_id,
            project_id=project_id,
            request_hash=request_hash,
            nonce_hash=nonce_hash,
            approved_by=approved_by,
            approved_at=approved_at,
        )
    )


def design_candidate_approval_receipt_hash(
    *,
    approval_id: str,
    approval_hash: str,
    project_id: str,
    request_hash: str,
    candidate_hash: str,
    consumed_by: str,
    consumed_at: datetime,
) -> str:
    return canonical_sha256(
        _DesignCandidateApprovalReceiptHashMaterial(
            approval_id=approval_id,
            approval_hash=approval_hash,
            project_id=project_id,
            request_hash=request_hash,
            candidate_hash=candidate_hash,
            consumed_by=consumed_by,
            consumed_at=consumed_at,
        )
    )


class StoredDesignCandidate(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    session_id: str = "session-1"
    candidate_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    candidate_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    candidate: DesignCandidateSnapshot
    stored_at: datetime

    @field_validator("stored_at")
    @classmethod
    def stored_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "stored_at")

    @model_validator(mode="after")
    def candidate_must_bind_project_hash_and_time(self) -> StoredDesignCandidate:
        DesignCandidateSnapshot.model_validate(self.candidate.model_dump(mode="python"))
        if self.project_id != self.candidate.project_id:
            raise ValueError("stored candidate project does not match payload")
        if self.candidate_hash != self.candidate.candidate_hash:
            raise ValueError("stored candidate hash does not match payload")
        if (
            self.candidate_id != self.candidate.candidate_id
            or self.revision != self.candidate.revision
            or self.proposal_hash != self.candidate.proposal_hash
        ):
            raise ValueError("stored candidate identity does not match payload")
        if self.candidate.confirmed_at > self.stored_at:
            raise ValueError("candidate cannot be stored before confirmation")
        return self


class DesignCandidateApprovalRequest(ContractModel):
    """Exact candidate payload a human approved before it becomes executable."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    parameters: tuple[DesignParameter, ...] = Field(min_length=1)
    requirements: tuple[str, ...] = Field(min_length=1)
    confirmed_by: str = Field(min_length=1)


class StoredDesignCandidateApproval(ContractModel):
    """Trusted, single-use approval for one exact candidate request."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    approval_id: str = Field(min_length=1)
    approval_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    project_id: str = Field(min_length=1)
    request_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    nonce_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    approved_by: str = Field(min_length=1)
    request: DesignCandidateApprovalRequest
    approved_at: datetime
    stored_at: datetime

    @field_validator("approved_at", "stored_at")
    @classmethod
    def approval_timestamps_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "approval timestamp")

    @model_validator(mode="after")
    def approval_must_bind_exact_request_actor_and_hash(
        self,
    ) -> StoredDesignCandidateApproval:
        if self.project_id != self.request.project_id:
            raise ValueError("candidate approval project does not match request")
        if self.approved_by != self.request.confirmed_by:
            raise ValueError("candidate approval actor does not match request")
        if self.request_hash != canonical_sha256(self.request):
            raise ValueError("candidate approval request hash does not reproduce")
        expected_approval_hash = design_candidate_approval_hash(
            approval_id=self.approval_id,
            project_id=self.project_id,
            request_hash=self.request_hash,
            nonce_hash=self.nonce_hash,
            approved_by=self.approved_by,
            approved_at=self.approved_at,
        )
        if self.approval_hash != expected_approval_hash:
            raise ValueError("candidate approval hash does not reproduce")
        if self.approved_at > self.stored_at:
            raise ValueError("candidate approval cannot be stored before approval")
        return self


class StoredDesignCandidateApprovalReceipt(ContractModel):
    """Immutable proof that one approval produced one candidate exactly once."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    receipt_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    approval_id: str = Field(min_length=1)
    approval_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    project_id: str = Field(min_length=1)
    request_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    candidate_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    consumed_by: str = Field(min_length=1)
    consumed_at: datetime

    @field_validator("consumed_at")
    @classmethod
    def consumed_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "consumed_at")

    @model_validator(mode="after")
    def receipt_hash_must_reproduce(self) -> StoredDesignCandidateApprovalReceipt:
        expected = design_candidate_approval_receipt_hash(
            approval_id=self.approval_id,
            approval_hash=self.approval_hash,
            project_id=self.project_id,
            request_hash=self.request_hash,
            candidate_hash=self.candidate_hash,
            consumed_by=self.consumed_by,
            consumed_at=self.consumed_at,
        )
        if self.receipt_hash != expected:
            raise ValueError("candidate approval receipt hash does not reproduce")
        return self


class StoredSimulationBinding(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    session_id: str = "session-1"
    candidate_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    simulation_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    simulation_id: str = Field(min_length=1)
    simulation: SimulationBinding
    stored_at: datetime

    @field_validator("stored_at")
    @classmethod
    def stored_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "stored_at")

    @model_validator(mode="after")
    def simulation_must_bind_candidate_hash_result_hash_and_time(
        self,
    ) -> StoredSimulationBinding:
        SimulationBinding.model_validate(self.simulation.model_dump(mode="python"))
        if self.candidate_hash != self.simulation.candidate_hash:
            raise ValueError("stored simulation candidate does not match payload")
        if self.simulation_hash != self.simulation.simulation_hash:
            raise ValueError("stored simulation hash does not match payload")
        if self.simulation_id != self.simulation.simulation_id:
            raise ValueError("stored simulation identity does not match payload")
        if self.simulation.created_at > self.stored_at:
            raise ValueError("simulation cannot be stored before creation")
        return self


class StoredEvidenceClaim(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    session_id: str = "session-1"
    claim_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    claim_id: str = Field(min_length=1)
    claim: EvidenceClaim
    stored_at: datetime

    @field_validator("stored_at")
    @classmethod
    def stored_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "stored_at")

    @model_validator(mode="after")
    def claim_must_bind_exact_hash(self) -> StoredEvidenceClaim:
        EvidenceClaim.model_validate(self.claim.model_dump(mode="python"))
        if self.claim_hash != self.claim.claim_hash:
            raise ValueError("stored evidence claim hash does not match payload")
        if self.claim_id != self.claim.claim_id:
            raise ValueError("stored evidence claim identity does not match payload")
        return self


class StoredDesignStateTransition(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    transition_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    from_state: DesignConversationState
    to_state: DesignConversationState
    candidate_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    simulation_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    transition: DesignStateTransition
    stored_at: datetime

    @field_validator("stored_at")
    @classmethod
    def stored_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "stored_at")

    @model_validator(mode="after")
    def transition_must_bind_session_sequence_hash_and_time(
        self,
    ) -> StoredDesignStateTransition:
        DesignStateTransition.model_validate(self.transition.model_dump(mode="python"))
        if self.session_id != self.transition.session_id:
            raise ValueError("stored transition session does not match payload")
        if self.sequence != self.transition.sequence:
            raise ValueError("stored transition sequence does not match payload")
        if (
            self.transition_hash is not None
            and self.transition_hash != canonical_sha256(self.transition)
        ):
            raise ValueError("stored transition hash does not match payload")
        if (
            self.from_state is not self.transition.from_state
            or self.to_state is not self.transition.to_state
            or self.candidate_hash != self.transition.candidate_hash
            or self.simulation_hash != self.transition.simulation_hash
        ):
            raise ValueError("stored transition bindings do not match payload")
        if len(self.transition.evidence_refs) != len(
            set(self.transition.evidence_refs)
        ):
            raise ValueError("stored transition evidence references must be unique")
        if self.transition.occurred_at > self.stored_at:
            raise ValueError("transition cannot be stored before occurrence")
        return self


__all__ = [
    "DesignCandidateApprovalRequest",
    "StoredDesignCandidate",
    "StoredDesignCandidateApproval",
    "StoredDesignCandidateApprovalReceipt",
    "StoredDesignStateTransition",
    "StoredEvidenceClaim",
    "StoredSimulationBinding",
    "design_candidate_approval_hash",
    "design_candidate_approval_receipt_hash",
]
