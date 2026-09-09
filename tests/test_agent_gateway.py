from __future__ import annotations

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
            service, integration_hub=IntegrationHub(), installation_id="test-agent"
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
        self.assertNotIn("forge_run_conversation", by_name)

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


if __name__ == "__main__":
    unittest.main()
