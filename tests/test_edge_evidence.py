from __future__ import annotations

import unittest
from datetime import UTC, datetime

from forge_core.edge_evidence import (
    DevelopmentHMACVerifier,
    EdgeEvidenceEnvelope,
    EdgeEvidenceError,
    EdgeEvidenceIngestor,
    InMemoryReplayStore,
    create_development_signed_envelope,
)

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
CAPTURED_AT = datetime(2026, 9, 9, 7, 30, tzinfo=UTC)


class EdgeEvidenceTests(unittest.TestCase):
    def test_signed_edge_evidence_is_accepted_once_with_exact_lineage(self) -> None:
        verifier = DevelopmentHMACVerifier(secret="edge-secret")
        envelope = create_development_signed_envelope(
            project_id="forge-robot-arm",
            tenant_id="tenant-local",
            evidence_id="bench-run-001",
            device_id="upper-arm-rig",
            rig_id="bench-a",
            run_id="run-17",
            tier="bench",
            adapter="mqtt",
            source_uri="mqtt://bench.local/forge/evidence",
            captured_at=CAPTURED_AT,
            sequence=1,
            nonce="nonce-001",
            candidate_hash=HASH_A,
            firmware_hash=HASH_B,
            artifact_hash=HASH_C,
            payload={"temperature_c": 38.2, "result": "PASS"},
            verifier=verifier,
        )

        accepted = EdgeEvidenceIngestor(
            verifier=verifier,
            replay_store=InMemoryReplayStore(),
            now=lambda: CAPTURED_AT,
        ).ingest(envelope)

        self.assertEqual(accepted.envelope.tier, "bench")
        self.assertEqual(accepted.envelope.candidate_hash, HASH_A)
        self.assertEqual(accepted.envelope.firmware_hash, HASH_B)
        self.assertEqual(accepted.envelope.artifact_hash, HASH_C)
        self.assertEqual(
            accepted.envelope.source_uri, "mqtt://bench.local/forge/evidence"
        )
        self.assertRegex(accepted.acceptance_hash, r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn("edge-secret", repr(verifier))

    def test_replay_duplicate_nonce_and_sequence_rollback_are_rejected(self) -> None:
        verifier = DevelopmentHMACVerifier(secret="edge-secret")
        store = InMemoryReplayStore()
        ingestor = EdgeEvidenceIngestor(
            verifier=verifier, replay_store=store, now=lambda: CAPTURED_AT
        )
        first = _signed(sequence=10, nonce="unique-10", verifier=verifier)
        duplicate_nonce = _signed(sequence=11, nonce="unique-10", verifier=verifier)
        rollback = _signed(sequence=9, nonce="unique-09", verifier=verifier)

        ingestor.ingest(first)
        with self.assertRaisesRegex(EdgeEvidenceError, "edge_evidence_replay"):
            ingestor.ingest(duplicate_nonce)
        with self.assertRaisesRegex(
            EdgeEvidenceError, "edge_evidence_sequence_rollback"
        ):
            ingestor.ingest(rollback)

    def test_tampering_and_device_control_payloads_fail_closed(self) -> None:
        verifier = DevelopmentHMACVerifier(secret="edge-secret")
        envelope = _signed(sequence=1, nonce="nonce-1", verifier=verifier)
        tampered = envelope.model_copy(update={"payload": {"measured_voltage_v": 48.0}})
        with self.assertRaisesRegex(EdgeEvidenceError, "edge_evidence_bad_signature"):
            EdgeEvidenceIngestor(
                verifier=verifier,
                replay_store=InMemoryReplayStore(),
                now=lambda: CAPTURED_AT,
            ).ingest(tampered)

        with self.assertRaises(ValueError):
            create_development_signed_envelope(
                project_id="forge-robot-arm",
                tenant_id="tenant-local",
                evidence_id="hil-run-002",
                device_id="upper-arm-rig",
                rig_id="hil-a",
                run_id="run-18",
                tier="hil",
                adapter="ros2",
                source_uri="ros2://hil.local/forge/evidence",
                captured_at=CAPTURED_AT,
                sequence=2,
                nonce="nonce-2",
                candidate_hash=HASH_A,
                firmware_hash=HASH_B,
                artifact_hash=HASH_C,
                payload={"raw_device_command": "SET_SPEED 100"},
                verifier=verifier,
            )

        with self.assertRaises(ValueError):
            create_development_signed_envelope(
                project_id="forge-robot-arm",
                tenant_id="tenant-local",
                evidence_id="hil-run-003",
                device_id="upper-arm-rig",
                rig_id="hil-a",
                run_id="run-19",
                tier="hil",
                adapter="ros2",
                source_uri="ros2://hil.local/forge/evidence",
                captured_at=CAPTURED_AT,
                sequence=3,
                nonce="nonce-3",
                candidate_hash=HASH_A,
                firmware_hash=HASH_B,
                artifact_hash=HASH_C,
                payload={"measurement": {"command": "SET_SPEED 100"}},
                verifier=verifier,
            )

    def test_envelope_requires_utc_and_canonical_hashes(self) -> None:
        verifier = DevelopmentHMACVerifier(secret="edge-secret")
        with self.assertRaises(ValueError):
            create_development_signed_envelope(
                project_id="forge-robot-arm",
                tenant_id="tenant-local",
                evidence_id="physical-run-003",
                device_id="upper-arm-rig",
                rig_id="field-a",
                run_id="run-19",
                tier="physical_device",
                adapter="opcua",
                source_uri="opc.tcp://robot.local/forge/evidence",
                captured_at=datetime(2026, 9, 9, 7, 30),
                sequence=3,
                nonce="nonce-3",
                candidate_hash=HASH_A,
                firmware_hash=HASH_B,
                artifact_hash=HASH_C,
                payload={"result": "PASS"},
                verifier=verifier,
            )

        with self.assertRaises(ValueError):
            EdgeEvidenceEnvelope(
                schema_version="1.0.0",
                project_id="forge-robot-arm",
                tenant_id="tenant-local",
                evidence_id="bad-hash",
                device_id="upper-arm-rig",
                rig_id="bench-a",
                run_id="run-17",
                tier="bench",
                adapter="mqtt",
                source_uri="mqtt://bench.local/forge/evidence",
                captured_at=CAPTURED_AT,
                sequence=1,
                nonce="nonce-001",
                candidate_hash=HASH_A,
                firmware_hash=HASH_B,
                artifact_hash=HASH_C,
                payload={"result": "PASS"},
                signature_algorithm="hmac-sha256-development-only",
                signature_key_id="dev-key",
                signature="hmac-sha256:" + "0" * 64,
                envelope_hash="sha256:" + "0" * 64,
            )


def _signed(
    *,
    sequence: int,
    nonce: str,
    verifier: DevelopmentHMACVerifier,
) -> EdgeEvidenceEnvelope:
    return create_development_signed_envelope(
        project_id="forge-robot-arm",
        tenant_id="tenant-local",
        evidence_id=f"bench-run-{sequence}",
        device_id="upper-arm-rig",
        rig_id="bench-a",
        run_id="run-17",
        tier="bench",
        adapter="mqtt",
        source_uri="mqtt://bench.local/forge/evidence",
        captured_at=CAPTURED_AT,
        sequence=sequence,
        nonce=nonce,
        candidate_hash=HASH_A,
        firmware_hash=HASH_B,
        artifact_hash=HASH_C,
        payload={"measured_voltage_v": 47.9},
        verifier=verifier,
    )


if __name__ == "__main__":
    unittest.main()
