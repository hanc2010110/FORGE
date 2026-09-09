from __future__ import annotations

import unittest
from typing import Any, cast

from pydantic import ValidationError

from forge_core.integration_hub import (
    PROVIDERS,
    IntegrationCategory,
    IntegrationCredentialField,
    IntegrationHub,
    IntegrationImplementation,
    IntegrationProvider,
)


class StatusProvider:
    def __init__(self, configured: bool) -> None:
        self.configured = configured

    def status(self) -> dict[str, object]:
        return {"configured": self.configured}


class BrokenStatusProvider:
    def status(self) -> dict[str, object]:
        raise RuntimeError("offline")


class IntegrationHubTests(unittest.TestCase):
    def test_catalog_covers_every_future_engineering_integration_lane(self) -> None:
        hub = IntegrationHub(status_providers={"github": StatusProvider(True)})
        catalog = cast(list[dict[str, Any]], hub.catalog())
        provider_ids = {item["provider"]["provider_id"] for item in catalog}
        self.assertTrue(
            {
                "github",
                "gitlab",
                "local_artifacts",
                "onshape",
                "autodesk_aps",
                "solidworks_3dexperience",
                "teamcenter",
                "windchill",
                "aras_innovator",
                "ansys",
                "simscale",
                "matlab_simulink",
                "jenkins",
                "digikey",
                "mouser",
                "llm_host_mcp",
                "openai_hosted",
                "manual_bom_quote",
                "ros2_edge",
                "mqtt_device",
                "opcua_lab",
                "hil_agent",
            }.issubset(provider_ids)
        )
        github = next(
            item for item in catalog if item["provider"]["provider_id"] == "github"
        )
        self.assertEqual(github["connection_state"], "connected")
        local = next(
            item
            for item in catalog
            if item["provider"]["provider_id"] == "local_artifacts"
        )
        self.assertEqual(local["connection_state"], "local_available")
        llm_host = next(
            item
            for item in catalog
            if item["provider"]["provider_id"] == "llm_host_mcp"
        )
        self.assertEqual(llm_host["connection_state"], "local_available")
        self.assertNotIn(
            "nexar_octopart",
            provider_ids,
            "one aggregator must not be a mandatory product dependency",
        )
        credential_ready = [
            item
            for item in catalog
            if item["provider"]["provider_id"]
            in {
                "gitlab",
                "jenkins",
                "onshape",
                "autodesk_aps",
                "windchill",
                "aras_innovator",
                "simscale",
                "matlab_simulink",
                "openai_hosted",
            }
        ]
        self.assertTrue(
            all(
                item["connection_state"] == "not_connected" for item in credential_ready
            )
        )
        unimplemented = [
            item
            for item in catalog
            if item["provider"]["implementation"] == "adapter_required"
        ]
        self.assertTrue(unimplemented)
        self.assertTrue(
            all(
                item["connection_state"] == "adapter_required" for item in unimplemented
            )
        )
        self.assertTrue(all(item["provider"]["read_only"] for item in catalog))

    def test_status_errors_are_isolated_and_unknown_drivers_are_rejected(self) -> None:
        catalog = cast(
            list[dict[str, Any]],
            IntegrationHub(
                status_providers={"github": BrokenStatusProvider()}
            ).catalog(),
        )
        github = next(
            item for item in catalog if item["provider"]["provider_id"] == "github"
        )
        self.assertEqual(github["connection_state"], "error")
        with self.assertRaises(ValueError):
            IntegrationHub(status_providers={"unknown": StatusProvider(False)})
        with self.assertRaises(ValueError):
            IntegrationHub(providers=(PROVIDERS[0], PROVIDERS[0]))

    def test_provider_contracts_reject_unsafe_or_mislabelled_credentials(self) -> None:
        with self.assertRaises(ValidationError):
            IntegrationCredentialField(
                field_id="api/key", label="API key", kind="secret", secret=True
            )
        with self.assertRaises(ValidationError):
            IntegrationCredentialField(
                field_id="api_key", label="API key", kind="secret", secret=False
            )
        with self.assertRaises(ValidationError):
            IntegrationProvider(
                provider_id="bad/provider",
                name="Bad",
                category=IntegrationCategory.SOURCE_CONTROL,
                implementation=IntegrationImplementation.ADAPTER_REQUIRED,
                auth_strategy="none",
                capabilities=("read",),
            )
        with self.assertRaises(ValidationError):
            PROVIDERS[0].model_copy(
                update={"capabilities": ("stl.read", "stl.read")}
            ).model_validate(
                PROVIDERS[0]
                .model_copy(update={"capabilities": ("stl.read", "stl.read")})
                .model_dump()
            )


if __name__ == "__main__":
    unittest.main()
