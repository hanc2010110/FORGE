from __future__ import annotations

import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from forge_core.dashboard import (
    DASHBOARD_CONTENT_SECURITY_POLICY,
    load_dashboard_asset,
)
from forge_core.dashboard_server import create_dashboard_server, main
from forge_core.loopback_api import LoopbackAPI
from forge_core.release_service import ReleaseIntegrationService
from forge_core.sqlite_store import SQLiteEvidenceStore
from tests.test_release_readiness import NOW
from tests.test_release_service import registry


class DashboardAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SQLiteEvidenceStore(Path(self.temporary.name) / "forge.db")
        self.api = LoopbackAPI(
            ReleaseIntegrationService(self.store, registry(), clock=lambda: NOW),
            port=43127,
            csrf_secret=b"test-secret",
            local_installation_id="installation-1",
        )
        self.headers = [("Host", self.api.expected_host)]

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_dashboard_assets_are_allowlisted_same_origin_and_bounded(self) -> None:
        expected_types = {
            "/app/": "text/html; charset=utf-8",
            "/app/styles.css": "text/css; charset=utf-8",
            "/app/app.js": "text/javascript; charset=utf-8",
        }
        total_bytes = 0
        for target, expected_type in expected_types.items():
            with self.subTest(target=target):
                response = self.api.handle("GET", target, self.headers)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Content-Type"], expected_type)
                self.assertEqual(
                    response.headers["Content-Security-Policy"],
                    DASHBOARD_CONTENT_SECURITY_POLICY,
                )
                self.assertNotIn(
                    "'unsafe-inline'", response.headers["Content-Security-Policy"]
                )
                self.assertEqual(
                    int(response.headers["Content-Length"]), len(response.body)
                )
                total_bytes += len(response.body)
        self.assertLess(total_bytes, 100_000)

    def test_html_references_only_public_same_origin_assets(self) -> None:
        asset = load_dashboard_asset("/app/")
        assert asset is not None
        html = asset.body.decode("utf-8")
        self.assertIn('href="/app/styles.css"', html)
        self.assertIn('src="/app/app.js"', html)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("<style", html)
        self.assertNotIn("<script>", html)
        self.assertIn('id="decision-summary"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn('pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,127}"', html)

    def test_javascript_uses_safe_dom_and_preserves_evidence_tiers(self) -> None:
        asset = load_dashboard_asset("/app/app.js")
        assert asset is not None
        script = asset.body.decode("utf-8")
        for unsafe in ("innerHTML", "outerHTML", "document.write", "eval("):
            self.assertNotIn(unsafe, script)
        for tier in (
            '"simulation"',
            '"bench"',
            '"hil"',
            '"physical_device"',
        ):
            self.assertIn(tier, script)
        self.assertIn("textContent", script)
        self.assertIn("replaceChildren", script)
        self.assertIn('credentials: "same-origin"', script)
        self.assertIn("/api/v1/projects/${project}/release-decisions/", script)
        self.assertNotIn("encodeURIComponent(project)", script)
        self.assertIn("PROJECT_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/", script)

    def test_dashboard_methods_unknown_assets_and_targets_fail_closed(self) -> None:
        post = self.api.handle(
            "POST",
            "/app/",
            self.headers + [("Origin", self.api.expected_origin)],
            b"",
        )
        self.assertEqual(post.status, 405)
        self.assertEqual(post.headers["Allow"], "GET")
        self.assertEqual(post.headers["Content-Type"], "application/json")

        missing = self.api.handle("GET", "/app/private.db", self.headers)
        self.assertEqual(missing.status, 404)
        self.assertEqual(missing.headers["Content-Type"], "application/json")

        traversal = self.api.handle("GET", "/app/../forge.db", self.headers)
        self.assertEqual(traversal.status, 400)
        query = self.api.handle("GET", "/app/?project=secret", self.headers)
        self.assertEqual(query.status, 400)
        wrong_host = self.api.handle("GET", "/app/", [("Host", "localhost:43127")])
        self.assertEqual(wrong_host.status, 400)


class DashboardServerTests(unittest.TestCase):
    def test_server_binds_only_to_loopback_and_serves_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server, store = create_dashboard_server(
                Path(temporary) / "forge.db", port=0
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host = str(server.server_address[0])
                port = int(server.server_address[1])
                self.assertEqual(host, "127.0.0.1")
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/app/", timeout=2
                ) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(
                        response.headers["Content-Security-Policy"],
                        DASHBOARD_CONTENT_SECURITY_POLICY,
                    )
                    self.assertIn(
                        b"FORGE \xc2\xb7 Release Evidence Console", response.read()
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                store.close()

    def test_main_rejects_invalid_port_before_opening_store(self) -> None:
        with (
            patch.dict(
                "os.environ", {"FORGE_DASHBOARD_PORT": "not-a-port"}, clear=True
            ),
            self.assertRaisesRegex(SystemExit, "must be an integer"),
        ):
            main()

        with (
            patch.dict("os.environ", {"FORGE_DASHBOARD_PORT": "65536"}, clear=True),
            self.assertRaisesRegex(SystemExit, "must be between"),
        ):
            main()


if __name__ == "__main__":
    unittest.main()
