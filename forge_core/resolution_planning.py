from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from forge_core.change_management import (
    ArtifactDomain,
    EvidenceTier,
    FindingSeverity,
    RetestStatus,
)
from forge_core.hashing import canonical_sha256
from forge_core.models import ContractModel, SourceRef, Verdict
from forge_core.release_readiness import ReleaseReadinessDecision, TestExecutionEvidence


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


class GoalPriority(StrEnum):
    COST = "cost"
    MINIMAL_CHANGE = "minimal_change"
    PERFORMANCE = "performance"
    RELIABILITY = "reliability"


class DesignStrategy(StrEnum):
    MINIMAL_CHANGE = "minimal_change"
    PERFORMANCE = "performance"
    COST_OPTIMIZED = "cost_optimized"
    CUSTOM = "custom"


class DiagnosisConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INSUFFICIENT = "insufficient"


class EngineeringGoalPayload(ContractModel):
    goal_statement: str
    priority: GoalPriority
    constraints: tuple[str, ...]
    source_refs: tuple[SourceRef, ...]


class EngineeringGoal(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    goal_statement: str = Field(min_length=1)
    priority: GoalPriority
    constraints: tuple[str, ...] = ()
    source_refs: tuple[SourceRef, ...] = Field(min_length=1)
    goal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("constraints")
    @classmethod
    def constraints_must_be_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("goal constraints must be unique")
        return value

    @model_validator(mode="after")
    def hash_must_match_payload(self) -> EngineeringGoal:
        expected = engineering_goal_hash(
            self.goal_statement, self.priority, self.constraints, self.source_refs
        )
        if self.goal_hash != expected:
            raise ValueError("engineering goal hash does not match payload")
        return self


class TradeoffScores(ContractModel):
    cost: int = Field(ge=0, le=100)
    change: int = Field(ge=0, le=100)
    performance: int = Field(ge=0, le=100)
    risk: int = Field(ge=0, le=100)
    goal_satisfaction: int = Field(ge=0, le=100)


class DesignAlternativePayload(ContractModel):
    alternative_id: str
    strategy: DesignStrategy
    proposed_change_refs: tuple[str, ...]
    affected_domains: tuple[ArtifactDomain, ...]
    tradeoff_scores: TradeoffScores
    rationale: str
    assumptions: tuple[str, ...]
    missing_information: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    planning_only: bool


class DesignAlternative(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    alternative_id: str = Field(min_length=1)
    strategy: DesignStrategy
    proposed_change_refs: tuple[str, ...] = Field(min_length=1)
    affected_domains: tuple[ArtifactDomain, ...] = Field(min_length=1)
    tradeoff_scores: TradeoffScores
    rationale: str = Field(min_length=1)
    assumptions: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    planning_only: Literal[True] = True
    alternative_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator(
        "proposed_change_refs",
        "affected_domains",
        "assumptions",
        "missing_information",
        "evidence_refs",
    )
    @classmethod
    def tuple_values_must_be_unique(
        cls, value: tuple[object, ...]
    ) -> tuple[object, ...]:
        if len(value) != len(set(value)):
            raise ValueError("design alternative tuple values must be unique")
        return value

    @model_validator(mode="after")
    def hash_must_match_payload(self) -> DesignAlternative:
        expected = design_alternative_hash(
            self.alternative_id,
            self.strategy,
            self.proposed_change_refs,
            self.affected_domains,
            self.tradeoff_scores,
            self.rationale,
            self.assumptions,
            self.missing_information,
            self.evidence_refs,
        )
        if self.alternative_hash != expected:
            raise ValueError("design alternative hash does not match payload")
        return self


class DesignProposalSetPayload(ContractModel):
    project_id: str
    baseline_snapshot_id: str
    baseline_snapshot_hash: str
    preview_hash: str
    goal: EngineeringGoal
    alternatives: tuple[DesignAlternative, ...]
    planning_only: bool


class DesignProposalSet(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    baseline_snapshot_id: str = Field(min_length=1)
    baseline_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    preview_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    goal: EngineeringGoal
    alternatives: tuple[DesignAlternative, ...] = Field(min_length=2)
    planning_only: Literal[True] = True
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def alternatives_and_hash_must_be_canonical(self) -> DesignProposalSet:
        ids = [item.alternative_id for item in self.alternatives]
        if len(ids) != len(set(ids)):
            raise ValueError("design alternatives must have unique IDs")
        if self.alternatives != tuple(
            sorted(self.alternatives, key=lambda item: item.alternative_id)
        ):
            raise ValueError("design alternatives must be canonically ordered")
        expected = design_proposal_set_hash(
            self.project_id,
            self.baseline_snapshot_id,
            self.baseline_snapshot_hash,
            self.preview_hash,
            self.goal,
            self.alternatives,
        )
        if self.proposal_hash != expected:
            raise ValueError("design proposal hash does not match payload")
        return self


class DiagnosisHypothesis(ContractModel):
    hypothesis_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)

    @field_validator("evidence_refs")
    @classmethod
    def evidence_refs_must_be_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("diagnosis hypothesis evidence refs must be unique")
        return value


class DiagnosisItemPayload(ContractModel):
    item_id: str
    blocker_code: str
    affected_domains: tuple[ArtifactDomain, ...]
    hypotheses: tuple[DiagnosisHypothesis, ...]
    confidence: DiagnosisConfidence
    evidence_refs: tuple[str, ...]
    selected_test_evidence: TestExecutionEvidence | None
    additional_verification: tuple[str, ...]
    planning_only: bool


class DiagnosisItem(ContractModel):
    item_id: str = Field(min_length=1)
    blocker_code: str = Field(min_length=1)
    affected_domains: tuple[ArtifactDomain, ...] = Field(min_length=1)
    hypotheses: tuple[DiagnosisHypothesis, ...] = ()
    confidence: DiagnosisConfidence
    evidence_refs: tuple[str, ...] = ()
    selected_test_evidence: TestExecutionEvidence | None = None
    additional_verification: tuple[str, ...] = ()
    planning_only: Literal[True] = True
    item_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("affected_domains", "evidence_refs", "additional_verification")
    @classmethod
    def tuple_values_must_be_unique(
        cls, value: tuple[object, ...]
    ) -> tuple[object, ...]:
        if len(value) != len(set(value)):
            raise ValueError("diagnosis item tuple values must be unique")
        return value

    @model_validator(mode="after")
    def evidence_contract_must_match_confidence(self) -> DiagnosisItem:
        if self.confidence is DiagnosisConfidence.INSUFFICIENT:
            if not self.additional_verification:
                raise ValueError(
                    "insufficient diagnosis requires additional verification"
                )
            if self.hypotheses:
                raise ValueError(
                    "insufficient diagnosis cannot present supported hypotheses"
                )
        elif not self.evidence_refs or not self.hypotheses:
            raise ValueError("supported diagnosis requires evidence-bound hypotheses")
        for hypothesis in self.hypotheses:
            if not set(hypothesis.evidence_refs).issubset(self.evidence_refs):
                raise ValueError("hypothesis evidence must be bound to diagnosis item")
        expected = diagnosis_item_hash(
            self.item_id,
            self.blocker_code,
            self.affected_domains,
            self.hypotheses,
            self.confidence,
            self.evidence_refs,
            self.selected_test_evidence,
            self.additional_verification,
        )
        if self.item_hash != expected:
            raise ValueError("diagnosis item hash does not match payload")
        return self


class BlockerDiagnosisPayload(ContractModel):
    project_id: str
    decision_hash: str
    decision: ReleaseReadinessDecision
    items: tuple[DiagnosisItem, ...]
    planning_only: bool


class BlockerDiagnosis(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    decision_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    decision: ReleaseReadinessDecision
    items: tuple[DiagnosisItem, ...]
    planning_only: Literal[True] = True
    diagnosis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def items_must_cover_blockers_and_hash(self) -> BlockerDiagnosis:
        if self.decision_hash != self.decision.decision_hash:
            raise ValueError("diagnosis decision hash does not match decision")
        if self.project_id != self.decision.report.project_id:
            raise ValueError("diagnosis project must match release decision")
        item_ids = [item.item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("diagnosis item IDs must be unique")
        if self.items != tuple(sorted(self.items, key=lambda item: item.blocker_code)):
            raise ValueError("diagnosis items must be canonically ordered")
        if tuple(item.blocker_code for item in self.items) != tuple(
            self.decision.report.blocker_codes
        ):
            raise ValueError("diagnosis must contain exactly one item per blocker")
        expected = blocker_diagnosis_hash(
            self.project_id, self.decision_hash, self.decision, self.items
        )
        if self.diagnosis_hash != expected:
            raise ValueError("blocker diagnosis hash does not match payload")
        return self


class ProposedChange(ContractModel):
    change_ref: str = Field(min_length=1)
    domain: ArtifactDomain
    description: str = Field(min_length=1)


class RequiredReverifyTest(ContractModel):
    test_id: str = Field(min_length=1)
    required_tier: EvidenceTier
    reason: str = Field(min_length=1)


class FixProposalPayload(ContractModel):
    fix_id: str
    diagnosis_item_hash: str
    blocker_code: str
    affected_domains: tuple[ArtifactDomain, ...]
    tradeoff_scores: TradeoffScores
    changes: tuple[ProposedChange, ...]
    required_reverify_tests: tuple[RequiredReverifyTest, ...]
    rationale: str
    no_writeback: bool
    planning_only: bool


class FixProposal(ContractModel):
    fix_id: str = Field(min_length=1)
    diagnosis_item_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    blocker_code: str = Field(min_length=1)
    affected_domains: tuple[ArtifactDomain, ...] = Field(min_length=1)
    tradeoff_scores: TradeoffScores
    changes: tuple[ProposedChange, ...] = Field(min_length=1)
    required_reverify_tests: tuple[RequiredReverifyTest, ...] = Field(min_length=1)
    rationale: str = Field(min_length=1)
    no_writeback: Literal[True] = True
    planning_only: Literal[True] = True
    fix_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("affected_domains")
    @classmethod
    def domains_must_be_unique(
        cls, value: tuple[ArtifactDomain, ...]
    ) -> tuple[ArtifactDomain, ...]:
        if len(value) != len(set(value)):
            raise ValueError("fix affected domains must be unique")
        return value

    @model_validator(mode="after")
    def hash_must_match_payload(self) -> FixProposal:
        if {item.domain for item in self.changes}.difference(self.affected_domains):
            raise ValueError("fix changes must stay inside affected domains")
        test_keys = [
            (item.test_id, item.required_tier) for item in self.required_reverify_tests
        ]
        if len(test_keys) != len(set(test_keys)):
            raise ValueError("fix reverify tests must be unique")
        expected = fix_proposal_hash(
            self.fix_id,
            self.diagnosis_item_hash,
            self.blocker_code,
            self.affected_domains,
            self.tradeoff_scores,
            self.changes,
            self.required_reverify_tests,
            self.rationale,
        )
        if self.fix_hash != expected:
            raise ValueError("fix proposal hash does not match payload")
        return self


class FixProposalSetPayload(ContractModel):
    project_id: str
    diagnosis_hash: str
    diagnosis_item_hash: str
    blocker_code: str
    proposals: tuple[FixProposal, ...]
    no_writeback: bool
    planning_only: bool


class FixProposalSet(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    diagnosis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    diagnosis_item_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    blocker_code: str = Field(min_length=1)
    proposals: tuple[FixProposal, ...] = Field(min_length=1)
    no_writeback: Literal[True] = True
    planning_only: Literal[True] = True
    fix_set_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def proposals_must_bind_blocker_and_hash(self) -> FixProposalSet:
        ids = [item.fix_id for item in self.proposals]
        if len(ids) != len(set(ids)):
            raise ValueError("fix proposal IDs must be unique")
        if self.proposals != tuple(
            sorted(self.proposals, key=lambda item: item.fix_id)
        ):
            raise ValueError("fix proposals must be canonically ordered")
        if any(
            item.diagnosis_item_hash != self.diagnosis_item_hash
            or item.blocker_code != self.blocker_code
            for item in self.proposals
        ):
            raise ValueError("fix proposals must bind the same diagnosis blocker")
        expected = fix_proposal_set_hash(
            self.project_id,
            self.diagnosis_hash,
            self.diagnosis_item_hash,
            self.blocker_code,
            self.proposals,
        )
        if self.fix_set_hash != expected:
            raise ValueError("fix proposal set hash does not match payload")
        return self


class ReplanSeedPayload(ContractModel):
    project_id: str
    source_decision_hash: str
    diagnosis_hash: str
    diagnosis_item_hash: str
    blocker_code: str
    selected_fix_id: str
    selected_fix_hash: str
    changes: tuple[ProposedChange, ...]
    required_reverify_tests: tuple[RequiredReverifyTest, ...]


class ReplanSeed(ContractModel):
    project_id: str = Field(min_length=1)
    source_decision_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    diagnosis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    diagnosis_item_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    blocker_code: str = Field(min_length=1)
    selected_fix_id: str = Field(min_length=1)
    selected_fix_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    changes: tuple[ProposedChange, ...] = Field(min_length=1)
    required_reverify_tests: tuple[RequiredReverifyTest, ...] = Field(min_length=1)
    seed_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def hash_must_match_payload(self) -> ReplanSeed:
        expected = replan_seed_hash(
            self.project_id,
            self.source_decision_hash,
            self.diagnosis_hash,
            self.diagnosis_item_hash,
            self.blocker_code,
            self.selected_fix_id,
            self.selected_fix_hash,
            self.changes,
            self.required_reverify_tests,
        )
        if self.seed_hash != expected:
            raise ValueError("replan seed hash does not match payload")
        return self


class ProposalSelectionPayload(ContractModel):
    project_id: str
    fix_set: FixProposalSet
    fix_set_hash: str
    selected_fix_id: str
    selected_fix_hash: str
    selected_fix: FixProposal
    selected_by: str
    selected_at: datetime
    replan_seed: ReplanSeed
    lineage: tuple[str, ...]
    planning_only: bool


class ProposalSelection(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    fix_set: FixProposalSet
    fix_set_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    selected_fix_id: str = Field(min_length=1)
    selected_fix_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    selected_fix: FixProposal
    selected_by: str = Field(min_length=1)
    selected_at: datetime
    replan_seed: ReplanSeed
    lineage: tuple[str, ...] = Field(min_length=1)
    planning_only: Literal[True] = True
    selection_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("selected_at")
    @classmethod
    def selected_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "selected_at")

    @model_validator(mode="after")
    def selection_must_bind_exact_fix_and_seed(self) -> ProposalSelection:
        if self.project_id != self.fix_set.project_id:
            raise ValueError("selection project must match fix set")
        if self.fix_set_hash != self.fix_set.fix_set_hash:
            raise ValueError("selection fix set hash does not match fix set")
        matching = tuple(
            item
            for item in self.fix_set.proposals
            if item.fix_id == self.selected_fix_id
        )
        if len(matching) != 1:
            raise ValueError("selected fix ID must exist exactly once in fix set")
        exact = matching[0]
        if (
            exact != self.selected_fix
            or self.selected_fix_hash != exact.fix_hash
            or self.selected_fix.fix_hash != self.selected_fix_hash
        ):
            raise ValueError("selection must bind the exact selected fix")
        expected_seed = build_replan_seed(
            project_id=self.project_id,
            source_decision_hash=self.lineage[0],
            diagnosis_hash=self.fix_set.diagnosis_hash,
            diagnosis_item_hash=self.fix_set.diagnosis_item_hash,
            blocker_code=self.fix_set.blocker_code,
            selected_fix=exact,
        )
        if self.replan_seed != expected_seed:
            raise ValueError("selection replan seed does not match selected fix")
        if self.lineage != (
            self.replan_seed.source_decision_hash,
            self.fix_set.diagnosis_hash,
            self.fix_set.diagnosis_item_hash,
            self.fix_set.fix_set_hash,
            exact.fix_hash,
            self.replan_seed.seed_hash,
        ):
            raise ValueError("selection lineage must be canonical and complete")
        expected = proposal_selection_hash(
            self.project_id,
            self.fix_set,
            self.fix_set_hash,
            self.selected_fix_id,
            self.selected_fix_hash,
            self.selected_fix,
            self.selected_by,
            self.selected_at,
            self.replan_seed,
            self.lineage,
        )
        if self.selection_hash != expected:
            raise ValueError("proposal selection hash does not match payload")
        return self


def engineering_goal_hash(
    goal_statement: str,
    priority: GoalPriority,
    constraints: tuple[str, ...],
    source_refs: tuple[SourceRef, ...],
) -> str:
    return canonical_sha256(
        EngineeringGoalPayload(
            goal_statement=goal_statement,
            priority=priority,
            constraints=constraints,
            source_refs=source_refs,
        )
    )


def build_engineering_goal(
    goal_statement: str,
    priority: GoalPriority,
    source_refs: tuple[SourceRef, ...],
    constraints: tuple[str, ...] = (),
) -> EngineeringGoal:
    return EngineeringGoal(
        goal_statement=goal_statement,
        priority=priority,
        constraints=constraints,
        source_refs=source_refs,
        goal_hash=engineering_goal_hash(
            goal_statement, priority, constraints, source_refs
        ),
    )


def design_alternative_hash(
    alternative_id: str,
    strategy: DesignStrategy,
    proposed_change_refs: tuple[str, ...],
    affected_domains: tuple[ArtifactDomain, ...],
    tradeoff_scores: TradeoffScores,
    rationale: str,
    assumptions: tuple[str, ...],
    missing_information: tuple[str, ...],
    evidence_refs: tuple[str, ...],
) -> str:
    return canonical_sha256(
        DesignAlternativePayload(
            alternative_id=alternative_id,
            strategy=strategy,
            proposed_change_refs=proposed_change_refs,
            affected_domains=affected_domains,
            tradeoff_scores=tradeoff_scores,
            rationale=rationale,
            assumptions=assumptions,
            missing_information=missing_information,
            evidence_refs=evidence_refs,
            planning_only=True,
        )
    )


def build_design_alternative(
    *,
    alternative_id: str,
    strategy: DesignStrategy,
    proposed_change_refs: tuple[str, ...],
    affected_domains: tuple[ArtifactDomain, ...],
    tradeoff_scores: TradeoffScores,
    rationale: str,
    assumptions: tuple[str, ...] = (),
    missing_information: tuple[str, ...] = (),
    evidence_refs: tuple[str, ...] = (),
) -> DesignAlternative:
    return DesignAlternative(
        alternative_id=alternative_id,
        strategy=strategy,
        proposed_change_refs=proposed_change_refs,
        affected_domains=affected_domains,
        tradeoff_scores=tradeoff_scores,
        rationale=rationale,
        assumptions=assumptions,
        missing_information=missing_information,
        evidence_refs=evidence_refs,
        alternative_hash=design_alternative_hash(
            alternative_id,
            strategy,
            proposed_change_refs,
            affected_domains,
            tradeoff_scores,
            rationale,
            assumptions,
            missing_information,
            evidence_refs,
        ),
    )


def design_proposal_set_hash(
    project_id: str,
    baseline_snapshot_id: str,
    baseline_snapshot_hash: str,
    preview_hash: str,
    goal: EngineeringGoal,
    alternatives: tuple[DesignAlternative, ...],
) -> str:
    return canonical_sha256(
        DesignProposalSetPayload(
            project_id=project_id,
            baseline_snapshot_id=baseline_snapshot_id,
            baseline_snapshot_hash=baseline_snapshot_hash,
            preview_hash=preview_hash,
            goal=goal,
            alternatives=alternatives,
            planning_only=True,
        )
    )


def build_design_proposal_set(
    *,
    project_id: str,
    baseline_snapshot_id: str,
    baseline_snapshot_hash: str,
    preview_hash: str,
    goal: EngineeringGoal,
    alternatives: tuple[DesignAlternative, ...],
) -> DesignProposalSet:
    ordered = tuple(sorted(alternatives, key=lambda item: item.alternative_id))
    return DesignProposalSet(
        project_id=project_id,
        baseline_snapshot_id=baseline_snapshot_id,
        baseline_snapshot_hash=baseline_snapshot_hash,
        preview_hash=preview_hash,
        goal=goal,
        alternatives=ordered,
        proposal_hash=design_proposal_set_hash(
            project_id,
            baseline_snapshot_id,
            baseline_snapshot_hash,
            preview_hash,
            goal,
            ordered,
        ),
    )


def diagnosis_item_hash(
    item_id: str,
    blocker_code: str,
    affected_domains: tuple[ArtifactDomain, ...],
    hypotheses: tuple[DiagnosisHypothesis, ...],
    confidence: DiagnosisConfidence,
    evidence_refs: tuple[str, ...],
    selected_test_evidence: TestExecutionEvidence | None,
    additional_verification: tuple[str, ...],
) -> str:
    return canonical_sha256(
        DiagnosisItemPayload(
            item_id=item_id,
            blocker_code=blocker_code,
            affected_domains=affected_domains,
            hypotheses=hypotheses,
            confidence=confidence,
            evidence_refs=evidence_refs,
            selected_test_evidence=selected_test_evidence,
            additional_verification=additional_verification,
            planning_only=True,
        )
    )


def build_diagnosis_item(
    *,
    item_id: str,
    blocker_code: str,
    affected_domains: tuple[ArtifactDomain, ...],
    confidence: DiagnosisConfidence,
    hypotheses: tuple[DiagnosisHypothesis, ...] = (),
    evidence_refs: tuple[str, ...] = (),
    selected_test_evidence: TestExecutionEvidence | None = None,
    additional_verification: tuple[str, ...] = (),
) -> DiagnosisItem:
    return DiagnosisItem(
        item_id=item_id,
        blocker_code=blocker_code,
        affected_domains=affected_domains,
        hypotheses=hypotheses,
        confidence=confidence,
        evidence_refs=evidence_refs,
        selected_test_evidence=selected_test_evidence,
        additional_verification=additional_verification,
        item_hash=diagnosis_item_hash(
            item_id,
            blocker_code,
            affected_domains,
            hypotheses,
            confidence,
            evidence_refs,
            selected_test_evidence,
            additional_verification,
        ),
    )


def blocker_diagnosis_hash(
    project_id: str,
    decision_hash: str,
    decision: ReleaseReadinessDecision,
    items: tuple[DiagnosisItem, ...],
) -> str:
    return canonical_sha256(
        BlockerDiagnosisPayload(
            project_id=project_id,
            decision_hash=decision_hash,
            decision=decision,
            items=items,
            planning_only=True,
        )
    )


def build_blocker_diagnosis(decision: ReleaseReadinessDecision) -> BlockerDiagnosis:
    selected_tests = {item.evidence_id: item for item in decision.selected_test_results}
    report_test_evidence = {
        (item.test_id, item.tier): item for item in decision.report.evidence
    }
    findings_by_rule = {
        item.rule_id: item
        for item in decision.report.findings
        if item.severity is FindingSeverity.BLOCKER
    }
    retests_by_code = {
        f"retest:{item.test_id}:{item.required_tier.value}:{item.status.value}": item
        for item in decision.report.required_retests
        if item.status is not RetestStatus.PASSED
    }

    items: list[DiagnosisItem] = []
    for index, blocker_code in enumerate(decision.report.blocker_codes, start=1):
        if blocker_code.startswith("finding:"):
            rule_id = blocker_code.removeprefix("finding:")
            finding = findings_by_rule[rule_id]
            evidence_refs = finding.evidence_refs
            item = build_diagnosis_item(
                item_id=f"diagnosis-{index:03d}",
                blocker_code=blocker_code,
                affected_domains=finding.affected_domains,
                hypotheses=(
                    DiagnosisHypothesis(
                        hypothesis_id=f"hypothesis-{index:03d}-001",
                        statement=(
                            "Finding is release-blocking and must be resolved: "
                            f"{finding.summary}"
                        ),
                        evidence_refs=evidence_refs,
                    ),
                ),
                confidence=DiagnosisConfidence.MEDIUM,
                evidence_refs=evidence_refs,
            )
        else:
            retest = retests_by_code[blocker_code]
            reverify = (
                f"Collect {retest.required_tier.value} evidence for "
                f"{retest.test_id} against decision {decision.decision_hash}"
            )
            evidence = report_test_evidence.get((retest.test_id, retest.required_tier))
            selected_test = (
                selected_tests.get(evidence.evidence_id)
                if evidence is not None
                and evidence.verdict is Verdict.FAIL
                and evidence.evidence_id in selected_tests
                else None
            )
            if selected_test is None:
                item = build_diagnosis_item(
                    item_id=f"diagnosis-{index:03d}",
                    blocker_code=blocker_code,
                    affected_domains=(ArtifactDomain.TEST,),
                    confidence=DiagnosisConfidence.INSUFFICIENT,
                    additional_verification=(reverify,),
                )
            else:
                item = build_diagnosis_item(
                    item_id=f"diagnosis-{index:03d}",
                    blocker_code=blocker_code,
                    affected_domains=(ArtifactDomain.TEST,),
                    hypotheses=(
                        DiagnosisHypothesis(
                            hypothesis_id=f"hypothesis-{index:03d}-001",
                            statement=(
                                "Required retest failed; failure is evidence-bound "
                                "but root cause remains unconfirmed."
                            ),
                            evidence_refs=(selected_test.evidence_id,),
                        ),
                    ),
                    confidence=DiagnosisConfidence.LOW,
                    evidence_refs=(selected_test.evidence_id,),
                    selected_test_evidence=selected_test,
                    additional_verification=(reverify,),
                )
        items.append(item)

    ordered = tuple(sorted(items, key=lambda item: item.blocker_code))
    return BlockerDiagnosis(
        project_id=decision.report.project_id,
        decision_hash=decision.decision_hash,
        decision=decision,
        items=ordered,
        diagnosis_hash=blocker_diagnosis_hash(
            decision.report.project_id, decision.decision_hash, decision, ordered
        ),
    )


def fix_proposal_hash(
    fix_id: str,
    diagnosis_item_hash: str,
    blocker_code: str,
    affected_domains: tuple[ArtifactDomain, ...],
    tradeoff_scores: TradeoffScores,
    changes: tuple[ProposedChange, ...],
    required_reverify_tests: tuple[RequiredReverifyTest, ...],
    rationale: str,
) -> str:
    return canonical_sha256(
        FixProposalPayload(
            fix_id=fix_id,
            diagnosis_item_hash=diagnosis_item_hash,
            blocker_code=blocker_code,
            affected_domains=affected_domains,
            tradeoff_scores=tradeoff_scores,
            changes=changes,
            required_reverify_tests=required_reverify_tests,
            rationale=rationale,
            no_writeback=True,
            planning_only=True,
        )
    )


def build_fix_proposal(
    *,
    fix_id: str,
    diagnosis_item: DiagnosisItem,
    affected_domains: tuple[ArtifactDomain, ...],
    tradeoff_scores: TradeoffScores,
    changes: tuple[ProposedChange, ...],
    required_reverify_tests: tuple[RequiredReverifyTest, ...],
    rationale: str,
) -> FixProposal:
    return FixProposal(
        fix_id=fix_id,
        diagnosis_item_hash=diagnosis_item.item_hash,
        blocker_code=diagnosis_item.blocker_code,
        affected_domains=affected_domains,
        tradeoff_scores=tradeoff_scores,
        changes=changes,
        required_reverify_tests=required_reverify_tests,
        rationale=rationale,
        fix_hash=fix_proposal_hash(
            fix_id,
            diagnosis_item.item_hash,
            diagnosis_item.blocker_code,
            affected_domains,
            tradeoff_scores,
            changes,
            required_reverify_tests,
            rationale,
        ),
    )


def fix_proposal_set_hash(
    project_id: str,
    diagnosis_hash: str,
    diagnosis_item_hash: str,
    blocker_code: str,
    proposals: tuple[FixProposal, ...],
) -> str:
    return canonical_sha256(
        FixProposalSetPayload(
            project_id=project_id,
            diagnosis_hash=diagnosis_hash,
            diagnosis_item_hash=diagnosis_item_hash,
            blocker_code=blocker_code,
            proposals=proposals,
            no_writeback=True,
            planning_only=True,
        )
    )


def build_fix_proposal_set(
    *,
    project_id: str,
    diagnosis: BlockerDiagnosis,
    diagnosis_item: DiagnosisItem,
    proposals: tuple[FixProposal, ...],
) -> FixProposalSet:
    if diagnosis_item not in diagnosis.items:
        raise ValueError("diagnosis item must belong to the blocker diagnosis")
    ordered = tuple(sorted(proposals, key=lambda item: item.fix_id))
    return FixProposalSet(
        project_id=project_id,
        diagnosis_hash=diagnosis.diagnosis_hash,
        diagnosis_item_hash=diagnosis_item.item_hash,
        blocker_code=diagnosis_item.blocker_code,
        proposals=ordered,
        fix_set_hash=fix_proposal_set_hash(
            project_id,
            diagnosis.diagnosis_hash,
            diagnosis_item.item_hash,
            diagnosis_item.blocker_code,
            ordered,
        ),
    )


def replan_seed_hash(
    project_id: str,
    source_decision_hash: str,
    diagnosis_hash: str,
    diagnosis_item_hash: str,
    blocker_code: str,
    selected_fix_id: str,
    selected_fix_hash: str,
    changes: tuple[ProposedChange, ...],
    required_reverify_tests: tuple[RequiredReverifyTest, ...],
) -> str:
    return canonical_sha256(
        ReplanSeedPayload(
            project_id=project_id,
            source_decision_hash=source_decision_hash,
            diagnosis_hash=diagnosis_hash,
            diagnosis_item_hash=diagnosis_item_hash,
            blocker_code=blocker_code,
            selected_fix_id=selected_fix_id,
            selected_fix_hash=selected_fix_hash,
            changes=changes,
            required_reverify_tests=required_reverify_tests,
        )
    )


def build_replan_seed(
    *,
    project_id: str,
    source_decision_hash: str,
    diagnosis_hash: str,
    diagnosis_item_hash: str,
    blocker_code: str,
    selected_fix: FixProposal,
) -> ReplanSeed:
    return ReplanSeed(
        project_id=project_id,
        source_decision_hash=source_decision_hash,
        diagnosis_hash=diagnosis_hash,
        diagnosis_item_hash=diagnosis_item_hash,
        blocker_code=blocker_code,
        selected_fix_id=selected_fix.fix_id,
        selected_fix_hash=selected_fix.fix_hash,
        changes=selected_fix.changes,
        required_reverify_tests=selected_fix.required_reverify_tests,
        seed_hash=replan_seed_hash(
            project_id,
            source_decision_hash,
            diagnosis_hash,
            diagnosis_item_hash,
            blocker_code,
            selected_fix.fix_id,
            selected_fix.fix_hash,
            selected_fix.changes,
            selected_fix.required_reverify_tests,
        ),
    )


def proposal_selection_hash(
    project_id: str,
    fix_set: FixProposalSet,
    fix_set_hash: str,
    selected_fix_id: str,
    selected_fix_hash: str,
    selected_fix: FixProposal,
    selected_by: str,
    selected_at: datetime,
    replan_seed: ReplanSeed,
    lineage: tuple[str, ...],
) -> str:
    return canonical_sha256(
        ProposalSelectionPayload(
            project_id=project_id,
            fix_set=fix_set,
            fix_set_hash=fix_set_hash,
            selected_fix_id=selected_fix_id,
            selected_fix_hash=selected_fix_hash,
            selected_fix=selected_fix,
            selected_by=selected_by,
            selected_at=selected_at,
            replan_seed=replan_seed,
            lineage=lineage,
            planning_only=True,
        )
    )


def select_fix_proposal(
    *,
    fix_set: FixProposalSet,
    selected_fix_id: str,
    source_decision_hash: str,
    selected_by: str,
    selected_at: datetime,
) -> ProposalSelection:
    selected = next(
        (item for item in fix_set.proposals if item.fix_id == selected_fix_id), None
    )
    if selected is None:
        raise ValueError("selected fix ID does not exist in fix set")
    seed = build_replan_seed(
        project_id=fix_set.project_id,
        source_decision_hash=source_decision_hash,
        diagnosis_hash=fix_set.diagnosis_hash,
        diagnosis_item_hash=fix_set.diagnosis_item_hash,
        blocker_code=fix_set.blocker_code,
        selected_fix=selected,
    )
    lineage = (
        source_decision_hash,
        fix_set.diagnosis_hash,
        fix_set.diagnosis_item_hash,
        fix_set.fix_set_hash,
        selected.fix_hash,
        seed.seed_hash,
    )
    return ProposalSelection(
        project_id=fix_set.project_id,
        fix_set=fix_set,
        fix_set_hash=fix_set.fix_set_hash,
        selected_fix_id=selected.fix_id,
        selected_fix_hash=selected.fix_hash,
        selected_fix=selected,
        selected_by=selected_by,
        selected_at=selected_at,
        replan_seed=seed,
        lineage=lineage,
        selection_hash=proposal_selection_hash(
            fix_set.project_id,
            fix_set,
            fix_set.fix_set_hash,
            selected.fix_id,
            selected.fix_hash,
            selected,
            selected_by,
            selected_at,
            seed,
            lineage,
        ),
    )


__all__ = [
    "BlockerDiagnosis",
    "DesignAlternative",
    "DesignProposalSet",
    "DesignStrategy",
    "DiagnosisConfidence",
    "DiagnosisHypothesis",
    "DiagnosisItem",
    "EngineeringGoal",
    "FixProposal",
    "FixProposalSet",
    "GoalPriority",
    "ProposalSelection",
    "ProposedChange",
    "ReplanSeed",
    "RequiredReverifyTest",
    "TradeoffScores",
    "build_blocker_diagnosis",
    "build_design_alternative",
    "build_design_proposal_set",
    "build_engineering_goal",
    "build_fix_proposal",
    "build_fix_proposal_set",
    "select_fix_proposal",
]
