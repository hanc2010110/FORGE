from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from forge_core.cae_connectors import (
    CAEApprovalRef,
    CAEConnectorError,
    CAESubmitRequest,
    InMemoryCAEApprovalStore,
    MATLABProductionServerClient,
    SimScaleCAEClient,
    SQLiteCAEApprovalStore,
    ansys_connector_status,
    cae_execution_request_hash,
)

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
NOW = datetime(2026, 9, 9, 7, 30, tzinfo=UTC)


def _approval(
    *,
    provider: Literal["simscale", "matlab-production-server"] = "simscale",
    operation: Literal["submit_and_start", "execute"] = "submit_and_start",
    candidate_hash: str = HASH_A,
    scenario_hash: str = HASH_B,
    payload: Mapping[str, object] | None = None,
    target: str = "sim-1",
    execution_key: str = "idem-1234",
) -> CAEApprovalRef:
    request_payload = payload or {}
    return CAEApprovalRef(
        approval_id="approval-1",
        provider=provider,
        operation=operation,
        project_id="project-1",
        simulation_id="sim-1",
        candidate_hash=candidate_hash,
        scenario_hash=scenario_hash,
        execution_key=execution_key,
        request_hash=cae_execution_request_hash(
            provider=provider,
            operation=operation,
            project_id="project-1",
            simulation_id="sim-1",
            candidate_hash=candidate_hash,
            scenario_hash=scenario_hash,
            execution_key=execution_key,
            target=target,
            payload=request_payload,
        ),
        approved_by="local-operator",
        approved_at=NOW,
    )


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


class RawCAETransport:
    def __init__(self, raw: bytes, *, status: int = 200) -> None:
        self.raw = raw
        self.status = status

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
        return self.status, {"content-type": "application/json"}, self.raw


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
        request_payload = {"load_n": 120, "mesh": "coarse"}
        approval = _approval(payload=request_payload)
        approvals = InMemoryCAEApprovalStore()
        approvals.register(approval)
        client = SimScaleCAEClient(
            base_url="https://api.simscale.com",
            api_key="simscale-secret",
            approval_verifier=approvals,
            transport=transport,
            clock=lambda: NOW,
        )
        submit = CAESubmitRequest(
            project_id="project-1",
            simulation_id="sim-1",
            candidate_hash=HASH_A,
            scenario_hash=HASH_B,
            idempotency_key="idem-1234",
            approval=approval,
            payload=request_payload,
        )

        run = client.submit_run(submit)
        started = client.start_run(
            run,
            approval=approval,
            idempotency_key="idem-1234",
        )
        replayed_start = client.start_run(
            run,
            approval=approval,
            idempotency_key="idem-1234",
        )
        self.assertEqual(replayed_start, started)
        self.assertEqual(len(transport.requests), 2)
        status = client.get_status(run)
        result = client.get_result(run)

        self.assertEqual(run.run_id, "run-001")
        self.assertEqual(run.evidence.operation, "submit")
        self.assertEqual(started.operation, "start")
        self.assertEqual(status.payload["state"], "FINISHED")
        self.assertEqual(result.payload["max_stress_mpa"], 42.5)
        self.assertEqual(result.candidate_hash, HASH_A)
        self.assertEqual(result.scenario_hash, HASH_B)
        self.assertEqual(result.approval_request_hash, approval.request_hash)
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
        approval = _approval(candidate_hash=HASH_B, payload={"load_n": 120})
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

        exact = _approval(payload={"load_n": 120})
        with self.assertRaisesRegex(ValueError, "exact request hash"):
            CAESubmitRequest(
                project_id="project-1",
                simulation_id="sim-1",
                candidate_hash=HASH_A,
                scenario_hash=HASH_B,
                idempotency_key="idem-1234",
                approval=exact,
                payload={"load_n": 121},
            )

    def test_nested_cae_payload_is_immutable_and_rechecked_before_transport(
        self,
    ) -> None:
        payload: dict[str, object] = {"mesh": {"size": 1}, "loads": [1, 2]}
        approval = _approval(payload=payload, execution_key="idem-immutable")
        request = CAESubmitRequest(
            project_id="project-1",
            simulation_id="sim-1",
            candidate_hash=HASH_A,
            scenario_hash=HASH_B,
            idempotency_key="idem-immutable",
            approval=approval,
            payload=payload,
        )
        payload["mesh"] = {"size": 999}
        nested = request.payload["mesh"]
        self.assertIsInstance(nested, Mapping)
        with self.assertRaises(TypeError):
            nested["size"] = 999  # type: ignore[index]
        loads = request.payload["loads"]
        self.assertIsInstance(loads, tuple)
        self.assertEqual(request.model_dump(mode="json")["payload"]["mesh"]["size"], 1)

        tampered = request.model_copy(update={"payload": {"mesh": {"size": 999}}})
        approvals = InMemoryCAEApprovalStore()
        approvals.register(approval)
        client = SimScaleCAEClient(
            base_url="https://api.simscale.com",
            api_key="secret",
            approval_verifier=approvals,
            transport=FakeCAETransport([{"runId": "must-not-run"}]),
            clock=lambda: NOW,
        )
        with self.assertRaisesRegex(ValueError, "exact request hash"):
            client.submit_run(tampered)

    def test_matlab_adapter_executes_only_allowlisted_functions(self) -> None:
        transport = FakeCAETransport([{"margin": 1.8, "unit": "ratio"}])
        matlab_payload = {"candidate_hash": HASH_A}
        approval = _approval(
            provider="matlab-production-server",
            operation="execute",
            payload=matlab_payload,
            target="forge:upper_arm_margin:local-run-1",
        )
        approvals = InMemoryCAEApprovalStore()
        approvals.register(approval)
        client = MATLABProductionServerClient(
            base_url="https://matlab.example.com",
            bearer_token="matlab-secret",
            application="forge",
            allowlisted_functions=("upper_arm_margin",),
            approval_verifier=approvals,
            transport=transport,
            clock=lambda: NOW,
        )
        result = client.execute_allowlisted_function(
            function_name="upper_arm_margin",
            project_id="project-1",
            simulation_id="sim-1",
            run_id="local-run-1",
            candidate_hash=HASH_A,
            scenario_hash=HASH_B,
            idempotency_key="idem-1234",
            approval=approval,
            payload=matlab_payload,
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
                candidate_hash=HASH_A,
                scenario_hash=HASH_B,
                idempotency_key="idem-1234",
                approval=approval,
                payload={},
            )

    def test_connectors_fail_closed_on_invalid_boundaries_and_errors(self) -> None:
        with self.assertRaises(ValueError):
            SimScaleCAEClient(
                base_url="http://api.simscale.com",
                api_key="secret",
                approval_verifier=InMemoryCAEApprovalStore(),
                transport=FakeCAETransport([{}]),
                clock=lambda: NOW,
            )
        approval = _approval()
        approvals = InMemoryCAEApprovalStore()
        approvals.register(approval)
        client = SimScaleCAEClient(
            base_url="https://api.simscale.com",
            api_key="secret",
            approval_verifier=approvals,
            transport=FakeCAETransport([{"error": "bad"}], status=403),
            clock=lambda: NOW,
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

    def test_cae_connector_rejects_malformed_provider_boundaries(self) -> None:
        approvals = InMemoryCAEApprovalStore()
        constructor_cases = (
            {"base_url": "https://api.example.com\n.invalid"},
            {"api_key": "bad\nkey"},
            {"timeout_seconds": 0},
            {"timeout_seconds": 61},
        )
        for override in constructor_cases:
            with self.subTest(override=override), self.assertRaises(ValueError):
                arguments: dict[str, object] = {
                    "base_url": "https://api.simscale.com",
                    "api_key": "secret",
                    "approval_verifier": approvals,
                    "transport": FakeCAETransport([{}]),
                    "clock": lambda: NOW,
                }
                arguments.update(override)
                SimScaleCAEClient(**arguments)  # type: ignore[arg-type]

        matlab_arguments = {
            "base_url": "https://matlab.example.com",
            "bearer_token": "secret",
            "application": "forge",
            "allowlisted_functions": ("upper_arm_margin",),
            "approval_verifier": approvals,
            "transport": FakeCAETransport([{}]),
            "clock": lambda: NOW,
        }
        for matlab_override in (
            {"bearer_token": ""},
            {"application": "bad/application"},
            {"allowlisted_functions": ()},
            {"allowlisted_functions": ("unsafe.function",)},
        ):
            with self.subTest(override=matlab_override), self.assertRaises(ValueError):
                matlab_case: dict[str, object] = dict(matlab_arguments)
                matlab_case.update(matlab_override)
                MATLABProductionServerClient(**matlab_case)  # type: ignore[arg-type]

        approval = _approval()
        request = CAESubmitRequest(
            project_id="project-1",
            simulation_id="sim-1",
            candidate_hash=HASH_A,
            scenario_hash=HASH_B,
            idempotency_key="idem-1234",
            approval=approval,
            payload={},
        )
        provider_failures = (
            (500, b'{"error":"provider"}', "cae_http_500"),
            (200, b"\xff", "cae_invalid_response"),
            (200, b"[]", "cae_invalid_response"),
            (200, b"x" * 2_000_001, "cae_response_too_large"),
            (200, b'{"state":"created"}', "cae_missing_run_id"),
            (200, b'{"runId":"unsafe/run"}', "safe identifier"),
        )
        for status, raw, message in provider_failures:
            with self.subTest(status=status, message=message):
                verifier = InMemoryCAEApprovalStore()
                verifier.register(approval)
                client = SimScaleCAEClient(
                    base_url="https://api.simscale.com",
                    api_key="secret",
                    approval_verifier=verifier,
                    transport=RawCAETransport(raw, status=status),
                    clock=lambda: NOW,
                )
                with self.assertRaisesRegex((CAEConnectorError, ValueError), message):
                    client.submit_run(request)

        verifier = InMemoryCAEApprovalStore()
        verifier.register(approval)
        naive_clock_client = SimScaleCAEClient(
            base_url="https://api.simscale.com",
            api_key="secret",
            approval_verifier=verifier,
            transport=FakeCAETransport([{"runId": "run-1"}]),
            clock=lambda: datetime(2026, 9, 9, 7, 30),
        )
        with self.assertRaisesRegex(ValueError, "timezone-aware UTC"):
            naive_clock_client.submit_run(request)

    def test_exact_approval_bindings_are_enforced_for_all_cae_phases(self) -> None:
        approval = _approval()
        request_payload = {
            "project_id": "project-1",
            "simulation_id": "sim-1",
            "candidate_hash": HASH_A,
            "scenario_hash": HASH_B,
            "idempotency_key": "idem-1234",
            "approval": approval.model_dump(mode="python"),
            "payload": {},
        }
        approval_mutations = (
            {"provider": "matlab-production-server", "operation": "execute"},
            {"operation": "execute"},
            {"project_id": "project-2"},
            {"simulation_id": "sim-2"},
            {"scenario_hash": HASH_A},
            {"execution_key": "idem-5678"},
        )
        for mutation in approval_mutations:
            with self.subTest(submit_mutation=mutation), self.assertRaises(ValueError):
                candidate = dict(request_payload)
                candidate["approval"] = approval.model_copy(update=mutation).model_dump(
                    mode="python"
                )
                CAESubmitRequest.model_validate(candidate)

        verifier = InMemoryCAEApprovalStore()
        verifier.register(approval)
        client = SimScaleCAEClient(
            base_url="https://api.simscale.com",
            api_key="secret",
            approval_verifier=verifier,
            transport=FakeCAETransport([{"runId": "run-1"}]),
            clock=lambda: NOW,
        )
        request = CAESubmitRequest.model_validate(request_payload)
        run = client.submit_run(request)
        start_cases = (
            (run.model_copy(update={"provider": "matlab-production-server"}), approval),
            (run, approval.model_copy(update={"project_id": "project-2"})),
            (run, approval.model_copy(update={"operation": "execute"})),
            (run, approval.model_copy(update={"simulation_id": "sim-2"})),
            (run, approval.model_copy(update={"candidate_hash": HASH_B})),
            (run, approval.model_copy(update={"scenario_hash": HASH_A})),
            (run, approval.model_copy(update={"approval_id": "approval-2"})),
            (run, approval.model_copy(update={"request_hash": HASH_A})),
        )
        for candidate_run, candidate_approval in start_cases:
            with (
                self.subTest(start_approval=candidate_approval),
                self.assertRaises(ValueError),
            ):
                client.start_run(
                    candidate_run,
                    approval=candidate_approval,
                    idempotency_key="idem-1234",
                )
        with self.assertRaisesRegex(ValueError, "approved execution key"):
            client.start_run(run, approval=approval, idempotency_key="idem-5678")
        with self.assertRaisesRegex(ValueError, "read SimScale"):
            client.get_status(
                run.model_copy(update={"provider": "matlab-production-server"})
            )

    def test_cae_execution_requires_registered_single_use_approval(self) -> None:
        approval = _approval()
        request = CAESubmitRequest(
            project_id="project-1",
            simulation_id="sim-1",
            candidate_hash=HASH_A,
            scenario_hash=HASH_B,
            idempotency_key="idem-1234",
            approval=approval,
            payload={},
        )
        transport = FakeCAETransport([{"runId": "run-1"}, {"runId": "run-2"}])
        approvals = InMemoryCAEApprovalStore()
        client = SimScaleCAEClient(
            base_url="https://api.simscale.com",
            api_key="secret",
            approval_verifier=approvals,
            transport=transport,
            clock=lambda: NOW,
        )
        with self.assertRaisesRegex(ValueError, "not registered"):
            client.submit_run(request)
        self.assertEqual(transport.requests, [])

        approvals.register(approval)
        first = client.submit_run(request)
        replayed = client.submit_run(request)
        self.assertEqual(replayed, first)
        self.assertEqual(len(transport.requests), 1)

    def test_sqlite_cae_ledger_replays_completed_result_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cae-executions.db"
            approval = _approval(payload={"load_n": 120})
            request = CAESubmitRequest(
                project_id="project-1",
                simulation_id="sim-1",
                candidate_hash=HASH_A,
                scenario_hash=HASH_B,
                idempotency_key="idem-1234",
                approval=approval,
                payload={"load_n": 120},
            )
            first_store = SQLiteCAEApprovalStore(path, clock=lambda: NOW)
            first_store.register(approval)
            first_transport = FakeCAETransport([{"runId": "run-durable"}])
            first_client = SimScaleCAEClient(
                base_url="https://api.simscale.com",
                api_key="secret",
                approval_verifier=first_store,
                transport=first_transport,
                clock=lambda: NOW,
            )
            first = first_client.submit_run(request)

            restarted_store = SQLiteCAEApprovalStore(path, clock=lambda: NOW)
            restarted_store.register(approval)
            replay_transport = FakeCAETransport([])
            restarted_client = SimScaleCAEClient(
                base_url="https://api.simscale.com",
                api_key="secret",
                approval_verifier=restarted_store,
                transport=replay_transport,
                clock=lambda: NOW,
            )
            replayed = restarted_client.submit_run(request)

            self.assertEqual(replayed, first)
            self.assertEqual(replay_transport.requests, [])
            with sqlite3.connect(path) as connection:
                row = connection.execute(
                    """
                    SELECT status, result_hash, provider_response_hash, receipt_json
                    FROM cae_execution_phases WHERE phase = 'submit'
                    """
                ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row[0], "completed")
            self.assertTrue(str(row[1]).startswith("sha256:"))
            self.assertEqual(row[2], first.evidence.response_hash)
            self.assertIn(first.approval_id, str(row[3]))

    def test_sqlite_cae_ledger_resumes_prepared_request_with_same_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cae-executions.db"
            approval = _approval(payload={"load_n": 120})
            request = CAESubmitRequest(
                project_id="project-1",
                simulation_id="sim-1",
                candidate_hash=HASH_A,
                scenario_hash=HASH_B,
                idempotency_key="idem-1234",
                approval=approval,
                payload={"load_n": 120},
            )
            interrupted = SQLiteCAEApprovalStore(path, clock=lambda: NOW)
            interrupted.register(approval)
            self.assertIsNone(interrupted.begin_once(approval, phase="submit"))

            restarted = SQLiteCAEApprovalStore(path, clock=lambda: NOW)
            restarted.register(approval)
            transport = FakeCAETransport([{"runId": "run-resumed"}])
            client = SimScaleCAEClient(
                base_url="https://api.simscale.com",
                api_key="secret",
                approval_verifier=restarted,
                transport=transport,
                clock=lambda: NOW,
            )
            run = client.submit_run(request)

            self.assertEqual(run.run_id, "run-resumed")
            headers = transport.requests[0]["headers"]
            self.assertIsInstance(headers, dict)
            assert isinstance(headers, dict)
            self.assertEqual(
                headers["Idempotency-Key"],
                approval.execution_key,
            )

    def test_simscale_start_requires_exact_completed_submit_and_receipt_is_immutable(
        self,
    ) -> None:
        approval = _approval()
        request = CAESubmitRequest(
            project_id="project-1",
            simulation_id="sim-1",
            candidate_hash=HASH_A,
            scenario_hash=HASH_B,
            idempotency_key="idem-1234",
            approval=approval,
            payload={},
        )
        memory = InMemoryCAEApprovalStore()
        memory.register(approval)
        seed_client = SimScaleCAEClient(
            base_url="https://api.simscale.com",
            api_key="secret",
            approval_verifier=memory,
            transport=FakeCAETransport([{"runId": "run-approved"}]),
            clock=lambda: NOW,
        )
        approved_run = seed_client.submit_run(request)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cae-start-binding.db"
            ticks = iter(
                (
                    NOW,
                    NOW + timedelta(seconds=1),
                    NOW + timedelta(seconds=2),
                    NOW + timedelta(seconds=3),
                    NOW + timedelta(seconds=4),
                )
            )
            durable = SQLiteCAEApprovalStore(path, clock=lambda: next(ticks))
            durable.register(approval)
            self.assertIsNone(durable.begin_once(approval, phase="submit"))
            start_transport = FakeCAETransport([{"state": "STARTED"}])
            client = SimScaleCAEClient(
                base_url="https://api.simscale.com",
                api_key="secret",
                approval_verifier=durable,
                transport=start_transport,
                clock=lambda: NOW,
            )
            with self.assertRaisesRegex(ValueError, "has not completed"):
                client.start_run(
                    approved_run,
                    approval=approval,
                    idempotency_key="idem-1234",
                )
            self.assertEqual(start_transport.requests, [])

            durable.complete_once(approval, phase="submit", result=approved_run)
            with sqlite3.connect(path) as connection:
                original_receipt = connection.execute(
                    """
                    SELECT receipt_json, receipt_hash, updated_at
                    FROM cae_execution_phases WHERE phase = 'submit'
                    """
                ).fetchone()
            self.assertIsNotNone(original_receipt)

            durable.complete_once(approval, phase="submit", result=approved_run)
            with sqlite3.connect(path) as connection:
                replayed_receipt = connection.execute(
                    """
                    SELECT receipt_json, receipt_hash, updated_at
                    FROM cae_execution_phases WHERE phase = 'submit'
                    """
                ).fetchone()
            self.assertEqual(replayed_receipt, original_receipt)

            forged_run = approved_run.model_copy(update={"run_id": "victim-run"})
            with self.assertRaisesRegex(ValueError, "completed submit result"):
                client.start_run(
                    forged_run,
                    approval=approval,
                    idempotency_key="idem-1234",
                )
            self.assertEqual(start_transport.requests, [])

            started = client.start_run(
                approved_run,
                approval=approval,
                idempotency_key="idem-1234",
            )
            self.assertEqual(started.payload["state"], "STARTED")
            self.assertEqual(len(start_transport.requests), 1)

    def test_cae_approval_ledgers_reject_conflicts_and_tampering(self) -> None:
        approval = _approval(payload={"load_n": 120})
        request = CAESubmitRequest(
            project_id="project-1",
            simulation_id="sim-1",
            candidate_hash=HASH_A,
            scenario_hash=HASH_B,
            idempotency_key="idem-1234",
            approval=approval,
            payload={"load_n": 120},
        )
        memory = InMemoryCAEApprovalStore()
        memory.register(approval)
        conflicting_approval = approval.model_copy(update={"candidate_hash": HASH_B})
        with self.assertRaisesRegex(ValueError, "another execution"):
            memory.register(conflicting_approval)
        with self.assertRaisesRegex(ValueError, "has not begun"):
            memory.assert_consumed(approval, phase="submit")
        with self.assertRaisesRegex(ValueError, "has not begun"):
            memory.complete_once(approval, phase="submit", result=approval)
        self.assertIsNone(memory.begin_once(approval, phase="submit"))
        self.assertIsNone(memory.begin_once(approval, phase="submit"))
        with self.assertRaisesRegex(ValueError, "has not completed"):
            memory.assert_consumed(approval, phase="submit")

        transport = FakeCAETransport([{"runId": "run-ledger"}])
        memory_client = SimScaleCAEClient(
            base_url="https://api.simscale.com",
            api_key="secret",
            approval_verifier=memory,
            transport=transport,
            clock=lambda: NOW,
        )
        run = memory_client.submit_run(request)
        with self.assertRaisesRegex(ValueError, "another result"):
            memory.complete_once(
                approval,
                phase="submit",
                result=run.model_copy(update={"run_id": "run-other"}),
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cae-integrity.db"
            durable = SQLiteCAEApprovalStore(path, clock=lambda: NOW)
            durable.register(approval)
            with self.assertRaisesRegex(ValueError, "not registered"):
                durable.begin_once(
                    approval.model_copy(update={"approval_id": "missing-approval"}),
                    phase="submit",
                )
            with self.assertRaisesRegex(ValueError, "has not begun"):
                durable.assert_consumed(approval, phase="submit")
            with self.assertRaisesRegex(ValueError, "has not begun"):
                durable.complete_once(approval, phase="submit", result=run)

            durable_transport = FakeCAETransport([{"runId": "run-durable"}])
            durable_client = SimScaleCAEClient(
                base_url="https://api.simscale.com",
                api_key="secret",
                approval_verifier=durable,
                transport=durable_transport,
                clock=lambda: NOW,
            )
            durable_run = durable_client.submit_run(request)
            with self.assertRaisesRegex(ValueError, "another result"):
                durable.complete_once(
                    approval,
                    phase="submit",
                    result=durable_run.model_copy(update={"run_id": "run-other"}),
                )

            second_approval = approval.model_copy(update={"approval_id": "approval-2"})
            durable.register(second_approval)
            with self.assertRaisesRegex(ValueError, "another approval"):
                durable.begin_once(second_approval, phase="submit")
            with self.assertRaisesRegex(ValueError, "another execution"):
                durable.register(approval.model_copy(update={"scenario_hash": HASH_A}))

            with sqlite3.connect(path) as connection:
                original = connection.execute(
                    """
                    SELECT result_json, result_hash, provider_response_hash,
                        receipt_hash, receipt_json
                    FROM cae_execution_phases WHERE phase = 'submit'
                    """
                ).fetchone()
            self.assertIsNotNone(original)
            assert original is not None

            tamper_cases = (
                ("result_json", None, "missing its result"),
                ("result_json", "[]", "JSON object"),
                ("result_hash", "sha256:" + "0" * 64, "result hash"),
                (
                    "provider_response_hash",
                    "sha256:" + "0" * 64,
                    "provider receipt",
                ),
                ("receipt_json", None, "missing its receipt"),
                ("receipt_hash", "sha256:" + "0" * 64, "receipt hash"),
            )
            for column, value, message in tamper_cases:
                with self.subTest(column=column, value=value):
                    with sqlite3.connect(path) as connection:
                        connection.execute(
                            f"UPDATE cae_execution_phases SET {column} = ?",
                            (value,),
                        )
                    with self.assertRaisesRegex(ValueError, message):
                        durable.begin_once(approval, phase="submit")
                    with sqlite3.connect(path) as connection:
                        connection.execute(
                            """
                            UPDATE cae_execution_phases SET result_json = ?,
                                result_hash = ?, provider_response_hash = ?,
                                receipt_hash = ?, receipt_json = ?
                            WHERE phase = 'submit'
                            """,
                            original,
                        )

            receipt = json.loads(str(original[4]))
            receipt["approval_id"] = "forged-approval"
            forged_receipt_json = json.dumps(
                receipt,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            forged_receipt_hash = (
                "sha256:" + hashlib.sha256(forged_receipt_json.encode()).hexdigest()
            )
            with sqlite3.connect(path) as connection:
                connection.execute(
                    """
                    UPDATE cae_execution_phases
                    SET receipt_json = ?, receipt_hash = ? WHERE phase = 'submit'
                    """,
                    (forged_receipt_json, forged_receipt_hash),
                )
            with self.assertRaisesRegex(ValueError, "receipt bindings"):
                durable.begin_once(approval, phase="submit")

    def test_ansys_is_declared_adapter_required_not_fake_connected(self) -> None:
        status = ansys_connector_status()
        self.assertEqual(status.provider, "ansys")
        self.assertEqual(status.status, "adapter_required")
        self.assertIn("version", status.required_inputs)


if __name__ == "__main__":
    unittest.main()
