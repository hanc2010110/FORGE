from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from forge_core.hashing import canonical_sha256
from forge_core.models import ContractModel

EvidenceTierName = str
HashValue = str


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value


def _unique_tuple(value: tuple[str, ...], name: str) -> tuple[str, ...]:
    if len(value) != len(set(value)):
        raise ValueError(f"{name} must be unique")
    return value


class IterationTransitionError(ValueError):
    """Raised when an approved engineering iteration would be replayed or forged."""


class IterationPhase(StrEnum):
    TALK = "talk"
    CONFIRM = "confirm"
    SIMULATE = "simulate"
    REVISE = "revise"
    VERIFY = "verify"


class IterationDecision(StrEnum):
    COLLECT_REQUIRED_EVIDENCE = "collect_required_evidence"
    REVISE_AND_RESIMULATE = "revise_and_resimulate"
    INTERPRET_WITH_USER = "interpret_with_user"
    WAIT_FOR_NEW_EVIDENCE = "wait_for_new_evidence"
    VERIFY_RELEASE = "verify_release"


class SimulationRequest(ContractModel):
    project_id: str = Field(min_length=1)
    candidate_hash: HashValue = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    request_id: str = Field(min_length=1, max_length=128)
    requested_by: str = Field(min_length=1, max_length=128)
    requested_at: datetime
    scenario_hash: HashValue = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    expected_result_kinds: tuple[str, ...] = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("requested_at")
    @classmethod
    def requested_at_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value, "requested_at")

    @field_validator("expected_result_kinds")
    @classmethod
    def result_kinds_must_be_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique_tuple(value, "expected result kinds")


class CandidateSimulationApproval(ContractModel):
    approval_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1)
    candidate_hash: HashValue = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    simulation_request_hash: HashValue = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    approved_by: str = Field(min_length=1, max_length=128)
    approved_at: datetime

    @field_validator("approved_at")
    @classmethod
    def approved_at_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value, "approved_at")


class SimulationExecutionDirective(ContractModel):
    project_id: str = Field(min_length=1)
    candidate_hash: HashValue = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    simulation_request_hash: HashValue = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    approval_hash: HashValue = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=128)
    expected_result_kinds: tuple[str, ...] = Field(min_length=1)
    execution_authority: Literal["user_approved_candidate_only"] = (
        "user_approved_candidate_only"
    )
    release_authority: Literal["forge_policy_only"] = "forge_policy_only"

    @field_validator("expected_result_kinds")
    @classmethod
    def result_kinds_must_be_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique_tuple(value, "expected result kinds")


class IterationEvent(ContractModel):
    project_id: str = Field(min_length=1)
    candidate_hash: HashValue = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sequence: int = Field(ge=1)
    from_phase: IterationPhase
    to_phase: IterationPhase
    reason: str = Field(min_length=1, max_length=400)
    observed_at: datetime
    previous_event_hash: HashValue | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    approval_hash: HashValue | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    simulation_request_hash: HashValue | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    evidence_hashes: tuple[HashValue, ...] = ()

    @field_validator("observed_at")
    @classmethod
    def observed_at_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value, "observed_at")

    @field_validator("evidence_hashes")
    @classmethod
    def evidence_hashes_must_be_unique(
        cls, value: tuple[HashValue, ...]
    ) -> tuple[HashValue, ...]:
        return _unique_tuple(value, "evidence hashes")

    @model_validator(mode="after")
    def transition_must_be_ordered(self) -> IterationEvent:
        allowed = {
            (IterationPhase.TALK, IterationPhase.CONFIRM),
            (IterationPhase.CONFIRM, IterationPhase.SIMULATE),
            (IterationPhase.SIMULATE, IterationPhase.REVISE),
            (IterationPhase.REVISE, IterationPhase.SIMULATE),
            (IterationPhase.SIMULATE, IterationPhase.VERIFY),
            (IterationPhase.VERIFY, IterationPhase.REVISE),
        }
        if (self.from_phase, self.to_phase) not in allowed:
            raise ValueError("iteration phase transition is not allowed")
        if self.sequence == 1 and self.previous_event_hash is not None:
            raise ValueError("first iteration event cannot reference previous hash")
        return self


class LLMIterationRecommendation(ContractModel):
    project_id: str = Field(min_length=1)
    candidate_hash: HashValue = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    recommendation_id: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=1200)
    cited_evidence_hashes: tuple[HashValue, ...] = Field(min_length=1)
    created_at: datetime
    can_execute_simulation: Literal[False] = False
    can_decide_release: Literal[False] = False

    @field_validator("created_at")
    @classmethod
    def created_at_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value, "created_at")

    @field_validator("cited_evidence_hashes")
    @classmethod
    def cited_hashes_must_be_unique(
        cls, value: tuple[HashValue, ...]
    ) -> tuple[HashValue, ...]:
        return _unique_tuple(value, "cited evidence hashes")


class IterationEvidenceState(ContractModel):
    project_id: str = Field(min_length=1)
    candidate_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    latest_simulation_hash: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    missing_required_tiers: tuple[EvidenceTierName, ...] = ()
    failing_required_tiers: tuple[EvidenceTierName, ...] = ()
    user_confirmed_release: bool = False
    latest_evidence_hash: HashValue | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    last_evaluated_evidence_hash: HashValue | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def observed_at_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value, "observed_at")

    @model_validator(mode="after")
    def tiers_must_be_unique(self) -> IterationEvidenceState:
        if len(self.missing_required_tiers) != len(set(self.missing_required_tiers)):
            raise ValueError("missing tiers must be unique")
        if len(self.failing_required_tiers) != len(set(self.failing_required_tiers)):
            raise ValueError("failing tiers must be unique")
        return self


class IterationNextStep(ContractModel):
    decision: IterationDecision
    required_actions: tuple[str, ...]
    next_phase: IterationPhase
    release_authority: str = "forge_policy_only"


def simulation_request_hash(request: SimulationRequest) -> str:
    return canonical_sha256(request)


def simulation_approval_hash(approval: CandidateSimulationApproval) -> str:
    return canonical_sha256(approval)


def iteration_event_hash(event: IterationEvent) -> str:
    return canonical_sha256(event)


def authorize_simulation_request(
    request: SimulationRequest, approval: CandidateSimulationApproval
) -> SimulationExecutionDirective:
    request_hash = simulation_request_hash(request)
    if request.project_id != approval.project_id:
        raise IterationTransitionError("approval project does not match request")
    if request.candidate_hash != approval.candidate_hash:
        raise IterationTransitionError("approval candidate does not match request")
    if request_hash != approval.simulation_request_hash:
        raise IterationTransitionError("approval does not cover exact request hash")
    return SimulationExecutionDirective(
        project_id=request.project_id,
        candidate_hash=request.candidate_hash,
        simulation_request_hash=request_hash,
        approval_hash=simulation_approval_hash(approval),
        idempotency_key=request.idempotency_key,
        expected_result_kinds=request.expected_result_kinds,
    )


def append_iteration_event(
    history: tuple[IterationEvent, ...], event: IterationEvent
) -> tuple[IterationEvent, ...]:
    if not history:
        if event.sequence != 1:
            raise IterationTransitionError("first iteration event must be sequence 1")
        if event.previous_event_hash is not None:
            raise IterationTransitionError("first iteration event cannot be chained")
        return (event,)

    previous = history[-1]
    if event.project_id != previous.project_id:
        raise IterationTransitionError("iteration event project changed")
    if event.candidate_hash != previous.candidate_hash:
        raise IterationTransitionError("iteration event candidate changed")
    if event.sequence != previous.sequence + 1:
        raise IterationTransitionError("iteration event sequence must be contiguous")
    if event.previous_event_hash != iteration_event_hash(previous):
        raise IterationTransitionError("iteration event previous hash mismatch")
    event_hash = iteration_event_hash(event)
    if any(iteration_event_hash(item) == event_hash for item in history):
        raise IterationTransitionError("iteration event replay detected")
    return (*history, event)


def decide_next_iteration(state: IterationEvidenceState) -> IterationNextStep:
    if (
        state.latest_evidence_hash is not None
        and state.last_evaluated_evidence_hash == state.latest_evidence_hash
    ):
        return IterationNextStep(
            decision=IterationDecision.WAIT_FOR_NEW_EVIDENCE,
            required_actions=(
                "wait for new evidence before reevaluating this candidate",
            ),
            next_phase=IterationPhase.SIMULATE,
        )
    if state.missing_required_tiers:
        return IterationNextStep(
            decision=IterationDecision.COLLECT_REQUIRED_EVIDENCE,
            required_actions=tuple(
                f"collect {tier} evidence for candidate {state.candidate_hash}"
                for tier in state.missing_required_tiers
            ),
            next_phase=IterationPhase.SIMULATE,
        )
    if state.failing_required_tiers:
        return IterationNextStep(
            decision=IterationDecision.REVISE_AND_RESIMULATE,
            required_actions=tuple(
                f"revise design and rerun {tier} evidence"
                for tier in state.failing_required_tiers
            ),
            next_phase=IterationPhase.REVISE,
        )
    if not state.user_confirmed_release:
        return IterationNextStep(
            decision=IterationDecision.INTERPRET_WITH_USER,
            required_actions=(
                "explain source-bound results and ask user whether to verify release",
            ),
            next_phase=IterationPhase.CONFIRM,
        )
    return IterationNextStep(
        decision=IterationDecision.VERIFY_RELEASE,
        required_actions=("call deterministic FORGE release verification",),
        next_phase=IterationPhase.VERIFY,
    )


__all__ = [
    "CandidateSimulationApproval",
    "IterationEvent",
    "IterationDecision",
    "IterationEvidenceState",
    "IterationNextStep",
    "IterationPhase",
    "IterationTransitionError",
    "LLMIterationRecommendation",
    "SimulationExecutionDirective",
    "SimulationRequest",
    "append_iteration_event",
    "authorize_simulation_request",
    "decide_next_iteration",
    "iteration_event_hash",
    "simulation_approval_hash",
    "simulation_request_hash",
]
