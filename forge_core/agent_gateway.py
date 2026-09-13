from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import ValidationError

from forge_core.access_control import LOCAL_ORG_ID
from forge_core.cad_imports import (
    import_cad_payload,
    import_solidworks_export_package,
)
from forge_core.integration_hub import IntegrationHub
from forge_core.models import ContractModel
from forge_core.release_service import (
    AnalyzeChangeCommand,
    AppendDesignTransitionCommand,
    CaptureSnapshotCommand,
    ConfirmDesignCandidateCommand,
    CreateChangePreviewCommand,
    CreateDesignProposalCommand,
    CreateExternalEvidencePlanCommand,
    CreateProjectCommand,
    CreateReleaseDiagnosisCommand,
    CreateResolutionPlanCommand,
    IngestCADGeometryCommand,
    IngestKnowledgeSourceCommand,
    MutationContext,
    RecordConversationalClaimCommand,
    ReleaseIntegrationService,
    ServiceMutationResult,
    VerifyExternalEvidencePlanCommand,
    VerifyPlanCommand,
)

ApprovalMode = Literal["never", "always"]


@dataclass(frozen=True, slots=True)
class _MutationTool:
    name: str
    title: str
    description: str
    command_type: type[ContractModel]
    service_method: str
    creates_project: bool = False


_MUTATION_TOOLS = (
    _MutationTool(
        "forge_create_project",
        "Create FORGE project",
        "Create the evidence ledger and local authorization records for a project.",
        CreateProjectCommand,
        "create_project",
        creates_project=True,
    ),
    _MutationTool(
        "forge_ingest_text_source",
        "Attach grounded text source",
        "Store a versioned document, datasheet, BOM export, or requirement source.",
        IngestKnowledgeSourceCommand,
        "ingest_knowledge_source",
    ),
    _MutationTool(
        "forge_ingest_stl_asset",
        "Attach STL geometry",
        "Parse and store a bounded ASCII or binary STL asset with immutable hashes.",
        IngestCADGeometryCommand,
        "ingest_cad_geometry",
    ),
    _MutationTool(
        "forge_capture_snapshot",
        "Capture engineering snapshot",
        "Capture read-only connector data into one immutable project snapshot.",
        CaptureSnapshotCommand,
        "capture_snapshot",
    ),
    _MutationTool(
        "forge_analyze_change",
        "Analyze hardware change",
        "Compare two captured snapshots and calculate affected artifacts and retests.",
        AnalyzeChangeCommand,
        "analyze_change",
    ),
    _MutationTool(
        "forge_create_change_preview",
        "Preview proposed change",
        "Create a planning-only impact preview from a proposed component change.",
        CreateChangePreviewCommand,
        "create_change_preview",
    ),
    _MutationTool(
        "forge_create_design_proposal",
        "Generate design alternatives",
        "Generate evidence-linked alternatives from a stored change preview.",
        CreateDesignProposalCommand,
        "create_design_proposal",
    ),
    _MutationTool(
        "forge_confirm_design_candidate",
        "Confirm design candidate",
        "Consume a backend-issued operator approval and freeze its exact candidate; "
        "the agent cannot issue that approval itself.",
        ConfirmDesignCandidateCommand,
        "confirm_design_candidate",
    ),
    _MutationTool(
        "forge_record_evidence_claim",
        "Record evidence-backed claim",
        "Record a conversational claim with exact evidence references and confidence.",
        RecordConversationalClaimCommand,
        "record_conversational_claim",
    ),
    _MutationTool(
        "forge_append_design_transition",
        "Advance design workflow",
        "Append an auditable TALK, CONFIRM, SIMULATE, REVISE, or VERIFY transition.",
        AppendDesignTransitionCommand,
        "append_design_transition",
    ),
    _MutationTool(
        "forge_create_external_evidence_plan",
        "Plan external simulation or test",
        "Specify the solver, bench, HIL, or device evidence required for a scenario.",
        CreateExternalEvidencePlanCommand,
        "create_external_evidence_plan",
    ),
    _MutationTool(
        "forge_verify_plan",
        "Verify plan against actual change",
        "Compare a planning preview with the actual captured change.",
        VerifyPlanCommand,
        "verify_plan",
    ),
    _MutationTool(
        "forge_verify_external_evidence",
        "Verify external evidence",
        "Check imported simulation or test evidence against the required "
        "evidence plan.",
        VerifyExternalEvidencePlanCommand,
        "verify_external_evidence_plan",
    ),
    _MutationTool(
        "forge_diagnose_release",
        "Diagnose blocked release",
        "Create evidence-linked blocker diagnoses for a stored release decision.",
        CreateReleaseDiagnosisCommand,
        "create_release_diagnosis",
    ),
    _MutationTool(
        "forge_create_resolution_plan",
        "Create resolution plan",
        "Create a planning-only repair plan from a selected verified fix proposal.",
        CreateResolutionPlanCommand,
        "create_resolution_plan",
    ),
)


def _empty_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {}, "additionalProperties": False}


def _project_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "minLength": 1},
        },
        "required": ["project_id"],
        "additionalProperties": False,
    }


def _cad_inspection_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "minLength": 1},
            "asset_id": {"type": "string", "minLength": 1},
            "filename": {"type": "string", "minLength": 1},
            "source_uri": {"type": "string", "minLength": 1},
            "source_version": {"type": "string", "minLength": 1},
            "content_base64": {"type": "string", "minLength": 1},
        },
        "required": [
            "project_id",
            "asset_id",
            "filename",
            "source_uri",
            "source_version",
            "content_base64",
        ],
        "additionalProperties": False,
    }


def _mutation_schema(spec: _MutationTool) -> dict[str, Any]:
    command_schema = spec.command_type.model_json_schema()
    definitions = command_schema.pop("$defs", {})
    properties: dict[str, Any] = {
        "request_id": {
            "type": "string",
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
            "description": "Stable idempotency key for this exact request.",
        },
        "command": command_schema,
    }
    required = ["request_id", "command"]
    if not spec.creates_project:
        properties["project_id"] = {"type": "string", "minLength": 1}
        required.insert(0, "project_id")
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    if definitions:
        schema["$defs"] = definitions
    return schema


class ForgeAgentGateway:
    """Host-neutral, approval-gated tool surface over the FORGE backend."""

    def __init__(
        self,
        service: ReleaseIntegrationService,
        *,
        integration_hub: IntegrationHub | None = None,
        installation_id: str = "forge-agent-local",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._service = service
        self._integration_hub = integration_hub or IntegrationHub()
        self._installation_id = installation_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._mutations = {item.name: item for item in _MUTATION_TOOLS}

    def list_tools(self) -> list[dict[str, Any]]:
        read_tools = [
            self._read_tool(
                "forge_get_capabilities",
                "Get FORGE capabilities",
                "Describe the supported engineering workflow and authority boundaries.",
                _empty_schema(),
            ),
            self._read_tool(
                "forge_list_integrations",
                "List engineering integrations",
                "List real connection states; catalog presence never implies "
                "connection.",
                _empty_schema(),
            ),
            self._read_tool(
                "forge_get_project_context",
                "Get project engineering context",
                "Read the latest evidence, candidate, simulation, and release state.",
                _project_schema(),
            ),
            self._read_tool(
                "forge_inspect_cad_import",
                "Inspect STEP, STL, or SolidWorks export",
                "Parse a bounded attached CAD payload and return source-bound "
                "geometry metadata without writing CAD or release evidence.",
                _cad_inspection_schema(),
            ),
        ]
        mutation_tools = [
            {
                "name": spec.name,
                "title": spec.title,
                "description": spec.description,
                "inputSchema": _mutation_schema(spec),
                "annotations": {
                    "title": spec.title,
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "_meta": {
                    "forge/approvalRequired": True,
                    "forge/authority": "backend-only",
                },
            }
            for spec in _MUTATION_TOOLS
        ]
        return read_tools + mutation_tools

    @staticmethod
    def _read_tool(
        name: str, title: str, description: str, input_schema: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "name": name,
            "title": title,
            "description": description,
            "inputSchema": input_schema,
            "annotations": {
                "title": title,
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            "_meta": {
                "forge/approvalRequired": False,
                "forge/authority": "read-only",
            },
        }

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "forge_get_capabilities":
            self._reject_arguments(arguments)
            return self._capabilities()
        if name == "forge_list_integrations":
            self._reject_arguments(arguments)
            return {"integrations": self._integration_hub.catalog()}
        if name == "forge_get_project_context":
            return self._project_context(self._required_text(arguments, "project_id"))
        if name == "forge_inspect_cad_import":
            return self._inspect_cad_import(arguments)
        spec = self._mutations.get(name)
        if spec is None:
            raise KeyError(f"unknown FORGE tool: {name}")
        return self._call_mutation(spec, arguments)

    @staticmethod
    def _reject_arguments(arguments: dict[str, Any]) -> None:
        if arguments:
            raise ValueError("this tool accepts no arguments")

    @staticmethod
    def _required_text(arguments: dict[str, Any], key: str) -> str:
        value = arguments.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{key} must be a non-empty string")
        return value

    def _call_mutation(
        self, spec: _MutationTool, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        request_id = self._required_text(arguments, "request_id")
        raw_command = arguments.get("command")
        if not isinstance(raw_command, dict):
            raise ValueError("command must be an object")
        try:
            command = spec.command_type.model_validate(raw_command)
        except ValidationError as exc:
            raise ValueError(f"invalid {spec.name} command: {exc}") from exc
        method = getattr(self._service, spec.service_method)
        if spec.creates_project:
            context = MutationContext(
                local_installation_id=self._installation_id,
                idempotency_key=request_id,
            )
            result = method(command, context)
        else:
            project_id = self._required_text(arguments, "project_id")
            project = self._service.get_project(project_id)
            context = MutationContext(
                local_installation_id=self._installation_id,
                idempotency_key=request_id,
                expected_project_version=int(project["version"]),
            )
            result = method(project_id, command, context)
        if not isinstance(result, ServiceMutationResult):
            raise TypeError("FORGE mutation returned an invalid result")
        return result.payload | {
            "project_version": result.project_version,
            "replayed": result.replayed,
        }

    def _project_context(self, project_id: str) -> dict[str, Any]:
        self._service.authorize_project_read(project_id)
        collections = {
            "knowledge_sources": self._service.list_knowledge_sources(project_id),
            "cad_assets": self._service.list_cad_geometries(project_id),
            "change_previews": self._service.list_change_previews(project_id),
            "design_proposals": self._service.list_design_proposals(project_id),
            "design_candidates": self._service.list_design_candidates(project_id),
            "simulations": self._service.list_simulation_bindings(project_id),
            "claims": self._service.list_conversational_claims(project_id),
            "transitions": self._service.list_design_transitions(project_id),
            "plan_verifications": self._service.list_plan_verifications(project_id),
            "external_evidence_plans": self._service.list_external_evidence_plans(
                project_id
            ),
            "external_evidence_verifications": (
                self._service.list_external_evidence_plan_verifications(project_id)
            ),
            "release_evidence": self._service.list_release_evidence(project_id),
            "release_decisions": self._service.list_release_decisions(project_id),
            "release_diagnoses": self._service.list_release_diagnoses(project_id),
            "resolution_plans": self._service.list_resolution_plans(project_id),
        }
        return {
            "project": self._service.get_project(project_id),
            "counts": {key: len(value) for key, value in collections.items()},
            "latest": {
                key: value[-1] if value else None for key, value in collections.items()
            },
            "authority": {
                "llm_may_explain": True,
                "llm_may_calculate_physical_results": False,
                "llm_may_decide_release": False,
                "release_verdict_source": "operator_or_trusted_backend_api",
            },
        }

    def _inspect_cad_import(self, arguments: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "project_id",
            "asset_id",
            "filename",
            "source_uri",
            "source_version",
            "content_base64",
        }
        if set(arguments) != allowed:
            raise ValueError("CAD inspection arguments are missing or unknown")
        project_id = self._required_text(arguments, "project_id")
        self._service.authorize_project_read(project_id)
        encoded = self._required_text(arguments, "content_base64")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("content_base64 must be valid base64") from exc
        asset_id = self._required_text(arguments, "asset_id")
        source_uri = self._required_text(arguments, "source_uri")
        source_version = self._required_text(arguments, "source_version")
        captured_at = self._clock()
        filename = self._required_text(arguments, "filename")
        if PurePosixPath(filename).suffix.casefold() == ".zip":
            inspection = import_solidworks_export_package(
                asset_id=asset_id,
                project_id=project_id,
                tenant_id=LOCAL_ORG_ID,
                source_uri=source_uri,
                source_version=source_version,
                captured_at=captured_at,
                content=content,
            ).model_dump(mode="json")
        else:
            inspection = import_cad_payload(
                asset_id=asset_id,
                project_id=project_id,
                tenant_id=LOCAL_ORG_ID,
                source_uri=source_uri,
                source_version=source_version,
                captured_at=captured_at,
                filename=filename,
                content=content,
            ).model_dump(mode="json")
        return {
            "inspection": inspection,
            "authority": "read_only_geometry_inspection",
            "persisted": False,
            "release_evidence": False,
        }

    @staticmethod
    def _capabilities() -> dict[str, Any]:
        return {
            "product": "FORGE Engineering Agent",
            "primary_ux": "LLM host conversation",
            "backend": "independent evidence and policy service",
            "workflow": [
                "ATTACH_CONTEXT",
                "DISCUSS_CHANGE",
                "PLAN",
                "CONFIRM_CANDIDATE",
                "SIMULATE_OR_TEST",
                "INTERPRET",
                "REVISE_OR_ACCEPT",
                "VERIFY_RELEASE",
            ],
            "hard_boundaries": [
                "The LLM explains and orchestrates but does not invent numeric "
                "results.",
                "Only a user-confirmed candidate may receive simulation evidence.",
                "Simulation, bench, HIL, and physical-device evidence remain distinct.",
                "AI BOM prices are estimates, never release evidence.",
                "Only FORGE policy evaluation may return READY or BLOCKED.",
                "The LLM tool surface cannot ingest raw release evidence, bind "
                "simulation results, or invoke release evaluation.",
                "No generic device-control or CAD write-back tool is exposed.",
            ],
        }


__all__ = ["ForgeAgentGateway"]
