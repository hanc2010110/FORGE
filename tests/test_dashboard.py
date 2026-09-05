from __future__ import annotations

import tempfile
import threading
import unittest
import urllib.request
from html.parser import HTMLParser
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


class _ReleaseTableStructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.tbody_ids: list[str] = []
        self.invalid_tbody_ancestors: list[tuple[str, ...]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tbody":
            tbody_id = dict(attrs).get("id")
            if tbody_id:
                self.tbody_ids.append(tbody_id)
            invalid = tuple(
                ancestor for ancestor in self.stack if ancestor in {"thead", "tr", "th"}
            )
            if invalid:
                self.invalid_tbody_ancestors.append(invalid)
        if tag in {"table", "thead", "tbody", "tr", "th", "td"}:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag not in {"table", "thead", "tbody", "tr", "th", "td"}:
            return
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index] == tag:
                del self.stack[index:]
                return


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
        # Keep the dependency-free dashboard small enough to load instantly while
        # allowing the cited local-RAG interaction to remain self-contained.
        self.assertLess(total_bytes, 300_000)

    def test_html_references_only_public_same_origin_assets(self) -> None:
        asset = load_dashboard_asset("/app/")
        assert asset is not None
        html = asset.body.decode("utf-8")
        self.assertIn('href="./styles.css"', html)
        self.assertIn('defer src="./app.js"', html)
        self.assertNotIn('type="module"', html)
        self.assertIn('class="brand" href="./"', html)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("<style", html)
        self.assertNotIn("<script>", html)
        self.assertIn('id="decision-summary"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn('pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,127}"', html)
        self.assertIn('id="connect-panel"', html)
        self.assertIn('id="start-panel"', html)
        self.assertIn('id="start-change-form"', html)
        self.assertIn('id="start-change-input"', html)
        self.assertIn('id="connected-data-summary"', html)
        self.assertIn('class="forge-workspace"', html)
        self.assertIn('id="chat-sidebar"', html)
        self.assertIn("＋ New chat", html)
        self.assertIn("Recent sessions", html)
        self.assertIn("Pre-deploy essentials", html)
        self.assertIn("Static chat UX", html)
        self.assertIn("Live CAD/CAE/device connectors", html)
        self.assertIn('id="project-explorer"', html)
        self.assertIn('class="engineering-view"', html)
        self.assertIn('id="workflow-guide"', html)
        self.assertIn('id="workflow-next-action"', html)
        self.assertIn('data-flow-stage="plan"', html)
        self.assertIn('data-flow-stage="verify"', html)
        self.assertIn('data-flow-stage="release"', html)
        self.assertIn('id="forge-ai-panel"', html)
        self.assertIn('id="forge-ai-form"', html)
        self.assertIn('id="forge-ai-submit"', html)
        self.assertIn('id="forge-ai-transcript"', html)
        self.assertNotIn('class="stage-tracker"', html)
        self.assertIn('id="guided-stage-list"', html)
        self.assertIn('id="guided-workflow"', html)
        self.assertIn('id="guided-workflow-content"', html)
        self.assertIn('id="advanced-console"', html)
        self.assertIn("Advanced source-bound evidence console", html)
        self.assertIn('id="forge-ai-toggle"', html)
        self.assertIn('id="forge-ai-content"', html)
        self.assertIn('id="conversation-context"', html)
        self.assertIn('id="context-goal"', html)
        self.assertIn('id="context-constraints"', html)
        self.assertIn('id="context-required"', html)
        self.assertIn('id="connect-form"', html)
        self.assertIn('id="connect-connector-id"', html)
        self.assertIn('id="connect-capture-key"', html)
        self.assertIn('id="sample-connect-button"', html)
        self.assertIn('id="sample-preview-button"', html)
        self.assertIn('id="asset-kind"', html)
        self.assertIn('value="cad_model"', html)
        self.assertIn('value="connected_device"', html)
        self.assertIn('name="plan-intent"', html)
        self.assertIn('value="change_impact"', html)
        self.assertIn('value="operating_scenario"', html)
        self.assertIn('id="external-plan-fields"', html)
        self.assertIn('id="external-test-id"', html)
        self.assertIn('id="design-proposal-form"', html)
        self.assertIn('id="engineering-goal"', html)
        self.assertIn('id="engineering-priority"', html)
        self.assertIn('id="engineering-constraints"', html)
        self.assertIn('id="design-preview-hash"', html)
        self.assertIn("Design alternatives는 planning-only advisory", html)
        self.assertIn('name="verify-mode"', html)
        self.assertIn('value="external_evidence_plan"', html)
        self.assertIn('id="baseline-snapshot-id"', html)
        self.assertIn('id="proposed-hardware-revision-id"', html)
        self.assertIn('id="before-part-number"', html)
        self.assertIn('id="after-part-number"', html)
        self.assertIn('id="before-quote-source-hash"', html)
        self.assertIn('id="after-quote-source-hash"', html)
        self.assertIn('id="release-diagnosis"', html)
        self.assertIn('id="release-diagnosis-form"', html)
        self.assertIn('id="diagnosis-decision-hash"', html)
        self.assertIn("증거 기반 blocker 진단", html)
        self.assertNotIn("scenario-json", html)
        self.assertNotIn("CreateChangePreviewCommand JSON", html)

    def test_plan_verify_release_vocabularies_stay_separate(self) -> None:
        asset = load_dashboard_asset("/app/")
        assert asset is not None
        html = asset.body.decode("utf-8")
        plan_region = html.split('<section id="verify-panel"', 1)[0]
        self.assertIn("예상 영향", plan_region)
        self.assertIn("Engineering Session", plan_region)
        self.assertIn("planning-only advisory", plan_region)
        self.assertNotIn("READY", plan_region)
        self.assertNotIn("BLOCKED", plan_region)
        self.assertIn("00 CONNECT/IMPORT", html)
        self.assertIn("기준 자산 snapshot 연결", html)
        self.assertIn("01 · PLAN", html)
        self.assertIn("02 · VERIFY", html)
        self.assertIn("03 · RELEASE", html)

        script_asset = load_dashboard_asset("/app/app.js")
        assert script_asset is not None
        script = script_asset.body.decode("utf-8")
        self.assertIn("const PLAN_RECOMMENDATIONS = new Set", script)
        self.assertIn('"changes_required"', script)
        self.assertIn("const RELEASE_STATES = new Set", script)
        self.assertIn(
            'const IS_FILE_PROTOCOL = window.location.protocol === "file:"',
            script,
        )
        self.assertIn("enterFileProtocolMode()", script)
        self.assertIn("if (IS_FILE_PROTOCOL) {", script)
        self.assertIn("로컬 파일 미리보기 모드", script)
        self.assertIn("file-mode-notice", script)
        self.assertIn("./forge dashboard가 출력한 loopback URL", script)
        self.assertIn('"ready"', script)
        self.assertIn('"blocked"', script)
        self.assertIn("invalid_preview_recommendation", script)
        self.assertIn("invalid_release_state", script)

    def test_release_evidence_table_bodies_are_structurally_valid(self) -> None:
        asset = load_dashboard_asset("/app/")
        assert asset is not None
        parser = _ReleaseTableStructureParser()
        parser.feed(asset.body.decode("utf-8"))
        self.assertEqual(parser.invalid_tbody_ancestors, [])
        self.assertIn("change-rows", parser.tbody_ids)
        self.assertIn("retest-rows", parser.tbody_ids)

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
        self.assertIn("changePreviewPayload()", script)
        self.assertIn("captureSnapshotPayload()", script)
        self.assertIn("externalEvidencePlanPayload()", script)
        self.assertIn("designProposalPayload()", script)
        self.assertIn("releaseDiagnosisPayload()", script)
        self.assertIn("resolutionPlanPayload(", script)
        self.assertIn("assetInputPayload()", script)
        self.assertIn("renderOperatingScenarioPlan(", script)
        self.assertIn("renderDesignProposals(", script)
        self.assertIn("impactMap(preview)", script)
        self.assertIn("renderImpactInspector(", script)
        self.assertIn("designAlternativeCards(", script)
        self.assertIn("renderSelectedPlanDraft(", script)
        self.assertIn("setWorkflowStage(", script)
        self.assertIn("renderGuidedWorkflow()", script)
        self.assertIn("handleGuidedAction(action, value)", script)
        self.assertIn("persistGuidedCandidateAndSimulation(revision)", script)
        self.assertIn("persistGuidedTransition(toState", script)
        self.assertIn('persistGuidedMutation("design-candidates"', script)
        self.assertIn('persistGuidedMutation("design-simulations"', script)
        self.assertIn('persistGuidedMutation("conversational-claims"', script)
        self.assertIn('persistGuidedMutation("design-transitions"', script)
        self.assertIn("immutable ledger에 저장했습니다", script)
        self.assertIn("Robot Arm Project", script)
        self.assertIn("EC-014", script)
        self.assertIn("로봇 팔 길이를 10cm 늘리고 싶어", script)
        self.assertIn("conversationMessage(", script)
        self.assertIn("engineeringComposer(", script)
        self.assertIn("ingestSTLFile(file)", script)
        self.assertIn("mountSTLViewer(geometryCanvas, geometry)", script)
        self.assertIn("/cad-geometries", script)
        self.assertIn("renderEngineeringSession()", script)
        self.assertIn("workspaceOpen", script)
        self.assertIn("workspaceToggleButton()", script)
        self.assertIn("predeployEssentialsCard()", script)
        self.assertIn('"open-workspace"', script)
        self.assertIn('"close-workspace"', script)
        self.assertIn("livePlanCard()", script)
        self.assertIn("revisionComparisonCard()", script)
        self.assertIn('"Current"', script)
        self.assertIn('"Proposed"', script)
        self.assertIn('"Difference"', script)
        self.assertIn("Confirm & Simulate", script)
        self.assertIn("Design Candidate DC-014", script)
        self.assertIn("SIM-023", script)
        self.assertIn("SIM-024", script)
        self.assertIn("Maximum Deflection", script)
        self.assertIn("MEASURED · REQUIRED · not imported", script)
        for evidence_kind in (
            '"FACT"',
            '"CALCULATED"',
            '"SIMULATED"',
            '"INFERRED"',
        ):
            self.assertIn(evidence_kind, script)
        self.assertIn("MVP: deterministic local fixture", script)
        self.assertIn("browser-local demo", script)
        self.assertIn("연결된 project ledger에 저장합니다", script)
        self.assertIn("browser-local demo로만 유지됩니다", script)
        self.assertIn("setFlowGuide(", script)
        self.assertIn("scrollToFlowTarget(", script)
        self.assertIn("IntersectionObserver", script)
        self.assertIn('reducedMotion.matches ? "auto" : "smooth"', script)
        self.assertIn("Expected vs actual", script)
        self.assertIn("LOCAL PLAN DRAFT", script)
        self.assertIn("서버 승인 상태는 생성하지 않았습니다", script)
        self.assertIn("renderReleaseDiagnosis(", script)
        self.assertIn("Required external evidence", script)
        self.assertIn('componentDraft("before")', script)
        self.assertIn('componentDraft("after")', script)
        self.assertIn("optionalQuote(prefix, partNumber)", script)
        self.assertIn("/api/v1/projects/${project}/change-previews", script)
        self.assertIn("/api/v1/projects/${project}/design-proposals", script)
        self.assertIn(
            "/api/v1/projects/${project}/design-proposals/${proposalHash}", script
        )
        self.assertIn("/api/v1/projects/${project}/connector-snapshots", script)
        self.assertIn("/api/v1/projects/${project}/external-evidence-plans", script)
        self.assertIn(
            "/api/v1/projects/${project}/external-evidence-plan-verifications",
            script,
        )
        self.assertIn("/api/v1/projects/${project}/plan-verifications", script)
        self.assertIn("/api/v1/projects/${project}/release-diagnoses", script)
        self.assertIn(
            "/api/v1/projects/${project}/release-diagnoses/${diagnosisHash}", script
        )
        self.assertIn("/api/v1/projects/${project}/resolution-plans", script)
        self.assertIn(
            "/api/v1/projects/${project}/resolution-plans/${planHash}", script
        )
        self.assertIn("/api/v1/connectors", script)
        self.assertIn('"If-Match": `"project:${project}:v${expectedVersion}"`', script)
        self.assertIn("no-source-writeback", script)
        self.assertIn("no-device-control", script)
        self.assertIn("focusTarget?.focus({ preventScroll: true })", script)
        self.assertIn("target.scrollIntoView({ behavior:", script)
        self.assertNotIn("specification_hash", script)

    def test_dashboard_css_keeps_generated_tables_locally_scrollable(self) -> None:
        asset = load_dashboard_asset("/app/styles.css")
        assert asset is not None
        css = asset.body.decode("utf-8")
        self.assertIn(".local-table-wrap", css)
        self.assertIn("overflow-x: auto", css)
        self.assertIn(".session-card", css)
        self.assertIn(".fix-card", css)
        self.assertIn(".forge-workspace", css)
        self.assertIn(".chat-sidebar", css)
        self.assertIn(".chat-nav", css)
        self.assertIn(".predeploy-readiness", css)
        self.assertIn(".predeploy-card", css)
        self.assertIn(".workspace-toggle", css)
        self.assertIn(".project-explorer", css)
        self.assertIn(".engineering-view", css)
        self.assertIn(".forge-ai-panel", css)
        self.assertIn(".workflow-guide", css)
        self.assertIn(".workflow-focus", css)
        self.assertIn("prefers-reduced-motion:reduce", css)
        self.assertIn(".impact-map", css)
        self.assertIn(".impact-node", css)
        self.assertIn(".alternative-grid", css)
        self.assertIn(".expected-actual-table", css)
        self.assertIn(".guided-workflow", css)
        self.assertIn(".guided-stage-list", css)
        self.assertIn(".guided-impact-graph", css)
        self.assertIn(".guided-alternative-grid", css)
        self.assertIn(".task-tree", css)
        self.assertIn(".resolve-option", css)
        self.assertIn(".advanced-console", css)
        self.assertIn(".forge-ai-panel.collapsed", css)
        self.assertIn(".collaboration-workspace", css)
        self.assertIn(".collaboration-workspace.workspace-open", css)
        self.assertIn(".file-mode-notice", css)
        self.assertIn(".engineering-pane", css)
        self.assertIn(".forge-conversation-pane", css)
        self.assertIn(".live-plan-card", css)
        self.assertIn(".session-status-bar", css)
        self.assertIn("--canvas:#090d12", css)

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
                        b"FORGE \xc2\xb7 Conversational Engineering", response.read()
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
