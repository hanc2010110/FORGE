from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import Field, field_validator, model_validator

from forge_core.models import ContractModel

_PROVIDER_ID = re.compile(r"[a-z][a-z0-9_]{1,63}")


class IntegrationCategory(StrEnum):
    SOURCE_CONTROL = "source_control"
    CAD_PLM = "cad_plm"
    CAE_SIMULATION = "cae_simulation"
    CI_TEST = "ci_test"
    BOM_SUPPLY = "bom_supply"
    AI_RAG = "ai_rag"
    DEVICE_LAB = "device_lab"


class IntegrationImplementation(StrEnum):
    LIVE = "live"
    LOCAL = "local"
    ADAPTER_REQUIRED = "adapter_required"


class IntegrationCredentialField(ContractModel):
    field_id: str
    label: str = Field(min_length=1, max_length=100)
    kind: Literal["text", "url", "secret", "file"]
    required: bool = True
    secret: bool = False

    @field_validator("field_id")
    @classmethod
    def field_id_must_be_safe(cls, value: str) -> str:
        if _PROVIDER_ID.fullmatch(value) is None:
            raise ValueError("credential field ID must be safe")
        return value

    @model_validator(mode="after")
    def secret_kind_must_not_be_mislabelled(self) -> IntegrationCredentialField:
        if self.kind in {"secret", "file"} and not self.secret:
            raise ValueError("secret and file credentials must be marked secret")
        return self


class IntegrationProvider(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    provider_id: str
    name: str = Field(min_length=1, max_length=100)
    category: IntegrationCategory
    implementation: IntegrationImplementation
    auth_strategy: str = Field(min_length=1, max_length=100)
    capabilities: tuple[str, ...]
    credential_fields: tuple[IntegrationCredentialField, ...] = ()
    read_only: Literal[True] = True
    requires_edge_agent: bool = False

    @field_validator("provider_id")
    @classmethod
    def provider_id_must_be_safe(cls, value: str) -> str:
        if _PROVIDER_ID.fullmatch(value) is None:
            raise ValueError("provider ID must be safe")
        return value

    @field_validator("capabilities")
    @classmethod
    def capabilities_must_be_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or value != tuple(sorted(set(value))):
            raise ValueError("capabilities must be unique and sorted")
        return value

    @model_validator(mode="after")
    def credential_fields_must_be_unique(self) -> IntegrationProvider:
        ids = [item.field_id for item in self.credential_fields]
        if len(ids) != len(set(ids)):
            raise ValueError("credential fields must be unique")
        return self


ConnectionState = Literal[
    "connected", "not_connected", "local_available", "adapter_required", "error"
]


class IntegrationHubEntry(ContractModel):
    provider: IntegrationProvider
    connection_state: ConnectionState
    status_summary: str = Field(min_length=1, max_length=300)


class IntegrationStatusProvider(Protocol):
    def status(self) -> Mapping[str, object]: ...


def _field(
    field_id: str,
    label: str,
    kind: Literal["text", "url", "secret", "file"],
    *,
    secret: bool = False,
) -> IntegrationCredentialField:
    return IntegrationCredentialField(
        field_id=field_id, label=label, kind=kind, secret=secret
    )


_API_KEY = (_field("api_key", "API key", "secret", secret=True),)
_OAUTH = (_field("tenant_url", "Tenant URL", "url"),) + _API_KEY
_BEARER = (
    _field("base_url", "Base URL", "url"),
    _field("access_token", "OAuth access token", "secret", secret=True),
)
_EDGE = (
    _field("endpoint", "Edge agent endpoint", "url"),
    _field("client_certificate", "Client certificate", "file", secret=True),
)


PROVIDERS = (
    IntegrationProvider(
        provider_id="local_artifacts",
        name="Local CAD / evidence files",
        category=IntegrationCategory.CAD_PLM,
        implementation=IntegrationImplementation.LOCAL,
        auth_strategy="Local file picker",
        capabilities=(
            "documents.read",
            "solidworks_exports.read",
            "step.read",
            "stl.read",
            "test_results.read",
        ),
    ),
    IntegrationProvider(
        provider_id="llm_host_mcp",
        name="LLM host / MCP bridge",
        category=IntegrationCategory.AI_RAG,
        implementation=IntegrationImplementation.LOCAL,
        auth_strategy="Local stdio or localhost HTTP MCP",
        capabilities=("embeddings.read", "tools.call", "tools.discover"),
    ),
    IntegrationProvider(
        provider_id="openai_hosted",
        name="OpenAI hosted reasoning + embeddings",
        category=IntegrationCategory.AI_RAG,
        implementation=IntegrationImplementation.LIVE,
        auth_strategy="API key",
        capabilities=("embeddings.read", "recommendations.read"),
        credential_fields=(
            _field("base_url", "API base URL", "url"),
            _field("api_key", "API key", "secret", secret=True),
            _field("model", "Model ID", "text"),
        ),
    ),
    IntegrationProvider(
        provider_id="manual_bom_quote",
        name="Manual BOM quote evidence",
        category=IntegrationCategory.BOM_SUPPLY,
        implementation=IntegrationImplementation.LOCAL,
        auth_strategy="Versioned file or operator import",
        capabilities=("prices.read", "quotes.read"),
    ),
    IntegrationProvider(
        provider_id="github",
        name="GitHub + Actions",
        category=IntegrationCategory.SOURCE_CONTROL,
        implementation=IntegrationImplementation.LIVE,
        auth_strategy="GitHub App installation token",
        capabilities=("actions.read", "commits.read", "pull_requests.read"),
        credential_fields=(
            _field("app_id", "App ID", "text"),
            _field("installation_id", "Installation ID", "text"),
            _field("owner", "Repository owner", "text"),
            _field("repository", "Repository name", "text"),
            _field("private_key", "Private key", "file", secret=True),
        ),
    ),
    IntegrationProvider(
        provider_id="gitlab",
        name="GitLab + CI",
        category=IntegrationCategory.SOURCE_CONTROL,
        implementation=IntegrationImplementation.LIVE,
        auth_strategy="OAuth or project access token",
        capabilities=("commits.read", "merge_requests.read", "pipelines.read"),
        credential_fields=(
            _field("base_url", "GitLab base URL", "url"),
            _field("project_path", "Project path", "text"),
            _field("api_key", "Read API token", "secret", secret=True),
        ),
    ),
    IntegrationProvider(
        provider_id="onshape",
        name="Onshape",
        category=IntegrationCategory.CAD_PLM,
        implementation=IntegrationImplementation.LIVE,
        auth_strategy="OAuth 2.0 bearer token",
        capabilities=("assemblies.read", "documents.read", "revisions.read"),
        credential_fields=(
            _field("base_url", "Onshape base URL", "url"),
            _field("access_token", "OAuth access token", "secret", secret=True),
        ),
    ),
    IntegrationProvider(
        provider_id="autodesk_aps",
        name="Autodesk Platform Services",
        category=IntegrationCategory.CAD_PLM,
        implementation=IntegrationImplementation.LIVE,
        auth_strategy="OAuth 2.0 service account",
        capabilities=("derivatives.read", "models.read", "versions.read"),
        credential_fields=_BEARER,
    ),
    IntegrationProvider(
        provider_id="solidworks_3dexperience",
        name="SOLIDWORKS 3DEXPERIENCE",
        category=IntegrationCategory.CAD_PLM,
        implementation=IntegrationImplementation.ADAPTER_REQUIRED,
        auth_strategy="3DEXPERIENCE OAuth",
        capabilities=("bom.read", "models.read", "revisions.read"),
        credential_fields=_OAUTH,
    ),
    IntegrationProvider(
        provider_id="teamcenter",
        name="Siemens Teamcenter",
        category=IntegrationCategory.CAD_PLM,
        implementation=IntegrationImplementation.ADAPTER_REQUIRED,
        auth_strategy="Teamcenter REST/OAuth",
        capabilities=("bom.read", "change_orders.read", "revisions.read"),
        credential_fields=_OAUTH,
    ),
    IntegrationProvider(
        provider_id="windchill",
        name="PTC Windchill",
        category=IntegrationCategory.CAD_PLM,
        implementation=IntegrationImplementation.LIVE,
        auth_strategy="Windchill REST/OAuth",
        capabilities=("bom.read", "change_notices.read", "revisions.read"),
        credential_fields=_BEARER,
    ),
    IntegrationProvider(
        provider_id="aras_innovator",
        name="Aras Innovator",
        category=IntegrationCategory.CAD_PLM,
        implementation=IntegrationImplementation.LIVE,
        auth_strategy="OAuth 2.0",
        capabilities=("bom.read", "changes.read", "items.read"),
        credential_fields=_BEARER,
    ),
    IntegrationProvider(
        provider_id="ansys",
        name="Ansys Minerva/Mechanical",
        category=IntegrationCategory.CAE_SIMULATION,
        implementation=IntegrationImplementation.ADAPTER_REQUIRED,
        auth_strategy="Enterprise API credential",
        capabilities=("jobs.read", "models.read", "results.read"),
        credential_fields=_OAUTH,
    ),
    IntegrationProvider(
        provider_id="simscale",
        name="SimScale",
        category=IntegrationCategory.CAE_SIMULATION,
        implementation=IntegrationImplementation.LIVE,
        auth_strategy="API key",
        capabilities=("jobs.read", "projects.read", "results.read"),
        credential_fields=(
            _field("base_url", "SimScale API base URL", "url"),
            _field("api_key", "API key", "secret", secret=True),
        ),
    ),
    IntegrationProvider(
        provider_id="matlab_simulink",
        name="MATLAB / Simulink",
        category=IntegrationCategory.CAE_SIMULATION,
        implementation=IntegrationImplementation.LIVE,
        auth_strategy="MATLAB Production Server bearer token",
        capabilities=("models.read", "results.read", "runs.read"),
        credential_fields=(
            _field("base_url", "MATLAB Production Server URL", "url"),
            _field("access_token", "Bearer token", "secret", secret=True),
            _field("application", "Deployed application", "text"),
        ),
    ),
    IntegrationProvider(
        provider_id="jenkins",
        name="Jenkins",
        category=IntegrationCategory.CI_TEST,
        implementation=IntegrationImplementation.LIVE,
        auth_strategy="API token",
        capabilities=("artifacts.read", "builds.read", "tests.read"),
        credential_fields=(
            _field("base_url", "Jenkins base URL", "url"),
            _field("job_path", "Job path", "text"),
            _field("username", "Username", "text"),
            _field("api_key", "API token", "secret", secret=True),
        ),
    ),
    IntegrationProvider(
        provider_id="digikey",
        name="DigiKey",
        category=IntegrationCategory.BOM_SUPPLY,
        implementation=IntegrationImplementation.ADAPTER_REQUIRED,
        auth_strategy="OAuth 2.0 client credentials",
        capabilities=("availability.read", "parts.read", "prices.read"),
        credential_fields=_API_KEY,
    ),
    IntegrationProvider(
        provider_id="mouser",
        name="Mouser",
        category=IntegrationCategory.BOM_SUPPLY,
        implementation=IntegrationImplementation.ADAPTER_REQUIRED,
        auth_strategy="API key",
        capabilities=("availability.read", "parts.read", "prices.read"),
        credential_fields=_API_KEY,
    ),
    IntegrationProvider(
        provider_id="ros2_edge",
        name="ROS 2 edge agent",
        category=IntegrationCategory.DEVICE_LAB,
        implementation=IntegrationImplementation.ADAPTER_REQUIRED,
        auth_strategy="Mutual TLS edge agent",
        capabilities=("bags.read", "parameters.read", "telemetry.read"),
        credential_fields=_EDGE,
        requires_edge_agent=True,
    ),
    IntegrationProvider(
        provider_id="mqtt_device",
        name="MQTT device gateway",
        category=IntegrationCategory.DEVICE_LAB,
        implementation=IntegrationImplementation.ADAPTER_REQUIRED,
        auth_strategy="Mutual TLS",
        capabilities=("evidence.read", "telemetry.read", "topics.read"),
        credential_fields=_EDGE,
        requires_edge_agent=True,
    ),
    IntegrationProvider(
        provider_id="opcua_lab",
        name="OPC UA bench gateway",
        category=IntegrationCategory.DEVICE_LAB,
        implementation=IntegrationImplementation.ADAPTER_REQUIRED,
        auth_strategy="Mutual TLS",
        capabilities=("bench_results.read", "nodes.read", "telemetry.read"),
        credential_fields=_EDGE,
        requires_edge_agent=True,
    ),
    IntegrationProvider(
        provider_id="hil_agent",
        name="Bench / HIL evidence agent",
        category=IntegrationCategory.DEVICE_LAB,
        implementation=IntegrationImplementation.LOCAL,
        auth_strategy="Signed local agent",
        capabilities=("artifacts.read", "runs.read", "tests.read"),
        credential_fields=_EDGE,
        requires_edge_agent=True,
    ),
)


class IntegrationHub:
    def __init__(
        self,
        providers: tuple[IntegrationProvider, ...] = PROVIDERS,
        status_providers: Mapping[str, IntegrationStatusProvider] | None = None,
    ) -> None:
        ids = [item.provider_id for item in providers]
        if len(ids) != len(set(ids)):
            raise ValueError("integration provider IDs must be unique")
        self._providers = providers
        self._status_providers = dict(status_providers or {})
        if not set(self._status_providers).issubset(ids):
            raise ValueError("status provider is not present in the catalog")

    def catalog(self) -> list[dict[str, object]]:
        entries: list[IntegrationHubEntry] = []
        for provider in self._providers:
            status_provider = self._status_providers.get(provider.provider_id)
            state: ConnectionState
            if status_provider is not None:
                try:
                    configured = status_provider.status().get("configured") is True
                    state = "connected" if configured else "not_connected"
                    summary = "Connected" if configured else "Ready to connect"
                except Exception:
                    state = "error"
                    summary = "Driver status unavailable"
            elif provider.implementation is IntegrationImplementation.LOCAL:
                state = "local_available"
                summary = "Available locally"
            elif provider.implementation is IntegrationImplementation.LIVE:
                state = "not_connected"
                summary = "Ready to connect"
            else:
                state = "adapter_required"
                summary = "Adapter required"
            entries.append(
                IntegrationHubEntry(
                    provider=provider,
                    connection_state=state,
                    status_summary=summary,
                )
            )
        return [item.model_dump(mode="json") for item in entries]


__all__ = [
    "IntegrationCategory",
    "IntegrationCredentialField",
    "IntegrationHub",
    "IntegrationHubEntry",
    "IntegrationImplementation",
    "IntegrationProvider",
    "IntegrationStatusProvider",
    "PROVIDERS",
]
