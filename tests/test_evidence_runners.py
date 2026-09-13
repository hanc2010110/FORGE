from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime

from forge_core.evidence_runners import (
    CommandEvidenceRunner,
    EvidenceRunRequest,
    RegisteredEvidenceCommand,
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

    def test_interpreted_payload_is_detached_deeply_immutable_and_serializable(
        self,
    ) -> None:
        source: dict[str, object] = {"metrics": {"margin": 1.4}, "samples": [1, 2]}
        runner = CommandEvidenceRunner(
            commands=(
                RegisteredEvidenceCommand(
                    command_id="hil-arm",
                    adapter_id="hil-edge",
                    tier="hil",
                    argv=("forge-hil", "--case", "arm"),
                    output_kind="json",
                ),
            ),
            process_runner=lambda argv, timeout: (0, json.dumps(source).encode(), b""),
            clock=lambda: NOW,
        )
        result = runner.run(
            EvidenceRunRequest(
                run_id="run-immutable",
                command_id="hil-arm",
                project_id="robot-arm",
                candidate_hash="sha256:" + "1" * 64,
                scenario_hash="sha256:" + "2" * 64,
                requested_at=NOW,
            )
        )
        source["metrics"] = {"margin": 999}
        payload = result.interpreted_payload
        if payload is None:
            raise AssertionError("expected interpreted payload")
        metrics = payload["metrics"]
        self.assertIsInstance(metrics, Mapping)
        with self.assertRaises(TypeError):
            metrics["margin"] = 999  # type: ignore[index]
        self.assertEqual(
            result.model_dump(mode="json")["interpreted_payload"]["metrics"]["margin"],
            1.4,
        )

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


if __name__ == "__main__":
    unittest.main()
