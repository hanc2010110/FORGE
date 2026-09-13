from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum
from threading import Lock
from typing import Literal, Protocol

from pydantic import Field, field_validator, model_validator

from forge_core.conversational_design import DesignConversationState
from forge_core.hashing import canonical_sha256
from forge_core.models import ContractModel

EvidenceTierName = str
HashValue = str
_CANONICAL_HASH = re.compile(r"sha256:[0-9a-f]{64}")


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
    next_state: DesignConversationState
    release_authority: str = "forge_policy_only"


class AutomaticReverificationResult(ContractModel):
    next_step: IterationNextStep
    verification_invoked: bool
    evaluated_evidence_hash: HashValue | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    release_decision_hash: HashValue | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    release_status: Literal["READY", "BLOCKED"] | None = None

    @model_validator(mode="after")
    def invocation_fields_must_be_coherent(self) -> AutomaticReverificationResult:
        has_outcome = (
            self.evaluated_evidence_hash is not None
            and self.release_decision_hash is not None
            and self.release_status is not None
        )
        if self.verification_invoked != has_outcome:
            raise ValueError("automatic reverification result is inconsistent")
        return self


class DeterministicReleaseOutcome(ContractModel):
    status: Literal["READY", "BLOCKED"]
    decision_hash: HashValue = Field(pattern=r"^sha256:[0-9a-f]{64}$")


def next_step_for_release_outcome(
    outcome: DeterministicReleaseOutcome,
) -> IterationNextStep:
    if outcome.status == "READY":
        return IterationNextStep(
            decision=IterationDecision.VERIFY_RELEASE,
            required_actions=("release evidence is READY under FORGE policy",),
            next_state=DesignConversationState.ACCEPTED,
        )
    return IterationNextStep(
        decision=IterationDecision.REVISE_AND_RESIMULATE,
        required_actions=(
            "inspect deterministic BLOCKED reasons, revise, and collect new evidence",
        ),
        next_state=DesignConversationState.REVISED,
    )


class IterationCheckpointStore(Protocol):
    """Atomic replay boundary for evidence-triggered release verification."""

    def last_evaluated_hash(
        self, *, project_id: str, candidate_hash: str
    ) -> str | None: ...

    def verify_once(
        self,
        *,
        project_id: str,
        candidate_hash: str,
        evidence_hash: str,
        evaluator: Callable[[], DeterministicReleaseOutcome],
    ) -> tuple[bool, DeterministicReleaseOutcome | None]: ...


class InMemoryIterationCheckpointStore:
    """Thread-safe development store; production can inject durable persistence."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._evaluated: dict[tuple[str, str, str], DeterministicReleaseOutcome] = {}
        self._latest: dict[tuple[str, str], str] = {}

    def last_evaluated_hash(
        self, *, project_id: str, candidate_hash: str
    ) -> str | None:
        with self._lock:
            return self._latest.get((project_id, candidate_hash))

    def verify_once(
        self,
        *,
        project_id: str,
        candidate_hash: str,
        evidence_hash: str,
        evaluator: Callable[[], DeterministicReleaseOutcome],
    ) -> tuple[bool, DeterministicReleaseOutcome | None]:
        key = (project_id, candidate_hash, evidence_hash)
        with self._lock:
            checkpoint = self._evaluated.get(key)
            if checkpoint is not None:
                self._latest[(project_id, candidate_hash)] = evidence_hash
                return False, checkpoint
            outcome = evaluator()
            if _CANONICAL_HASH.fullmatch(outcome.decision_hash) is None:
                raise IterationTransitionError(
                    "release verifier returned a non-canonical decision hash"
                )
            self._evaluated[key] = outcome
            self._latest[(project_id, candidate_hash)] = evidence_hash
            return True, outcome


class ApprovedIterationOrchestrator:
    """Routes newly recorded evidence to deterministic verification when approved."""

    def __init__(
        self,
        *,
        checkpoint_store: IterationCheckpointStore,
        verify_release: Callable[[str, str, str], DeterministicReleaseOutcome],
    ) -> None:
        self._checkpoint_store = checkpoint_store
        self._verify_release = verify_release

    def on_evidence_recorded(
        self, state: IterationEvidenceState
    ) -> AutomaticReverificationResult:
        stored_hash = self._checkpoint_store.last_evaluated_hash(
            project_id=state.project_id,
            candidate_hash=state.candidate_hash,
        )
        if (
            stored_hash is not None
            and state.last_evaluated_evidence_hash is not None
            and stored_hash != state.last_evaluated_evidence_hash
        ):
            raise IterationTransitionError(
                "persisted and supplied iteration checkpoints do not match"
            )
        effective_state = state.model_copy(
            update={
                "last_evaluated_evidence_hash": stored_hash
                or state.last_evaluated_evidence_hash
            }
        )
        next_step = decide_next_iteration(effective_state)
        if next_step.decision is not IterationDecision.VERIFY_RELEASE:
            return AutomaticReverificationResult(
                next_step=next_step,
                verification_invoked=False,
            )
        evidence_hash = state.latest_evidence_hash
        if evidence_hash is None:
            raise IterationTransitionError(
                "automatic reverification requires a recorded evidence hash"
            )
        invoked, outcome = self._checkpoint_store.verify_once(
            project_id=state.project_id,
            candidate_hash=state.candidate_hash,
            evidence_hash=evidence_hash,
            evaluator=lambda: self._verify_release(
                state.project_id, state.candidate_hash, evidence_hash
            ),
        )
        if not invoked:
            return AutomaticReverificationResult(
                next_step=IterationNextStep(
                    decision=IterationDecision.WAIT_FOR_NEW_EVIDENCE,
                    required_actions=(
                        "wait for new evidence before reevaluating this candidate",
                    ),
                    next_state=DesignConversationState.SIMULATED,
                ),
                verification_invoked=False,
            )
        if outcome is None:
            raise IterationTransitionError("release verifier returned no outcome")
        verified_step = next_step_for_release_outcome(outcome)
        return AutomaticReverificationResult(
            next_step=verified_step,
            verification_invoked=True,
            evaluated_evidence_hash=evidence_hash,
            release_decision_hash=outcome.decision_hash,
            release_status=outcome.status,
        )


def simulation_request_hash(request: SimulationRequest) -> str:
    return canonical_sha256(request)


def simulation_approval_hash(approval: CandidateSimulationApproval) -> str:
    return canonical_sha256(approval)


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
            next_state=DesignConversationState.SIMULATED,
        )
    if state.missing_required_tiers:
        return IterationNextStep(
            decision=IterationDecision.COLLECT_REQUIRED_EVIDENCE,
            required_actions=tuple(
                f"collect {tier} evidence for candidate {state.candidate_hash}"
                for tier in state.missing_required_tiers
            ),
            next_state=DesignConversationState.CONFIRMED,
        )
    if state.failing_required_tiers:
        return IterationNextStep(
            decision=IterationDecision.REVISE_AND_RESIMULATE,
            required_actions=tuple(
                f"revise design and rerun {tier} evidence"
                for tier in state.failing_required_tiers
            ),
            next_state=DesignConversationState.REVISED,
        )
    if not state.user_confirmed_release:
        return IterationNextStep(
            decision=IterationDecision.INTERPRET_WITH_USER,
            required_actions=(
                "explain source-bound results and ask user whether to verify release",
            ),
            next_state=DesignConversationState.SIMULATED,
        )
    return IterationNextStep(
        decision=IterationDecision.VERIFY_RELEASE,
        required_actions=("call deterministic FORGE release verification",),
        next_state=DesignConversationState.ACCEPTED,
    )


__all__ = [
    "ApprovedIterationOrchestrator",
    "AutomaticReverificationResult",
    "CandidateSimulationApproval",
    "DeterministicReleaseOutcome",
    "InMemoryIterationCheckpointStore",
    "IterationCheckpointStore",
    "IterationDecision",
    "IterationEvidenceState",
    "IterationNextStep",
    "IterationTransitionError",
    "LLMIterationRecommendation",
    "SimulationExecutionDirective",
    "SimulationRequest",
    "authorize_simulation_request",
    "decide_next_iteration",
    "next_step_for_release_outcome",
    "simulation_approval_hash",
    "simulation_request_hash",
]
