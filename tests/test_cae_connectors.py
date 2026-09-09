from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from typing import Literal

from forge_core.cae_connectors import (
    CAEApprovalRef,
    CAEConnectorError,
    CAESubmitRequest,
    MATLABProductionServerClient,
    SimScaleCAEClient,
    ansys_connector_status,
)

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
NOW = datetime(2026, 9, 9, 7, 30, tzinfo=UTC)


class FakeCAETransport:
    def __init__(self, responses: list[object], *, status: int = 200) -> None:
        self.responses = responses
        self.status = status
        self.requests: list[dict[str, object]] = []

    def request(
        self,
        method: Literal["GET", "POST"],
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> tuple[int, dict[str, str], bytes]:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "body": body,
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
            }
        )
        response = self.responses.pop(0)
        return (
            self.status,
            {"content-type": "application/json"},
            json.dumps(response).encode(),
        )


class CAEConnectorTests(unittest.TestCase):
    def test_simscale_submit_status_and_result_are_evidence_bound(self) -> None:
        transport = FakeCAETransport(
            [
                {"runId": "run-001", "state": "STARTED"},
                {"state": "RUNNING"},
                {"state": "FINISHED"},
                {"max_stress_mpa": 42.5},
            ]
        )
        client = SimScaleCAEClient(
            base_url="https://api.simscale.com",
            api_key="simscale-secret",
            transport=transport,
            clock=lambda: NOW,
        )
        approval = CAEApprovalRef(
            approval_id="approval-1",
            project_id="project-1",
            candidate_hash=HASH_A,
            approved_by="local-operator",
            approved_at=NOW,
        )
        submit = CAESubmitRequest(
            project_id="project-1",
            simulation_id="sim-1",
            candidate_hash=HASH_A,
            scenario_hash=HASH_B,
            idempotency_key="idem-1234",
            approval=approval,
            payload={"load_n": 120, "mesh": "coarse"},
        )

        run = client.submit_run(submit)
        started = client.start_run(
            run,
            approval=approval,
            idempotency_key="start-1234",
        )
        status = client.get_status(run)
        result = client.get_result(run)

        self.assertEqual(run.run_id, "run-001")
        self.assertEqual(run.evidence.operation, "submit")
        self.assertEqual(started.operation, "start")
        self.assertEqual(status.payload["state"], "FINISHED")
        self.assertEqual(result.payload["max_stress_mpa"], 42.5)
        self.assertEqual(
            transport.requests[0]["url"],
            "https://api.simscale.com/v1/projects/project-1/simulations/sim-1/runs",
        )
        self.assertEqual(transport.requests[0]["method"], "POST")
        body = transport.requests[0]["body"]
        if not isinstance(body, bytes):
            raise AssertionError("expected SimScale submit request body bytes")
        self.assertIn("candidate_hash", body.decode())
        self.assertEqual(
            transport.requests[1]["url"],
            "https://api.simscale.com/v1/projects/project-1/simulations/sim-1/runs/run-001/start",
        )
        self.assertNotIn("simscale-secret", repr(transport.requests))

    def test_simscale_submit_requires_matching_user_approval(self) -> None:
        approval = CAEApprovalRef(
            approval_id="approval-1",
            project_id="project-1",
            candidate_hash=HASH_B,
            approved_by="local-operator",
            approved_at=NOW,
        )
        with self.assertRaises(ValueError):
            CAESubmitRequest(
                project_id="project-1",
                simulation_id="sim-1",
                candidate_hash=HASH_A,
                scenario_hash=HASH_B,
                idempotency_key="idem-1234",
                approval=approval,
                payload={"load_n": 120},
            )

    def test_matlab_adapter_executes_only_allowlisted_functions(self) -> None:
        transport = FakeCAETransport([{"margin": 1.8, "unit": "ratio"}])
        client = MATLABProductionServerClient(
            base_url="https://matlab.example.com",
            bearer_token="matlab-secret",
            application="forge",
            allowlisted_functions=("upper_arm_margin",),
            transport=transport,
            clock=lambda: NOW,
        )
        result = client.execute_allowlisted_function(
            function_name="upper_arm_margin",
            project_id="project-1",
            simulation_id="sim-1",
            run_id="local-run-1",
            payload={"candidate_hash": HASH_A},
        )
        self.assertEqual(result.provider, "matlab-production-server")
        self.assertEqual(result.operation, "execute")
        self.assertEqual(result.payload["margin"], 1.8)
        self.assertEqual(
            transport.requests[0]["url"],
            "https://matlab.example.com/forge/upper_arm_margin",
        )
        self.assertNotIn("matlab-secret", repr(transport.requests))

        with self.assertRaises(ValueError):
            client.execute_allowlisted_function(
                function_name="eval",
                project_id="project-1",
                simulation_id="sim-1",
                run_id="local-run-1",
                payload={},
            )

    def test_connectors_fail_closed_on_invalid_boundaries_and_errors(self) -> None:
        with self.assertRaises(ValueError):
            SimScaleCAEClient(
                base_url="http://api.simscale.com",
                api_key="secret",
                transport=FakeCAETransport([{}]),
                clock=lambda: NOW,
            )
        client = SimScaleCAEClient(
            base_url="https://api.simscale.com",
            api_key="secret",
            transport=FakeCAETransport([{"error": "bad"}], status=403),
            clock=lambda: NOW,
        )
        approval = CAEApprovalRef(
            approval_id="approval-1",
            project_id="project-1",
            candidate_hash=HASH_A,
            approved_by="local-operator",
            approved_at=NOW,
        )
        request = CAESubmitRequest(
            project_id="project-1",
            simulation_id="sim-1",
            candidate_hash=HASH_A,
            scenario_hash=HASH_B,
            idempotency_key="idem-1234",
            approval=approval,
            payload={},
        )
        with self.assertRaisesRegex(CAEConnectorError, "cae_unauthorized"):
            client.submit_run(request)

    def test_ansys_is_declared_adapter_required_not_fake_connected(self) -> None:
        status = ansys_connector_status()
        self.assertEqual(status.provider, "ansys")
        self.assertEqual(status.status, "adapter_required")
        self.assertIn("version", status.required_inputs)


if __name__ == "__main__":
    unittest.main()
