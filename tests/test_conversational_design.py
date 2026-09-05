from __future__ import annotations

import unittest
from datetime import UTC, datetime

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
from forge_core.models import SourceRef, Verdict

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


class ConversationalDesignTests(unittest.TestCase):
    def candidate(self) -> DesignCandidateSnapshot:
        return build_design_candidate(
            project_id="robot-arm-project",
            candidate_id="DC-014",
            revision=1,
            baseline_snapshot_hash=HASH_A,
            proposal_hash=HASH_B,
            parameters=(
                DesignParameter(
                    name="width",
                    current_value="42 mm",
                    proposed_value="42 mm",
                    source_refs=("cad:upper-arm-rev4",),
                ),
                DesignParameter(
                    name="length",
                    current_value="420 mm",
                    proposed_value="520 mm",
                    source_refs=("cad:upper-arm-rev4",),
                ),
            ),
            requirements=("deflection <= 3.0 mm", "mass <= 1.89 kg"),
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
                    metric="maximum stress",
                    actual="173 MPa",
                    requirement="<= 210 MPa",
                    verdict=Verdict.PASS,
                ),
                SimulationMetric(
                    metric="maximum deflection",
                    actual="4.1 mm",
                    requirement="<= 3.0 mm",
                    verdict=Verdict.FAIL,
                ),
            ),
            created_at=NOW,
        )

    def test_inferred_claim_requires_confidence_and_exact_hash(self) -> None:
        with self.assertRaises(ValidationError):
            build_evidence_claim(
                claim_id="claim-1",
                kind=EvidenceClaimKind.INFERRED,
                statement="Existing motor is probably sufficient.",
                evidence_refs=("simulation:SIM-023",),
            )
        claim = build_evidence_claim(
            claim_id="claim-1",
            kind=EvidenceClaimKind.INFERRED,
            statement="Existing motor is probably sufficient.",
            evidence_refs=("simulation:SIM-023",),
            confidence=ClaimConfidence.HIGH,
        )
        with self.assertRaises(ValidationError):
            type(claim).model_validate(
                claim.model_dump(mode="python")
                | {"statement": "Motor replacement is required."}
            )

    def test_candidate_is_canonical_frozen_and_hash_bound(self) -> None:
        candidate = self.candidate()
        self.assertEqual(
            tuple(item.name for item in candidate.parameters), ("length", "width")
        )
        with self.assertRaises(ValidationError):
            type(candidate).model_validate(
                candidate.model_dump(mode="python")
                | {"requirements": ("mass <= 1.90 kg",)}
            )
        with self.assertRaises(ValidationError):
            candidate.revision = 2  # type: ignore[misc]

    def test_simulation_binds_exact_candidate_hash(self) -> None:
        simulation = self.simulation()
        self.assertEqual(simulation.candidate_hash, self.candidate().candidate_hash)
        with self.assertRaises(ValidationError):
            SimulationBinding.model_validate(
                simulation.model_dump(mode="python") | {"candidate_hash": HASH_B}
            )

    def test_unconfirmed_selection_cannot_jump_to_simulation(self) -> None:
        with self.assertRaises(ValidationError):
            DesignStateTransition(
                session_id="SESSION-014",
                sequence=3,
                from_state=DesignConversationState.SELECTED,
                to_state=DesignConversationState.SIMULATED,
                candidate_hash=self.candidate().candidate_hash,
                simulation_hash=self.simulation().simulation_hash,
                occurred_at=NOW,
            )

    def test_confirmation_and_simulation_require_exact_bindings(self) -> None:
        candidate = self.candidate()
        simulation = self.simulation()
        confirmed = DesignStateTransition(
            session_id="SESSION-014",
            sequence=3,
            from_state=DesignConversationState.SELECTED,
            to_state=DesignConversationState.CONFIRMED,
            candidate_hash=candidate.candidate_hash,
            occurred_at=NOW,
        )
        simulated = DesignStateTransition(
            session_id="SESSION-014",
            sequence=4,
            from_state=DesignConversationState.CONFIRMED,
            to_state=DesignConversationState.SIMULATED,
            candidate_hash=candidate.candidate_hash,
            simulation_hash=simulation.simulation_hash,
            occurred_at=NOW,
        )
        self.assertEqual(confirmed.to_state, DesignConversationState.CONFIRMED)
        self.assertEqual(simulated.to_state, DesignConversationState.SIMULATED)

    def test_acceptance_is_not_verification(self) -> None:
        simulation = self.simulation()
        accepted = DesignStateTransition(
            session_id="SESSION-014",
            sequence=5,
            from_state=DesignConversationState.SIMULATED,
            to_state=DesignConversationState.ACCEPTED,
            simulation_hash=simulation.simulation_hash,
            occurred_at=NOW,
        )
        self.assertEqual(accepted.to_state, DesignConversationState.ACCEPTED)
        with self.assertRaises(ValidationError):
            DesignStateTransition(
                session_id="SESSION-014",
                sequence=6,
                from_state=DesignConversationState.ACCEPTED,
                to_state=DesignConversationState.VERIFIED,
                occurred_at=NOW,
            )

    def test_verified_transition_requires_imported_evidence(self) -> None:
        verified = DesignStateTransition(
            session_id="SESSION-014",
            sequence=6,
            from_state=DesignConversationState.ACCEPTED,
            to_state=DesignConversationState.VERIFIED,
            evidence_refs=("bench:duty-cycle-042",),
            occurred_at=NOW,
        )
        self.assertEqual(verified.evidence_refs, ("bench:duty-cycle-042",))


if __name__ == "__main__":
    unittest.main()
