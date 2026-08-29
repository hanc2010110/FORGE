from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any

from forge_core.loopback_api import LoopbackAPI, create_loopback_server
from forge_core.release_persistence import RawReleaseEvidence
from forge_core.release_service import (
    AnalyzeChangeCommand,
    CreateProjectCommand,
    EvaluateReleaseCommand,
    IngestReleaseEvidenceCommand,
    ReleaseIntegrationService,
)
from forge_core.sqlite_store import SQLiteEvidenceStore
from tests.test_release_readiness import (
    NOW,
    build_evidence,
    cost_record,
    required_test_evidence,
)
from tests.test_release_service import capture_command, registry


def json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class LoopbackAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SQLiteEvidenceStore(Path(self.temporary.name) / "forge.db")
        self.service = ReleaseIntegrationService(
            self.store, registry(), clock=lambda: NOW
        )
        self.api = LoopbackAPI(
            self.service,
            port=43127,
            csrf_secret=b"test-secret",
            local_installation_id="installation-1",
            nonce_factory=lambda: "a" * 64,
            max_request_bytes=1_048_576,
        )
        session = self.api.handle(
            "GET", "/api/v1/session", [("Host", self.api.expected_host)]
        )
        self.token = str(session.json()["csrf_token"])

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def mutation_headers(
        self,
        body: bytes,
        *,
        key: str = "request-1",
        version: int | None = None,
    ) -> list[tuple[str, str]]:
        headers = [
            ("Host", self.api.expected_host),
            ("Origin", self.api.expected_origin),
            ("Cookie", f"forge_csrf={self.token}"),
            ("X-FORGE-CSRF", self.token),
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
            ("Idempotency-Key", key),
        ]
        if version is not None:
            headers.append(("If-Match", f'"project:project-1:v{version}"'))
        return headers

    def post(
        self,
        target: str,
        payload: object,
        *,
        key: str,
        version: int | None,
    ) -> Any:
        body = json_bytes(payload)
        return self.api.handle(
            "POST",
            target,
            self.mutation_headers(body, key=key, version=version),
            body,
        )

    def test_full_change_release_flow_and_read_routes(self) -> None:
        created = self.post(
            "/api/v1/projects",
            CreateProjectCommand(project_id="project-1", name="Project One").model_dump(
                mode="json"
            ),
            key="create",
            version=None,
        )
        self.assertEqual(created.status, 201)
        self.assertEqual(created.headers["ETag"], '"project:project-1:v1"')
        self.assertEqual(created.headers["Idempotency-Replayed"], "false")

        replay = self.post(
            "/api/v1/projects",
            {"name": "Project One", "project_id": "project-1"},
            key="create",
            version=None,
        )
        self.assertEqual(replay.status, 201)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")

        before = capture_command("before", "snapshot-11", NOW - timedelta(hours=2))
        after = capture_command("after", "snapshot-12", NOW - timedelta(hours=1))
        first = self.post(
            "/api/v1/projects/project-1/connector-snapshots",
            before.model_dump(mode="json"),
            key="before",
            version=1,
        )
        second = self.post(
            "/api/v1/projects/project-1/connector-snapshots",
            after.model_dump(mode="json"),
            key="after",
            version=2,
        )
        self.assertEqual((first.status, second.status), (201, 201))

        impact = self.post(
            "/api/v1/projects/project-1/change-impacts",
            AnalyzeChangeCommand(
                from_snapshot_id="snapshot-11", to_snapshot_id="snapshot-12"
            ).model_dump(mode="json"),
            key="impact",
            version=3,
        )
        analysis_hash = impact.json()["change_assessment"]["analysis_hash"]
        assessment = self.store.get_change_assessment(analysis_hash)
        snapshot = self.store.get_connector_snapshot("project-1", "snapshot-12")
        evidence: tuple[RawReleaseEvidence, ...] = (
            cost_record(snapshot.snapshot),
            build_evidence(assessment.assessment, snapshot.snapshot),
            *required_test_evidence(assessment.assessment, snapshot.snapshot),
        )
        evidence_ids: list[str] = []
        version = 4
        for index, item in enumerate(evidence, start=1):
            ingested = self.post(
                "/api/v1/projects/project-1/release-evidence",
                IngestReleaseEvidenceCommand(
                    analysis_hash=analysis_hash, evidence=item
                ).model_dump(mode="json"),
                key=f"evidence-{index}",
                version=version,
            )
            self.assertEqual(ingested.status, 201)
            evidence_ids.append(ingested.json()["release_evidence"]["evidence_id"])
            version += 1

        decision = self.post(
            "/api/v1/projects/project-1/release-decisions",
            EvaluateReleaseCommand(analysis_hash=analysis_hash).model_dump(mode="json"),
            key="decision",
            version=version,
        )
        self.assertEqual(decision.status, 201)
        decision_hash = decision.json()["release_decision"]["decision_hash"]
        self.assertEqual(
            decision.json()["release_decision"]["decision"]["report"]["status"],
            "ready",
        )

        read_targets = (
            "/api/v1/projects/project-1",
            "/api/v1/projects/project-1/connector-snapshots/snapshot-12",
            f"/api/v1/projects/project-1/change-impacts/{analysis_hash}",
            f"/api/v1/projects/project-1/release-evidence/{evidence_ids[0]}",
            f"/api/v1/projects/project-1/release-decisions/{decision_hash}",
        )
        for target in read_targets:
            response = self.api.handle(
                "GET", target, [("Host", self.api.expected_host)]
            )
            self.assertEqual(response.status, 200, target)
            self.assertIn("data", response.json())

    def test_host_origin_csrf_and_preflight_are_strict(self) -> None:
        health = self.api.handle(
            "GET", "/api/v1/health", [("Host", self.api.expected_host)]
        )
        self.assertEqual(health.status, 200)
        self.assertEqual(health.headers["Cache-Control"], "no-store")
        self.assertEqual(health.headers["X-Content-Type-Options"], "nosniff")

        invalid_requests: tuple[list[tuple[str, str]], ...] = (
            [],
            [("Host", "localhost:43127")],
            [("Host", self.api.expected_host), ("Host", self.api.expected_host)],
        )
        for headers in invalid_requests:
            response = self.api.handle("GET", "/api/v1/health", headers)
            self.assertEqual(response.status, 400)

        body = json_bytes({"project_id": "project-1", "name": "Project One"})
        base = self.mutation_headers(body)
        no_origin = [item for item in base if item[0] != "Origin"]
        wrong_origin = [
            (name, "http://evil.invalid") if name == "Origin" else (name, value)
            for name, value in base
        ]
        no_csrf = [item for item in base if item[0] != "X-FORGE-CSRF"]
        self.assertEqual(
            self.api.handle("POST", "/api/v1/projects", no_origin, body).status, 403
        )
        self.assertEqual(
            self.api.handle("POST", "/api/v1/projects", wrong_origin, body).status,
            403,
        )
        self.assertEqual(
            self.api.handle("POST", "/api/v1/projects", no_csrf, body).status, 400
        )

        preflight_headers = [
            ("Host", self.api.expected_host),
            ("Origin", self.api.expected_origin),
            ("Access-Control-Request-Method", "POST"),
            (
                "Access-Control-Request-Headers",
                "content-type,idempotency-key,if-match,x-forge-csrf",
            ),
        ]
        preflight = self.api.handle("OPTIONS", "/api/v1/projects", preflight_headers)
        self.assertEqual(preflight.status, 204)
        self.assertEqual(preflight.body, b"")
        missing_origin = [item for item in preflight_headers if item[0] != "Origin"]
        self.assertEqual(
            self.api.handle("OPTIONS", "/api/v1/projects", missing_origin).status,
            403,
        )
        forbidden = preflight_headers[:-1] + [
            ("Access-Control-Request-Headers", "authorization")
        ]
        self.assertEqual(
            self.api.handle("OPTIONS", "/api/v1/projects", forbidden).status, 403
        )

    def test_json_framing_media_type_and_validation_fail_closed(self) -> None:
        target = "/api/v1/projects"
        valid = json_bytes({"project_id": "project-1", "name": "Project One"})
        cases: tuple[tuple[int, list[tuple[str, str]], bytes], ...] = (
            (
                411,
                [
                    item
                    for item in self.mutation_headers(valid)
                    if item[0] != "Content-Length"
                ],
                valid,
            ),
            (
                400,
                self.mutation_headers(valid) + [("Content-Length", str(len(valid)))],
                valid,
            ),
            (
                400,
                self.mutation_headers(valid) + [("Transfer-Encoding", "chunked")],
                valid,
            ),
            (
                415,
                [
                    (name, "application/json; charset=utf-8")
                    if name == "Content-Type"
                    else (name, value)
                    for name, value in self.mutation_headers(valid)
                ],
                valid,
            ),
            (
                400,
                self.mutation_headers(b'{"name":"a","name":"b"}'),
                b'{"name":"a","name":"b"}',
            ),
            (400, self.mutation_headers(b'{"value":NaN}'), b'{"value":NaN}'),
            (400, self.mutation_headers(b"[]"), b"[]"),
            (400, self.mutation_headers(b"{broken"), b"{broken"),
            (
                413,
                self.mutation_headers(b"x" * 1_048_577),
                b"x" * 1_048_577,
            ),
        )
        for expected, headers, body in cases:
            response = self.api.handle("POST", target, headers, body)
            self.assertEqual(response.status, expected, response.json())
            self.assertNotIn(str(Path(self.temporary.name)), response.body.decode())

        validation = self.post(
            target, {"project_id": "bad/id", "name": "x"}, key="bad", version=None
        )
        self.assertEqual(validation.status, 422)
        self.assertEqual(validation.json()["error"]["code"], "validation_failed")
        self.assertNotIn("input", validation.body.decode())

    def test_routes_methods_conflicts_and_targets_are_bounded(self) -> None:
        created = self.post(
            "/api/v1/projects",
            {"project_id": "project-1", "name": "Project One"},
            key="create",
            version=None,
        )
        self.assertEqual(created.status, 201)

        conflict = self.post(
            "/api/v1/projects",
            {"project_id": "project-1", "name": "Changed Name"},
            key="create",
            version=None,
        )
        self.assertEqual(conflict.status, 409)
        stale = self.post(
            "/api/v1/projects/project-1/connector-snapshots",
            capture_command("before", "snapshot-11", NOW).model_dump(mode="json"),
            key="stale",
            version=2,
        )
        self.assertEqual(stale.status, 409)

        for target in (
            "/api/v1/projects?x=1",
            "/api/v1/%2e%2e/.git/config",
            "/api/v1/../secret",
            "/etc/passwd",
        ):
            response = self.api.handle(
                "GET", target, [("Host", self.api.expected_host)]
            )
            self.assertIn(response.status, {400, 404})
            self.assertNotIn("/Users/", response.body.decode())

        method = self.api.handle(
            "DELETE",
            "/api/v1/projects/project-1",
            [
                ("Host", self.api.expected_host),
                ("Origin", self.api.expected_origin),
            ],
        )
        self.assertEqual(method.status, 405)
        self.assertEqual(method.headers["Allow"], "GET")
        missing = self.api.handle(
            "GET",
            "/api/v1/projects/missing",
            [("Host", self.api.expected_host)],
        )
        self.assertEqual(missing.status, 404)

    def test_server_binds_only_ipv4_loopback_and_hides_runtime_banner(self) -> None:
        for host in ("0.0.0.0", "localhost", "::1"):
            with self.assertRaises(ValueError):
                create_loopback_server(self.service, host=host)
        with self.assertRaises(ValueError):
            create_loopback_server(self.service, csrf_secret=b"")
        with self.assertRaises(ValueError):
            LoopbackAPI(
                self.service,
                port=1,
                csrf_secret=b"secret",
                local_installation_id="bad/path",
            )
        with self.assertRaises(ValueError):
            LoopbackAPI(
                self.service,
                port=1,
                csrf_secret=b"secret",
                local_installation_id="installation-1",
                max_request_bytes=0,
            )

        server = create_loopback_server(
            self.service,
            csrf_secret=b"server-secret",
            local_installation_id="installation-1",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertEqual(server.server_address[0], "127.0.0.1")
            connection = http.client.HTTPConnection(
                "127.0.0.1", int(server.server_address[1]), timeout=2
            )
            connection.request("GET", "/api/v1/health")
            response = connection.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(response.getheader("Server"), "FORGE")
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
