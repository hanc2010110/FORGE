from __future__ import annotations

import base64
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from forge_core.agent_gateway import ForgeAgentGateway
from forge_core.connectors import ConnectorRegistry
from forge_core.integration_hub import IntegrationHub
from forge_core.release_service import ReleaseIntegrationService
from forge_core.sqlite_store import SQLiteEvidenceStore

NOW = datetime(2026, 9, 9, 9, 0, tzinfo=UTC)


class ForgeAgentGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SQLiteEvidenceStore(Path(self.temporary.name) / "forge.db")
        service = ReleaseIntegrationService(
            self.store, ConnectorRegistry(), clock=lambda: NOW
        )
        self.gateway = ForgeAgentGateway(
            service,
            integration_hub=IntegrationHub(),
            installation_id="test-agent",
            clock=lambda: NOW,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_tool_catalog_separates_reads_from_approval_required_writes(self) -> None:
        tools = self.gateway.list_tools()
        by_name = {item["name"]: item for item in tools}
        self.assertTrue(
            by_name["forge_get_project_context"]["annotations"]["readOnlyHint"]
        )
        confirm = by_name["forge_confirm_design_candidate"]
        self.assertFalse(confirm["annotations"]["readOnlyHint"])
        self.assertTrue(confirm["_meta"]["forge/approvalRequired"])
        self.assertIn("$defs", confirm["inputSchema"])
        command_schema = confirm["inputSchema"]["properties"]["command"]
        self.assertIn("approval_id", command_schema["required"])
        self.assertIn("approval_nonce", command_schema["required"])
        self.assertNotIn("forge_approve_design_candidate", by_name)
        self.assertNotIn("forge_run_conversation", by_name)
        self.assertNotIn("forge_bind_simulation_result", by_name)
        self.assertNotIn("forge_ingest_release_evidence", by_name)
        self.assertNotIn("forge_evaluate_release", by_name)
        self.assertTrue(
            by_name["forge_inspect_cad_import"]["annotations"]["readOnlyHint"]
        )

    def test_create_project_and_read_compact_context(self) -> None:
        created = self.gateway.call_tool(
            "forge_create_project",
            {
                "request_id": "create-project-1",
                "command": {"project_id": "robot-arm", "name": "Robot Arm"},
            },
        )
        self.assertEqual(created["project_version"], 1)
        self.assertFalse(created["replayed"])

        replayed = self.gateway.call_tool(
            "forge_create_project",
            {
                "request_id": "create-project-1",
                "command": {"project_id": "robot-arm", "name": "Robot Arm"},
            },
        )
        self.assertTrue(replayed["replayed"])

        context = self.gateway.call_tool(
            "forge_get_project_context", {"project_id": "robot-arm"}
        )
        self.assertEqual(context["project"]["project_id"], "robot-arm")
        self.assertEqual(context["counts"]["release_decisions"], 0)
        self.assertIsNone(context["latest"]["simulations"])
        self.assertFalse(context["authority"]["llm_may_decide_release"])

    def test_capabilities_and_integration_truth_are_explicit(self) -> None:
        capabilities = self.gateway.call_tool("forge_get_capabilities", {})
        self.assertEqual(capabilities["primary_ux"], "LLM host conversation")
        self.assertIn(
            "AI BOM prices are estimates, never release evidence.",
            capabilities["hard_boundaries"],
        )
        integrations = self.gateway.call_tool("forge_list_integrations", {})
        entries = cast(list[dict[str, Any]], integrations["integrations"])
        states = {
            item["provider"]["provider_id"]: item["connection_state"]
            for item in entries
        }
        self.assertEqual(states["llm_host_mcp"], "local_available")
        self.assertNotIn("nexar_octopart", states)

    def test_read_only_step_inspection_is_available_to_llm_host(self) -> None:
        self.gateway.call_tool(
            "forge_create_project",
            {
                "request_id": "create-cad-project",
                "command": {"project_id": "cad-project", "name": "CAD Project"},
            },
        )
        step = b"""ISO-10303-21;
HEADER;
FILE_SCHEMA(('AUTOMOTIVE_DESIGN_CC2'));
ENDSEC;
DATA;
#1=PRODUCT('ARM','Arm','',(#2));
#20=CARTESIAN_POINT('',(0.,0.,0.));
#21=CARTESIAN_POINT('',(10.,20.,30.));
ENDSEC;
END-ISO-10303-21;
"""
        result = self.gateway.call_tool(
            "forge_inspect_cad_import",
            {
                "project_id": "cad-project",
                "asset_id": "arm-step",
                "filename": "arm.step",
                "source_uri": "attachment://arm.step",
                "source_version": "upload-1",
                "content_base64": base64.b64encode(step).decode("ascii"),
            },
        )
        self.assertEqual(result["inspection"]["kind"], "step_summary")
        self.assertFalse(result["persisted"])
        self.assertFalse(result["release_evidence"])

    def test_invalid_tool_calls_fail_closed(self) -> None:
        with self.assertRaisesRegex(KeyError, "unknown FORGE tool"):
            self.gateway.call_tool("forge_execute_robot_command", {})
        with self.assertRaisesRegex(ValueError, "accepts no arguments"):
            self.gateway.call_tool("forge_get_capabilities", {"unsafe": True})
        with self.assertRaisesRegex(ValueError, "invalid forge_create_project"):
            self.gateway.call_tool(
                "forge_create_project",
                {"request_id": "bad", "command": {"project_id": "missing-name"}},
            )
        for forbidden in (
            "forge_bind_simulation_result",
            "forge_ingest_release_evidence",
            "forge_evaluate_release",
        ):
            with self.assertRaisesRegex(KeyError, "unknown FORGE tool"):
                self.gateway.call_tool(forbidden, {})


if __name__ == "__main__":
    unittest.main()
