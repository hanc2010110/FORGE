from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from forge_core.conversational_design import (
    ClaimConfidence,
    DesignCandidateSnapshot,
    DesignConversationState,
    DesignParameter,
    DesignStateTransition,
    EvidenceClaimKind,
    SimulationBinding,
    SimulationMetric,
    build_design_candidate,
    build_evidence_claim,
    build_simulation_binding,
)
from forge_core.conversational_persistence import (
    DesignCandidateApprovalRequest,
    StoredDesignCandidate,
    StoredDesignCandidateApproval,
    StoredDesignCandidateApprovalReceipt,
    StoredDesignStateTransition,
    StoredEvidenceClaim,
    StoredSimulationBinding,
    design_candidate_approval_hash,
    design_candidate_approval_receipt_hash,
)
from forge_core.hashing import canonical_sha256
from forge_core.models import SourceRef, Verdict

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
PROJECT = "robot-arm-project"
SESSION = "SESSION-014"


class ConversationalPersistenceModelTests(unittest.TestCase):
    def candidate(self) -> DesignCandidateSnapshot:
        return build_design_candidate(
            project_id=PROJECT,
            candidate_id="DC-014",
            revision=1,
            baseline_snapshot_hash=HASH_A,
            proposal_hash=HASH_B,
            parameters=(
                DesignParameter(
                    name="length",
                    current_value="420 mm",
                    proposed_value="520 mm",
                    source_refs=("cad:upper-arm-rev4",),
                ),
            ),
            requirements=("deflection <= 3.0 mm",),
            confirmed_by="engineer-1",
            confirmed_at=NOW,
        )

    def simulation(self) -> SimulationBinding:
        return build_simulation_binding(
            simulation_id="SIM-023",
            candidate=self.candidate(),
            tool_ref=SourceRef(
                kind="plugin",
                identifier="structural-solver",
                version="2026.09",
                hash=HASH_A,
            ),
            metrics=(
                SimulationMetric(
                    metric="maximum deflection",
                    actual="4.1 mm",
                    requirement="<= 3.0 mm",
                    verdict=Verdict.FAIL,
                ),
            ),
            created_at=NOW + timedelta(minutes=1),
        )

    def test_stored_candidate_binds_project_hash_and_utc_storage_time(self) -> None:
        candidate = self.candidate()
        record = StoredDesignCandidate(
            project_id=PROJECT,
            session_id=SESSION,
            candidate_hash=candidate.candidate_hash,
            candidate_id=candidate.candidate_id,
            revision=candidate.revision,
            proposal_hash=candidate.proposal_hash,
            candidate=candidate,
            stored_at=NOW + timedelta(minutes=2),
        )

        self.assertEqual(record.candidate, candidate)
        with self.assertRaises(ValidationError):
            StoredDesignCandidate.model_validate(
                record.model_dump(mode="python") | {"project_id": "other-project"}
            )
        with self.assertRaises(ValidationError):
            StoredDesignCandidate.model_validate(
                record.model_dump(mode="python") | {"stored_at": datetime(2026, 9, 1)}
            )

    def test_stored_candidate_rejects_tampered_payload(self) -> None:
        candidate = self.candidate()
        tampered = candidate.model_copy(update={"candidate_id": "DC-999"})

        with self.assertRaises(ValidationError):
            StoredDesignCandidate(
                project_id=PROJECT,
                session_id=SESSION,
                candidate_hash=candidate.candidate_hash,
                candidate_id=candidate.candidate_id,
                revision=candidate.revision,
                proposal_hash=candidate.proposal_hash,
                candidate=tampered,
                stored_at=NOW + timedelta(minutes=2),
            )

    def test_candidate_approval_and_receipt_bind_actor_request_and_candidate(
        self,
    ) -> None:
        candidate = self.candidate()
        request = DesignCandidateApprovalRequest(
            project_id=PROJECT,
            session_id=SESSION,
            candidate_id=candidate.candidate_id,
            revision=candidate.revision,
            proposal_hash=candidate.proposal_hash,
            parameters=candidate.parameters,
            requirements=candidate.requirements,
            confirmed_by=candidate.confirmed_by,
        )
        request_hash = canonical_sha256(request)
        nonce_hash = "sha256:" + "c" * 64
        approval_hash = design_candidate_approval_hash(
            approval_id="approval-1",
            project_id=PROJECT,
            request_hash=request_hash,
            nonce_hash=nonce_hash,
            approved_by=candidate.confirmed_by,
            approved_at=NOW,
        )
        approval = StoredDesignCandidateApproval(
            approval_id="approval-1",
            approval_hash=approval_hash,
            project_id=PROJECT,
            request_hash=request_hash,
            nonce_hash=nonce_hash,
            approved_by=candidate.confirmed_by,
            request=request,
            approved_at=NOW,
            stored_at=NOW,
        )
        receipt_hash = design_candidate_approval_receipt_hash(
            approval_id=approval.approval_id,
            approval_hash=approval.approval_hash,
            project_id=PROJECT,
            request_hash=request_hash,
            candidate_hash=candidate.candidate_hash,
            consumed_by=candidate.confirmed_by,
            consumed_at=NOW + timedelta(seconds=1),
        )
        receipt = StoredDesignCandidateApprovalReceipt(
            receipt_hash=receipt_hash,
            approval_id=approval.approval_id,
            approval_hash=approval.approval_hash,
            project_id=PROJECT,
            request_hash=request_hash,
            candidate_hash=candidate.candidate_hash,
            consumed_by=candidate.confirmed_by,
            consumed_at=NOW + timedelta(seconds=1),
        )

        self.assertEqual(approval.request, request)
        self.assertEqual(receipt.candidate_hash, candidate.candidate_hash)
        invalid_approval_updates = (
            {"project_id": "other-project"},
            {"approved_by": "other-engineer"},
            {"request_hash": HASH_A},
            {"approval_hash": HASH_A},
            {"stored_at": NOW - timedelta(seconds=1)},
            {"approved_at": datetime(2026, 9, 1)},
        )
        for update in invalid_approval_updates:
            with self.subTest(update=update), self.assertRaises(ValidationError):
                StoredDesignCandidateApproval.model_validate(
                    approval.model_dump(mode="python") | update
                )
        with self.assertRaises(ValidationError):
            StoredDesignCandidateApprovalReceipt.model_validate(
                receipt.model_dump(mode="python") | {"receipt_hash": HASH_A}
            )
        with self.assertRaises(ValidationError):
            StoredDesignCandidateApprovalReceipt.model_validate(
                receipt.model_dump(mode="python")
                | {"consumed_at": datetime(2026, 9, 1)}
            )

    def test_stored_simulation_binds_candidate_and_result_hashes(self) -> None:
        simulation = self.simulation()
        record = StoredSimulationBinding(
            project_id=PROJECT,
            session_id=SESSION,
            candidate_hash=simulation.candidate_hash,
            simulation_hash=simulation.simulation_hash,
            simulation_id=simulation.simulation_id,
            simulation=simulation,
            stored_at=NOW + timedelta(minutes=2),
        )

        self.assertEqual(record.simulation, simulation)
        with self.assertRaises(ValidationError):
            StoredSimulationBinding.model_validate(
                record.model_dump(mode="python") | {"candidate_hash": HASH_B}
            )
        with self.assertRaises(ValidationError):
            StoredSimulationBinding.model_validate(
                record.model_dump(mode="python") | {"stored_at": NOW}
            )

    def test_stored_evidence_claim_preserves_exact_claim_hash(self) -> None:
        claim = build_evidence_claim(
            claim_id="claim-1",
            kind=EvidenceClaimKind.INFERRED,
            statement="Existing J2 motor may stay in scope.",
            evidence_refs=("simulation:SIM-023",),
            confidence=ClaimConfidence.MEDIUM,
        )
        record = StoredEvidenceClaim(
            project_id=PROJECT,
            session_id=SESSION,
            claim_hash=claim.claim_hash,
            claim_id=claim.claim_id,
            claim=claim,
            stored_at=NOW,
        )

        self.assertEqual(record.claim, claim)
        with self.assertRaises(ValidationError):
            StoredEvidenceClaim.model_validate(
                record.model_dump(mode="python") | {"claim_hash": HASH_A}
            )

    def test_stored_transition_binds_session_sequence_hash_and_time(self) -> None:
        candidate = self.candidate()
        transition = DesignStateTransition(
            session_id=SESSION,
            sequence=3,
            from_state=DesignConversationState.SELECTED,
            to_state=DesignConversationState.CONFIRMED,
            candidate_hash=candidate.candidate_hash,
            occurred_at=NOW,
        )
        record = StoredDesignStateTransition(
            project_id=PROJECT,
            session_id=SESSION,
            sequence=3,
            from_state=transition.from_state,
            to_state=transition.to_state,
            candidate_hash=transition.candidate_hash,
            simulation_hash=transition.simulation_hash,
            transition=transition,
            stored_at=NOW + timedelta(seconds=1),
        )

        self.assertEqual(record.transition, transition)
        with self.assertRaises(ValidationError):
            StoredDesignStateTransition.model_validate(
                record.model_dump(mode="python") | {"sequence": 4}
            )
        with self.assertRaises(ValidationError):
            StoredDesignStateTransition.model_validate(
                record.model_dump(mode="python") | {"transition_hash": HASH_A}
            )


if __name__ == "__main__":
    unittest.main()
