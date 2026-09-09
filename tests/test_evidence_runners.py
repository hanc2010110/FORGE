from __future__ import annotations

import unittest
from datetime import UTC, datetime

from forge_core.evidence_runners import (
    CommandEvidenceRunner,
    EdgeEvidenceVerifier,
    EvidenceRunRequest,
    RegisteredEvidenceCommand,
    sign_edge_evidence_hmac,
)

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)


class EvidenceRunnerTests(unittest.TestCase):
    def test_allowlisted_runner_captures_tier_bound_evidence(self) -> None:
        runner = CommandEvidenceRunner(
            commands=(
                RegisteredEvidenceCommand(
                    command_id="sim-arm",
                    adapter_id="local-sim",
                    tier="simulation",
                    argv=("forge-sim", "--case", "arm"),
                    output_kind="json",
                ),
            ),
            process_runner=lambda argv, timeout: (0, b'{"margin":1.4}', b""),
            clock=lambda: NOW,
        )
        result = runner.run(
            EvidenceRunRequest(
                run_id="run-1",
                command_id="sim-arm",
                project_id="robot-arm",
                candidate_hash="sha256:" + "1" * 64,
                scenario_hash="sha256:" + "2" * 64,
                requested_at=NOW,
            )
        )
        self.assertEqual(result.tier, "simulation")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout_sha256[:7], "sha256:")
        self.assertEqual(result.interpreted_payload, {"margin": 1.4})

    def test_runner_rejects_unknown_command_and_tier_mismatch(self) -> None:
        runner = CommandEvidenceRunner(
            commands=(
                RegisteredEvidenceCommand(
                    command_id="bench-1",
                    adapter_id="bench-edge",
                    tier="bench",
                    argv=("bench",),
                    output_kind="text",
                ),
            ),
            process_runner=lambda argv, timeout: (0, b"ok", b""),
            clock=lambda: NOW,
        )
        with self.assertRaisesRegex(KeyError, "unknown evidence command"):
            runner.run(
                EvidenceRunRequest(
                    run_id="run-1",
                    command_id="missing",
                    project_id="robot-arm",
                    candidate_hash="sha256:" + "1" * 64,
                    scenario_hash="sha256:" + "2" * 64,
                    requested_at=NOW,
                )
            )
        with self.assertRaises(ValueError):
            RegisteredEvidenceCommand(
                command_id="unsafe",
                adapter_id="bench-edge",
                tier="physical_device",
                argv=("python", "-c", "print(1)"),
                output_kind="text",
            )

    def test_signed_edge_evidence_accepts_distinct_lab_tiers_without_device_control(
        self,
    ) -> None:
        payload = {"max_current_a": 3.2, "status": "pass"}
        envelope = sign_edge_evidence_hmac(
            device_id="bench-01",
            tier="bench",
            sequence=1,
            candidate_hash="sha256:" + "1" * 64,
            scenario_hash="sha256:" + "2" * 64,
            payload=payload,
            captured_at=NOW,
            shared_secret=b"dev-secret",
        )

        verifier = EdgeEvidenceVerifier(
            verifier=lambda message, signature: (
                sign_edge_evidence_hmac(
                    device_id="bench-01",
                    tier="bench",
                    sequence=1,
                    candidate_hash="sha256:" + "1" * 64,
                    scenario_hash="sha256:" + "2" * 64,
                    payload=payload,
                    captured_at=NOW,
                    shared_secret=b"dev-secret",
                ).signature
                == signature
            )
        )
        accepted = verifier.verify(envelope)

        self.assertEqual(accepted.tier, "bench")
        self.assertEqual(accepted.payload_hash[:7], "sha256:")
        self.assertFalse(accepted.allows_device_control)
        with self.assertRaisesRegex(ValueError, "replay"):
            verifier.verify(envelope)

    def test_edge_evidence_rejects_unsigned_or_out_of_order_payloads(self) -> None:
        envelope = sign_edge_evidence_hmac(
            device_id="hil-01",
            tier="hil",
            sequence=2,
            candidate_hash="sha256:" + "1" * 64,
            scenario_hash="sha256:" + "2" * 64,
            payload={"status": "pass"},
            captured_at=NOW,
            shared_secret=b"dev-secret",
        ).model_copy(update={"signature": "sha256:" + "3" * 64})
        verifier = EdgeEvidenceVerifier(verifier=lambda _message, _signature: False)
        with self.assertRaisesRegex(ValueError, "signature"):
            verifier.verify(envelope)


if __name__ == "__main__":
    unittest.main()
