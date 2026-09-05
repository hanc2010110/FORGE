from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from forge_core.hashing import canonical_sha256
from forge_core.models import ContractModel, SourceRef, Verdict


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


class DesignConversationState(StrEnum):
    IDEA = "idea"
    PROPOSED = "proposed"
    SELECTED = "selected"
    CONFIRMED = "confirmed"
    SIMULATED = "simulated"
    REVISED = "revised"
    ACCEPTED = "accepted"
    VERIFIED = "verified"


class EvidenceClaimKind(StrEnum):
    FACT = "fact"
    CALCULATED = "calculated"
    SIMULATED = "simulated"
    MEASURED = "measured"
    INFERRED = "inferred"


class ClaimConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class EvidenceClaimPayload(ContractModel):
    claim_id: str
    kind: EvidenceClaimKind
    statement: str
    evidence_refs: tuple[str, ...]
    confidence: ClaimConfidence | None


class EvidenceClaim(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    claim_id: str = Field(min_length=1)
    kind: EvidenceClaimKind
    statement: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    confidence: ClaimConfidence | None = None
    claim_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("evidence_refs")
    @classmethod
    def evidence_refs_must_be_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("claim evidence references must be unique")
        return value

    @model_validator(mode="after")
    def inference_and_hash_must_be_explicit(self) -> EvidenceClaim:
        if self.kind is EvidenceClaimKind.INFERRED and self.confidence is None:
            raise ValueError("inferred claims require confidence")
        expected = evidence_claim_hash(
            self.claim_id,
            self.kind,
            self.statement,
            self.evidence_refs,
            self.confidence,
        )
        if self.claim_hash != expected:
            raise ValueError("evidence claim hash does not match payload")
        return self


class DesignParameter(ContractModel):
    name: str = Field(min_length=1)
    current_value: str = Field(min_length=1)
    proposed_value: str = Field(min_length=1)
    source_refs: tuple[str, ...] = Field(min_length=1)


class DesignCandidatePayload(ContractModel):
    project_id: str
    candidate_id: str
    revision: int
    baseline_snapshot_hash: str
    proposal_hash: str
    parameters: tuple[DesignParameter, ...]
    requirements: tuple[str, ...]
    confirmed_by: str
    confirmed_at: datetime


class DesignCandidateSnapshot(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    baseline_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    parameters: tuple[DesignParameter, ...] = Field(min_length=1)
    requirements: tuple[str, ...] = Field(min_length=1)
    confirmed_by: str = Field(min_length=1)
    confirmed_at: datetime
    candidate_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("confirmed_at")
    @classmethod
    def confirmed_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "confirmed_at")

    @model_validator(mode="after")
    def candidate_must_be_canonical_and_hash_bound(
        self,
    ) -> DesignCandidateSnapshot:
        names = [item.name for item in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("candidate parameter names must be unique")
        if self.parameters != tuple(
            sorted(self.parameters, key=lambda item: item.name)
        ):
            raise ValueError("candidate parameters must be canonically ordered")
        if len(self.requirements) != len(set(self.requirements)):
            raise ValueError("candidate requirements must be unique")
        expected = design_candidate_hash(
            self.project_id,
            self.candidate_id,
            self.revision,
            self.baseline_snapshot_hash,
            self.proposal_hash,
            self.parameters,
            self.requirements,
            self.confirmed_by,
            self.confirmed_at,
        )
        if self.candidate_hash != expected:
            raise ValueError("design candidate hash does not match payload")
        return self


class SimulationMetric(ContractModel):
    metric: str = Field(min_length=1)
    actual: str = Field(min_length=1)
    requirement: str = Field(min_length=1)
    verdict: Verdict

    @field_validator("verdict", mode="before")
    @classmethod
    def verdict_accepts_lowercase_api_values(cls, value: object) -> object:
        if isinstance(value, str):
            return value.upper()
        return value


class SimulationBindingPayload(ContractModel):
    simulation_id: str
    candidate_hash: str
    tool_ref: SourceRef
    metrics: tuple[SimulationMetric, ...]
    created_at: datetime


class SimulationBinding(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    simulation_id: str = Field(min_length=1)
    candidate_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    tool_ref: SourceRef
    metrics: tuple[SimulationMetric, ...] = Field(min_length=1)
    created_at: datetime
    simulation_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("created_at")
    @classmethod
    def created_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "created_at")

    @model_validator(mode="after")
    def simulation_must_be_canonical_and_hash_bound(self) -> SimulationBinding:
        names = [item.metric for item in self.metrics]
        if len(names) != len(set(names)):
            raise ValueError("simulation metric names must be unique")
        if self.metrics != tuple(sorted(self.metrics, key=lambda item: item.metric)):
            raise ValueError("simulation metrics must be canonically ordered")
        expected = simulation_binding_hash(
            self.simulation_id,
            self.candidate_hash,
            self.tool_ref,
            self.metrics,
            self.created_at,
        )
        if self.simulation_hash != expected:
            raise ValueError("simulation hash does not match payload")
        return self


class DesignStateTransition(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    session_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    from_state: DesignConversationState
    to_state: DesignConversationState
    candidate_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    simulation_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    evidence_refs: tuple[str, ...] = ()
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "occurred_at")

    @model_validator(mode="after")
    def transition_must_follow_confirmation_and_evidence_gates(
        self,
    ) -> DesignStateTransition:
        allowed = {
            DesignConversationState.IDEA: {DesignConversationState.PROPOSED},
            DesignConversationState.PROPOSED: {DesignConversationState.SELECTED},
            DesignConversationState.SELECTED: {
                DesignConversationState.PROPOSED,
                DesignConversationState.CONFIRMED,
            },
            DesignConversationState.CONFIRMED: {DesignConversationState.SIMULATED},
            DesignConversationState.SIMULATED: {
                DesignConversationState.REVISED,
                DesignConversationState.ACCEPTED,
            },
            DesignConversationState.REVISED: {DesignConversationState.CONFIRMED},
            DesignConversationState.ACCEPTED: {DesignConversationState.VERIFIED},
            DesignConversationState.VERIFIED: set(),
        }
        if self.to_state not in allowed[self.from_state]:
            raise ValueError("design conversation state transition is not allowed")
        if (
            self.to_state is DesignConversationState.CONFIRMED
            and self.candidate_hash is None
        ):
            raise ValueError("confirmation must bind an immutable candidate")
        if self.to_state is DesignConversationState.SIMULATED and (
            self.candidate_hash is None or self.simulation_hash is None
        ):
            raise ValueError("simulation must bind candidate and result hashes")
        if (
            self.to_state
            in {
                DesignConversationState.REVISED,
                DesignConversationState.ACCEPTED,
            }
            and self.simulation_hash is None
        ):
            raise ValueError("simulation review must bind the reviewed result")
        if self.to_state is DesignConversationState.VERIFIED and not self.evidence_refs:
            raise ValueError("verification requires imported evidence")
        return self


def evidence_claim_hash(
    claim_id: str,
    kind: EvidenceClaimKind,
    statement: str,
    evidence_refs: tuple[str, ...],
    confidence: ClaimConfidence | None,
) -> str:
    return canonical_sha256(
        EvidenceClaimPayload(
            claim_id=claim_id,
            kind=kind,
            statement=statement,
            evidence_refs=evidence_refs,
            confidence=confidence,
        )
    )


def build_evidence_claim(
    *,
    claim_id: str,
    kind: EvidenceClaimKind,
    statement: str,
    evidence_refs: tuple[str, ...],
    confidence: ClaimConfidence | None = None,
) -> EvidenceClaim:
    return EvidenceClaim(
        claim_id=claim_id,
        kind=kind,
        statement=statement,
        evidence_refs=evidence_refs,
        confidence=confidence,
        claim_hash=evidence_claim_hash(
            claim_id, kind, statement, evidence_refs, confidence
        ),
    )


def design_candidate_hash(
    project_id: str,
    candidate_id: str,
    revision: int,
    baseline_snapshot_hash: str,
    proposal_hash: str,
    parameters: tuple[DesignParameter, ...],
    requirements: tuple[str, ...],
    confirmed_by: str,
    confirmed_at: datetime,
) -> str:
    return canonical_sha256(
        DesignCandidatePayload(
            project_id=project_id,
            candidate_id=candidate_id,
            revision=revision,
            baseline_snapshot_hash=baseline_snapshot_hash,
            proposal_hash=proposal_hash,
            parameters=parameters,
            requirements=requirements,
            confirmed_by=confirmed_by,
            confirmed_at=confirmed_at,
        )
    )


def build_design_candidate(
    *,
    project_id: str,
    candidate_id: str,
    revision: int,
    baseline_snapshot_hash: str,
    proposal_hash: str,
    parameters: tuple[DesignParameter, ...],
    requirements: tuple[str, ...],
    confirmed_by: str,
    confirmed_at: datetime,
) -> DesignCandidateSnapshot:
    ordered = tuple(sorted(parameters, key=lambda item: item.name))
    candidate_hash = design_candidate_hash(
        project_id,
        candidate_id,
        revision,
        baseline_snapshot_hash,
        proposal_hash,
        ordered,
        requirements,
        confirmed_by,
        confirmed_at,
    )
    return DesignCandidateSnapshot(
        project_id=project_id,
        candidate_id=candidate_id,
        revision=revision,
        baseline_snapshot_hash=baseline_snapshot_hash,
        proposal_hash=proposal_hash,
        parameters=ordered,
        requirements=requirements,
        confirmed_by=confirmed_by,
        confirmed_at=confirmed_at,
        candidate_hash=candidate_hash,
    )


def simulation_binding_hash(
    simulation_id: str,
    candidate_hash: str,
    tool_ref: SourceRef,
    metrics: tuple[SimulationMetric, ...],
    created_at: datetime,
) -> str:
    return canonical_sha256(
        SimulationBindingPayload(
            simulation_id=simulation_id,
            candidate_hash=candidate_hash,
            tool_ref=tool_ref,
            metrics=metrics,
            created_at=created_at,
        )
    )


def build_simulation_binding(
    *,
    simulation_id: str,
    candidate: DesignCandidateSnapshot,
    tool_ref: SourceRef,
    metrics: tuple[SimulationMetric, ...],
    created_at: datetime,
) -> SimulationBinding:
    ordered = tuple(sorted(metrics, key=lambda item: item.metric))
    simulation_hash = simulation_binding_hash(
        simulation_id,
        candidate.candidate_hash,
        tool_ref,
        ordered,
        created_at,
    )
    return SimulationBinding(
        simulation_id=simulation_id,
        candidate_hash=candidate.candidate_hash,
        tool_ref=tool_ref,
        metrics=ordered,
        created_at=created_at,
        simulation_hash=simulation_hash,
    )
