from __future__ import annotations

import unittest
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from forge_core.iteration_workflow import (
    ApprovedIterationOrchestrator,
    CandidateSimulationApproval,
    DeterministicReleaseOutcome,
    InMemoryIterationCheckpointStore,
    IterationDecision,
    IterationEvidenceState,
    IterationTransitionError,
    LLMIterationRecommendation,
    SimulationExecutionDirective,
    SimulationRequest,
    authorize_simulation_request,
    decide_next_iteration,
    simulation_request_hash,
)

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)


class IterationWorkflowTests(unittest.TestCase):
    def test_authorizes_only_exact_user_approved_candidate_and_simulation_request(
        self,
    ) -> None:
        request = SimulationRequest(
            project_id="robot-arm",
            candidate_hash="sha256:" + "1" * 64,
            request_id="sim-014",
            requested_by="operator",
            requested_at=NOW,
            scenario_hash="sha256:" + "2" * 64,
            expected_result_kinds=("deflection-mm", "motor-current-a"),
            idempotency_key="idem-014",
        )
        approval = CandidateSimulationApproval(
            approval_id="approval-014",
            project_id="robot-arm",
            candidate_hash=request.candidate_hash,
            simulation_request_hash=simulation_request_hash(request),
            approved_by="operator",
            approved_at=NOW,
        )

        directive = authorize_simulation_request(request, approval)

        self.assertIsInstance(directive, SimulationExecutionDirective)
        self.assertEqual(directive.candidate_hash, request.candidate_hash)
        self.assertEqual(directive.execution_authority, "user_approved_candidate_only")
        self.assertEqual(directive.release_authority, "forge_policy_only")

    def test_rejects_stale_or_mismatched_simulation_approval(self) -> None:
        request = SimulationRequest(
            project_id="robot-arm",
            candidate_hash="sha256:" + "1" * 64,
            request_id="sim-014",
            requested_by="operator",
            requested_at=NOW,
            scenario_hash="sha256:" + "2" * 64,
            expected_result_kinds=("deflection-mm",),
            idempotency_key="idem-014",
        )
        approval = CandidateSimulationApproval(
            approval_id="approval-014",
            project_id="robot-arm",
            candidate_hash="sha256:" + "3" * 64,
            simulation_request_hash=simulation_request_hash(request),
            approved_by="operator",
            approved_at=NOW,
        )

        with self.assertRaises(IterationTransitionError):
            authorize_simulation_request(request, approval)

    def test_llm_recommendation_is_non_authoritative(self) -> None:
        recommendation = LLMIterationRecommendation(
            project_id="robot-arm",
            candidate_hash="sha256:" + "1" * 64,
            recommendation_id="rec-014",
            summary="Longer arm may require new motor current evidence.",
            cited_evidence_hashes=("sha256:" + "2" * 64,),
            created_at=NOW,
        )

        self.assertFalse(recommendation.can_execute_simulation)
        self.assertFalse(recommendation.can_decide_release)

        with self.assertRaises(ValidationError):
            unsafe_payload: dict[str, Any] = {
                "project_id": "robot-arm",
                "candidate_hash": "sha256:" + "1" * 64,
                "recommendation_id": "rec-015",
                "summary": "READY",
                "cited_evidence_hashes": ("sha256:" + "2" * 64,),
                "created_at": NOW,
                "can_decide_release": True,
            }
            LLMIterationRecommendation(**unsafe_payload)

    def test_failed_evidence_routes_back_to_revise(self) -> None:
        state = IterationEvidenceState(
            project_id="robot-arm",
            candidate_hash="sha256:" + "1" * 64,
            latest_simulation_hash="sha256:" + "2" * 64,
            missing_required_tiers=(),
            failing_required_tiers=("simulation",),
            user_confirmed_release=False,
            observed_at=NOW,
        )
        decision = decide_next_iteration(state)
        self.assertEqual(decision.decision, IterationDecision.REVISE_AND_RESIMULATE)
        self.assertIn("simulation", decision.required_actions[0])

    def test_complete_evidence_waits_for_release_confirmation(self) -> None:
        state = IterationEvidenceState(
            project_id="robot-arm",
            candidate_hash="sha256:" + "1" * 64,
            latest_simulation_hash="sha256:" + "2" * 64,
            missing_required_tiers=(),
            failing_required_tiers=(),
            user_confirmed_release=False,
            observed_at=NOW,
        )
        self.assertEqual(
            decide_next_iteration(state).decision,
            IterationDecision.INTERPRET_WITH_USER,
        )
        confirmed = state.model_copy(update={"user_confirmed_release": True})
        self.assertEqual(
            decide_next_iteration(confirmed).decision,
            IterationDecision.VERIFY_RELEASE,
        )

    def test_already_evaluated_evidence_waits_for_new_evidence(self) -> None:
        state = IterationEvidenceState(
            project_id="robot-arm",
            candidate_hash="sha256:" + "1" * 64,
            latest_simulation_hash="sha256:" + "2" * 64,
            latest_evidence_hash="sha256:" + "3" * 64,
            last_evaluated_evidence_hash="sha256:" + "3" * 64,
            user_confirmed_release=False,
            observed_at=NOW,
        )

        decision = decide_next_iteration(state)

        self.assertEqual(decision.decision, IterationDecision.WAIT_FOR_NEW_EVIDENCE)
        self.assertIn("new evidence", decision.required_actions[0])

    def test_new_evidence_automatically_reverifies_once_after_user_confirmation(
        self,
    ) -> None:
        calls: list[tuple[str, str, str]] = []

        def verify_release(
            project_id: str, candidate_hash: str, evidence_hash: str
        ) -> DeterministicReleaseOutcome:
            calls.append((project_id, candidate_hash, evidence_hash))
            return DeterministicReleaseOutcome(
                status="READY",
                decision_hash="sha256:" + str(len(calls)) * 64,
            )

        orchestrator = ApprovedIterationOrchestrator(
            checkpoint_store=InMemoryIterationCheckpointStore(),
            verify_release=verify_release,
        )
        first = IterationEvidenceState(
            project_id="robot-arm",
            candidate_hash="sha256:" + "1" * 64,
            latest_simulation_hash="sha256:" + "2" * 64,
            latest_evidence_hash="sha256:" + "3" * 64,
            user_confirmed_release=True,
            observed_at=NOW,
        )

        first_result = orchestrator.on_evidence_recorded(first)
        replay_result = orchestrator.on_evidence_recorded(first)
        changed_result = orchestrator.on_evidence_recorded(
            first.model_copy(update={"latest_evidence_hash": "sha256:" + "4" * 64})
        )

        self.assertTrue(first_result.verification_invoked)
        self.assertFalse(replay_result.verification_invoked)
        self.assertEqual(
            replay_result.next_step.decision,
            IterationDecision.WAIT_FOR_NEW_EVIDENCE,
        )
        self.assertTrue(changed_result.verification_invoked)
        self.assertEqual(len(calls), 2)

        replay_old_result = orchestrator.on_evidence_recorded(first)
        self.assertFalse(replay_old_result.verification_invoked)
        self.assertEqual(len(calls), 2)

    def test_blocked_reverification_routes_to_revision_not_acceptance(self) -> None:
        orchestrator = ApprovedIterationOrchestrator(
            checkpoint_store=InMemoryIterationCheckpointStore(),
            verify_release=lambda _project, _candidate, _evidence: (
                DeterministicReleaseOutcome(
                    status="BLOCKED", decision_hash="sha256:" + "8" * 64
                )
            ),
        )
        state = IterationEvidenceState(
            project_id="robot-arm",
            candidate_hash="sha256:" + "1" * 64,
            latest_evidence_hash="sha256:" + "3" * 64,
            user_confirmed_release=True,
            observed_at=NOW,
        )

        result = orchestrator.on_evidence_recorded(state)

        self.assertTrue(result.verification_invoked)
        self.assertEqual(result.release_status, "BLOCKED")
        self.assertEqual(
            result.next_step.decision, IterationDecision.REVISE_AND_RESIMULATE
        )
        self.assertEqual(result.next_step.next_state.value, "revised")

    def test_automatic_reverification_never_runs_without_approval_or_on_failure(
        self,
    ) -> None:
        calls: list[str] = []

        def verify_release(
            project_id: str, candidate_hash: str, evidence_hash: str
        ) -> DeterministicReleaseOutcome:
            calls.append(evidence_hash)
            return DeterministicReleaseOutcome(
                status="READY", decision_hash="sha256:" + "9" * 64
            )

        orchestrator = ApprovedIterationOrchestrator(
            checkpoint_store=InMemoryIterationCheckpointStore(),
            verify_release=verify_release,
        )
        unconfirmed = IterationEvidenceState(
            project_id="robot-arm",
            candidate_hash="sha256:" + "1" * 64,
            latest_simulation_hash="sha256:" + "2" * 64,
            latest_evidence_hash="sha256:" + "3" * 64,
            user_confirmed_release=False,
            observed_at=NOW,
        )
        failed = unconfirmed.model_copy(
            update={
                "user_confirmed_release": True,
                "failing_required_tiers": ("simulation",),
            }
        )

        self.assertFalse(
            orchestrator.on_evidence_recorded(unconfirmed).verification_invoked
        )
        self.assertFalse(orchestrator.on_evidence_recorded(failed).verification_invoked)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
