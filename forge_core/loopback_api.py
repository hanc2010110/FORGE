from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

from pydantic import ValidationError

from forge_core.access_control import (
    LOCAL_ACTOR_ID,
    LOCAL_ORG_ID,
    AuthorizationDeniedError,
)
from forge_core.dashboard import (
    DASHBOARD_CONTENT_SECURITY_POLICY,
    is_dashboard_target,
    load_dashboard_asset,
)
from forge_core.persistence import (
    CorruptRecordError,
    IdempotencyConflictError,
    IntegrityConflictError,
    RecordNotFoundError,
    StorageBusyError,
    VersionConflictError,
)
from forge_core.release_service import (
    AnalyzeChangeCommand,
    AppendDesignTransitionCommand,
    BindDesignSimulationCommand,
    CaptureSnapshotCommand,
    ConfirmDesignCandidateCommand,
    CreateChangePreviewCommand,
    CreateDesignProposalCommand,
    CreateExternalEvidencePlanCommand,
    CreateProjectCommand,
    CreateReleaseDiagnosisCommand,
    CreateResolutionPlanCommand,
    EvaluateReleaseCommand,
    IngestCADGeometryCommand,
    IngestKnowledgeSourceCommand,
    IngestReleaseEvidenceCommand,
    MutationContext,
    RecordConversationalClaimCommand,
    ReleaseIntegrationService,
    RunConversationCommand,
    VerifyExternalEvidencePlanCommand,
    VerifyPlanCommand,
)

MAX_REQUEST_BYTES = 1_048_576
_SAFE_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
_PROJECT_ROUTE = re.compile(rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})$")
_AUDIT_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/audit-events$"
)
_KNOWLEDGE_SOURCE_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/knowledge-sources$"
)
_KNOWLEDGE_SOURCE_ITEM_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/knowledge-sources/"
    rf"(?P<item>{_SAFE_SEGMENT})$"
)
_CONVERSATION_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/conversations$"
)
_CONVERSATION_ITEM_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/conversations/"
    rf"(?P<item>{_SAFE_SEGMENT})$"
)
_CAD_GEOMETRY_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/cad-geometries$"
)
_CAD_GEOMETRY_ITEM_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/cad-geometries/"
    rf"(?P<item>{_SAFE_SEGMENT})$"
)
_SNAPSHOT_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/connector-snapshots$"
)
_SNAPSHOT_ITEM_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/connector-snapshots/"
    rf"(?P<item>{_SAFE_SEGMENT})$"
)
_IMPACT_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/change-impacts$"
)
_IMPACT_ITEM_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/change-impacts/"
    r"(?P<item>sha256:[0-9a-f]{64})$"
)
_PREVIEW_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/change-previews$"
)
_PREVIEW_ITEM_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/change-previews/"
    r"(?P<item>sha256:[0-9a-f]{64})$"
)
_EXTERNAL_EVIDENCE_PLAN_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/external-evidence-plans$"
)
_EXTERNAL_EVIDENCE_PLAN_ITEM_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/external-evidence-plans/"
    r"(?P<item>sha256:[0-9a-f]{64})$"
)
_EXTERNAL_EVIDENCE_PLAN_VERIFICATION_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/"
    r"external-evidence-plan-verifications$"
)
_EXTERNAL_EVIDENCE_PLAN_VERIFICATION_ITEM_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/"
    r"external-evidence-plan-verifications/"
    r"(?P<item>sha256:[0-9a-f]{64})$"
)
_PLAN_VERIFICATION_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/plan-verifications$"
)
_PLAN_VERIFICATION_ITEM_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/plan-verifications/"
    r"(?P<item>sha256:[0-9a-f]{64})$"
)
_DESIGN_PROPOSAL_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/design-proposals$"
)
_DESIGN_PROPOSAL_ITEM_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/design-proposals/"
    r"(?P<item>sha256:[0-9a-f]{64})$"
)
_DESIGN_CANDIDATE_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/design-candidates$"
)
_DESIGN_CANDIDATE_ITEM_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/design-candidates/"
    r"(?P<item>sha256:[0-9a-f]{64})$"
)
_DESIGN_SIMULATION_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/design-simulations$"
)
_DESIGN_SIMULATION_ITEM_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/design-simulations/"
    r"(?P<item>sha256:[0-9a-f]{64})$"
)
_CONVERSATIONAL_CLAIM_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/conversational-claims$"
)
_CONVERSATIONAL_CLAIM_ITEM_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/conversational-claims/"
    r"(?P<item>sha256:[0-9a-f]{64})$"
)
_DESIGN_TRANSITION_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/design-transitions$"
)
_RELEASE_DIAGNOSIS_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/release-diagnoses$"
)
_RELEASE_DIAGNOSIS_ITEM_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/release-diagnoses/"
    r"(?P<item>sha256:[0-9a-f]{64})$"
)
_RESOLUTION_PLAN_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/resolution-plans$"
)
_RESOLUTION_PLAN_ITEM_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/resolution-plans/"
    r"(?P<item>sha256:[0-9a-f]{64})$"
)
_EVIDENCE_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/release-evidence$"
)
_EVIDENCE_ITEM_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/release-evidence/"
    rf"(?P<item>{_SAFE_SEGMENT})$"
)
_DECISION_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/release-decisions$"
)
_DECISION_ITEM_ROUTE = re.compile(
    rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})/release-decisions/"
    r"(?P<item>sha256:[0-9a-f]{64})$"
)

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'none'"
    ),
    "Cross-Origin-Resource-Policy": "same-origin",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


class APIError(RuntimeError):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        headers: Mapping[str, str] | None = None,
        fields: Sequence[Mapping[str, str]] = (),
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.headers = dict(headers or {})
        self.fields = tuple(dict(item) for item in fields)


@dataclass(frozen=True, slots=True)
class APIResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.body))


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON number")


class LoopbackAPI:
    def __init__(
        self,
        service: ReleaseIntegrationService,
        *,
        port: int,
        csrf_secret: bytes,
        local_installation_id: str,
        nonce_factory: Callable[[], str] = lambda: secrets.token_hex(32),
        max_request_bytes: int = MAX_REQUEST_BYTES,
    ) -> None:
        if not csrf_secret:
            raise ValueError("CSRF secret cannot be empty")
        if not (0 < port <= 65535):
            raise ValueError("loopback API requires a bound TCP port")
        if re.fullmatch(_SAFE_SEGMENT, local_installation_id) is None:
            raise ValueError("local installation ID must be opaque and safe")
        if max_request_bytes <= 0:
            raise ValueError("request body limit must be positive")
        self._service = service
        self.port = port
        self.expected_host = f"127.0.0.1:{port}"
        self.expected_origin = f"http://{self.expected_host}"
        self._csrf_secret = csrf_secret
        self._local_installation_id = local_installation_id
        self._nonce_factory = nonce_factory
        self.max_request_bytes = max_request_bytes

    def _token(self) -> str:
        nonce = self._nonce_factory()
        if re.fullmatch(r"[0-9a-f]{64}", nonce) is None:
            raise RuntimeError("CSRF nonce factory returned an invalid nonce")
        signature = hmac.new(
            self._csrf_secret, nonce.encode("ascii"), hashlib.sha256
        ).hexdigest()
        return f"{nonce}.{signature}"

    def _valid_token(self, token: str) -> bool:
        try:
            nonce, signature = token.split(".", 1)
        except ValueError:
            return False
        if (
            re.fullmatch(r"[0-9a-f]{64}", nonce) is None
            or re.fullmatch(r"[0-9a-f]{64}", signature) is None
        ):
            return False
        expected = hmac.new(
            self._csrf_secret, nonce.encode("ascii"), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature, expected)

    @staticmethod
    def _header_values(
        headers: Sequence[tuple[str, str]], name: str
    ) -> tuple[str, ...]:
        lowered = name.lower()
        return tuple(value for key, value in headers if key.lower() == lowered)

    def _one_header(
        self,
        headers: Sequence[tuple[str, str]],
        name: str,
        *,
        required: bool = False,
    ) -> str | None:
        values = self._header_values(headers, name)
        if len(values) > 1:
            raise APIError(400, "invalid_headers", "Request headers are invalid.")
        if not values:
            if required:
                raise APIError(400, "missing_header", f"{name} is required.")
            return None
        return values[0]

    def _validate_target(self, target: str) -> None:
        if (
            not (target.startswith("/api/v1/") or is_dashboard_target(target))
            or "?" in target
            or "#" in target
            or "%" in target
            or "\\" in target
            or "\x00" in target
            or any(segment == ".." for segment in target.split("/"))
        ):
            raise APIError(400, "invalid_target", "Request target is invalid.")

    def _validate_host_origin(
        self,
        method: str,
        headers: Sequence[tuple[str, str]],
    ) -> None:
        host = self._one_header(headers, "Host", required=True)
        if host != self.expected_host:
            raise APIError(400, "invalid_host", "Host header is invalid.")
        origin = self._one_header(headers, "Origin")
        if origin is not None and origin != self.expected_origin:
            raise APIError(403, "origin_forbidden", "Origin is not allowed.")
        if method in {"POST", "PUT", "PATCH", "DELETE", "OPTIONS"} and origin is None:
            raise APIError(403, "origin_required", "Origin is required.")

    def _validate_csrf(self, headers: Sequence[tuple[str, str]]) -> None:
        header_token = self._one_header(headers, "X-FORGE-CSRF", required=True)
        cookies = self._header_values(headers, "Cookie")
        if len(cookies) != 1:
            raise APIError(403, "csrf_failed", "CSRF validation failed.")
        cookie_tokens = [
            part.split("=", 1)[1].strip()
            for part in cookies[0].split(";")
            if part.strip().startswith("forge_csrf=") and "=" in part
        ]
        if (
            len(cookie_tokens) != 1
            or header_token is None
            or not hmac.compare_digest(cookie_tokens[0], header_token)
            or not self._valid_token(header_token)
        ):
            raise APIError(403, "csrf_failed", "CSRF validation failed.")

    def _parse_json(
        self, headers: Sequence[tuple[str, str]], body: bytes
    ) -> dict[str, Any]:
        if self._one_header(headers, "Transfer-Encoding") is not None:
            raise APIError(400, "unsupported_framing", "Request framing is invalid.")
        length_text = self._one_header(headers, "Content-Length")
        if length_text is None:
            raise APIError(411, "length_required", "Content-Length is required.")
        if re.fullmatch(r"0|[1-9][0-9]*", length_text) is None:
            raise APIError(400, "invalid_length", "Content-Length is invalid.")
        length = int(length_text)
        if length > self.max_request_bytes:
            raise APIError(413, "request_too_large", "Request body is too large.")
        if len(body) != length:
            raise APIError(400, "truncated_body", "Request body length is invalid.")
        if self._one_header(headers, "Content-Type") != "application/json":
            raise APIError(
                415,
                "unsupported_media_type",
                "Content-Type must be application/json.",
            )
        try:
            decoded = body.decode("utf-8", errors="strict")
            value = json.loads(
                decoded,
                object_pairs_hook=_strict_object_pairs,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise APIError(
                400, "invalid_json", "Request body is invalid JSON."
            ) from exc
        if not isinstance(value, dict):
            raise APIError(400, "invalid_json", "JSON body must be an object.")
        return value

    def _mutation_context(
        self,
        headers: Sequence[tuple[str, str]],
        project_id: str,
        *,
        creating: bool = False,
    ) -> MutationContext:
        idempotency_key = self._one_header(headers, "Idempotency-Key", required=True)
        assert idempotency_key is not None
        if creating:
            expected_version = None
            if self._one_header(headers, "If-Match") is not None:
                raise APIError(400, "invalid_if_match", "If-Match is not allowed.")
        else:
            if_match = self._one_header(headers, "If-Match", required=True)
            match = re.fullmatch(
                rf'"project:{re.escape(project_id)}:v([1-9][0-9]*)"', if_match or ""
            )
            if match is None:
                raise APIError(400, "invalid_if_match", "If-Match is invalid.")
            expected_version = int(match.group(1))
        return MutationContext(
            local_installation_id=self._local_installation_id,
            idempotency_key=idempotency_key,
            expected_project_version=expected_version,
            org_id=self._identity(headers)[0],
            actor_id=self._identity(headers)[1],
        )

    def _identity(self, headers: Sequence[tuple[str, str]]) -> tuple[str, str]:
        org_id = self._one_header(headers, "X-FORGE-ORG-ID") or LOCAL_ORG_ID
        actor_id = self._one_header(headers, "X-FORGE-ACTOR-ID") or LOCAL_ACTOR_ID
        if (
            re.fullmatch(_SAFE_SEGMENT, org_id) is None
            or re.fullmatch(_SAFE_SEGMENT, actor_id) is None
        ):
            raise APIError(400, "invalid_identity", "Identity headers are invalid.")
        return org_id, actor_id

    @staticmethod
    def _validation_fields(exc: ValidationError) -> tuple[dict[str, str], ...]:
        return tuple(
            {
                "path": "/" + "/".join(str(item) for item in error["loc"]),
                "code": str(error["type"]),
            }
            for error in exc.errors(include_input=False, include_context=False)
        )

    @staticmethod
    def _route_match(target: str) -> tuple[str, re.Match[str] | None]:
        if target == "/api/v1/projects":
            return "project_collection", None
        routes = (
            ("project_item", _PROJECT_ROUTE),
            ("audit_collection", _AUDIT_ROUTE),
            ("knowledge_source_collection", _KNOWLEDGE_SOURCE_ROUTE),
            ("knowledge_source_item", _KNOWLEDGE_SOURCE_ITEM_ROUTE),
            ("conversation_collection", _CONVERSATION_ROUTE),
            ("conversation_item", _CONVERSATION_ITEM_ROUTE),
            ("cad_geometry_collection", _CAD_GEOMETRY_ROUTE),
            ("cad_geometry_item", _CAD_GEOMETRY_ITEM_ROUTE),
            ("snapshot_collection", _SNAPSHOT_ROUTE),
            ("snapshot_item", _SNAPSHOT_ITEM_ROUTE),
            ("impact_collection", _IMPACT_ROUTE),
            ("impact_item", _IMPACT_ITEM_ROUTE),
            ("preview_collection", _PREVIEW_ROUTE),
            ("preview_item", _PREVIEW_ITEM_ROUTE),
            ("external_evidence_plan_collection", _EXTERNAL_EVIDENCE_PLAN_ROUTE),
            ("external_evidence_plan_item", _EXTERNAL_EVIDENCE_PLAN_ITEM_ROUTE),
            (
                "external_evidence_plan_verification_collection",
                _EXTERNAL_EVIDENCE_PLAN_VERIFICATION_ROUTE,
            ),
            (
                "external_evidence_plan_verification_item",
                _EXTERNAL_EVIDENCE_PLAN_VERIFICATION_ITEM_ROUTE,
            ),
            ("plan_verification_collection", _PLAN_VERIFICATION_ROUTE),
            ("plan_verification_item", _PLAN_VERIFICATION_ITEM_ROUTE),
            ("design_proposal_collection", _DESIGN_PROPOSAL_ROUTE),
            ("design_proposal_item", _DESIGN_PROPOSAL_ITEM_ROUTE),
            ("design_candidate_collection", _DESIGN_CANDIDATE_ROUTE),
            ("design_candidate_item", _DESIGN_CANDIDATE_ITEM_ROUTE),
            ("design_simulation_collection", _DESIGN_SIMULATION_ROUTE),
            ("design_simulation_item", _DESIGN_SIMULATION_ITEM_ROUTE),
            ("conversational_claim_collection", _CONVERSATIONAL_CLAIM_ROUTE),
            ("conversational_claim_item", _CONVERSATIONAL_CLAIM_ITEM_ROUTE),
            ("design_transition_collection", _DESIGN_TRANSITION_ROUTE),
            ("release_diagnosis_collection", _RELEASE_DIAGNOSIS_ROUTE),
            ("release_diagnosis_item", _RELEASE_DIAGNOSIS_ITEM_ROUTE),
            ("resolution_plan_collection", _RESOLUTION_PLAN_ROUTE),
            ("resolution_plan_item", _RESOLUTION_PLAN_ITEM_ROUTE),
            ("evidence_collection", _EVIDENCE_ROUTE),
            ("evidence_item", _EVIDENCE_ITEM_ROUTE),
            ("decision_collection", _DECISION_ROUTE),
            ("decision_item", _DECISION_ITEM_ROUTE),
        )
        for name, pattern in routes:
            match = pattern.fullmatch(target)
            if match is not None:
                return name, match
        return "unknown", None

    def _dispatch(
        self,
        method: str,
        target: str,
        headers: Sequence[tuple[str, str]],
        body: bytes,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        if method == "GET" and target == "/api/v1/health":
            return 200, {"status": "ok", "api_version": "v1"}, {}
        if method == "GET" and target == "/api/v1/health/readiness":
            report = self._service.operational_health()
            status = 200 if report["status"] == "READY" else 503
            return status, {"data": report}, {}
        if method == "GET" and target == "/api/v1/session":
            token = self._token()
            return (
                200,
                {
                    "csrf_token": token,
                    "identity": {
                        "org_id": LOCAL_ORG_ID,
                        "actor_id": LOCAL_ACTOR_ID,
                        "security_mode": "local_bootstrap",
                    },
                },
                {
                    "Set-Cookie": (
                        f"forge_csrf={token}; HttpOnly; SameSite=Strict; Path=/api/v1"
                    )
                },
            )
        if method == "GET" and target == "/api/v1/connectors":
            return 200, {"data": self._service.list_connectors()}, {}
        route, match = self._route_match(target)
        if route == "unknown":
            raise APIError(404, "not_found", "Resource was not found.")
        if route == "project_collection":
            if method != "POST":
                raise APIError(
                    405,
                    "method_not_allowed",
                    "Method is not allowed.",
                    headers={"Allow": "POST"},
                )
            self._validate_csrf(headers)
            payload = self._parse_json(headers, body)
            command = CreateProjectCommand.model_validate(payload)
            context = self._mutation_context(headers, command.project_id, creating=True)
            result = self._service.create_project(command, context)
            return (
                result.status,
                result.payload,
                {
                    "ETag": (
                        f'"project:{command.project_id}:v{result.project_version}"'
                    ),
                    "Idempotency-Replayed": "true" if result.replayed else "false",
                },
            )
        assert match is not None
        project_id = match.group("project")
        item_id = match.groupdict().get("item")
        if method == "GET":
            org_id, actor_id = self._identity(headers)
            if route != "audit_collection":
                self._service.authorize_project_read(
                    project_id, org_id=org_id, actor_id=actor_id
                )
            data: Any
            if route == "project_item":
                data = self._service.get_project(project_id)
            elif route == "audit_collection":
                data = self._service.list_audit_events(
                    project_id, org_id=org_id, actor_id=actor_id
                )
            elif route == "knowledge_source_collection":
                data = self._service.list_knowledge_sources(project_id)
            elif item_id is not None and route == "knowledge_source_item":
                data = self._service.get_knowledge_source(project_id, item_id)
            elif route == "conversation_collection":
                data = self._service.list_conversation_results(project_id)
            elif item_id is not None and route == "conversation_item":
                data = self._service.get_conversation_result(project_id, item_id)
            elif route == "cad_geometry_collection":
                data = self._service.list_cad_geometries(project_id)
            elif item_id is not None and route == "cad_geometry_item":
                data = self._service.get_cad_geometry(project_id, item_id)
            elif item_id is not None and route == "snapshot_item":
                data = self._service.get_connector_snapshot(project_id, item_id)
            elif item_id is not None and route == "impact_item":
                data = self._service.get_change_assessment(project_id, item_id)
            elif route == "preview_collection":
                data = self._service.list_change_previews(project_id)
            elif item_id is not None and route == "preview_item":
                data = self._service.get_change_preview(project_id, item_id)
            elif route == "external_evidence_plan_collection":
                data = self._service.list_external_evidence_plans(project_id)
            elif item_id is not None and route == "external_evidence_plan_item":
                data = self._service.get_external_evidence_plan(project_id, item_id)
            elif route == "external_evidence_plan_verification_collection":
                data = self._service.list_external_evidence_plan_verifications(
                    project_id
                )
            elif (
                item_id is not None
                and route == "external_evidence_plan_verification_item"
            ):
                data = self._service.get_external_evidence_plan_verification(
                    project_id, item_id
                )
            elif route == "plan_verification_collection":
                data = self._service.list_plan_verifications(project_id)
            elif item_id is not None and route == "plan_verification_item":
                data = self._service.get_plan_verification(project_id, item_id)
            elif route == "design_proposal_collection":
                data = self._service.list_design_proposals(project_id)
            elif item_id is not None and route == "design_proposal_item":
                data = self._service.get_design_proposal(project_id, item_id)
            elif route == "design_candidate_collection":
                data = self._service.list_design_candidates(project_id)
            elif item_id is not None and route == "design_candidate_item":
                data = self._service.get_design_candidate(project_id, item_id)
            elif route == "design_simulation_collection":
                data = self._service.list_simulation_bindings(project_id)
            elif item_id is not None and route == "design_simulation_item":
                data = self._service.get_simulation_binding(project_id, item_id)
            elif route == "conversational_claim_collection":
                data = self._service.list_conversational_claims(project_id)
            elif item_id is not None and route == "conversational_claim_item":
                data = self._service.get_conversational_claim(project_id, item_id)
            elif route == "design_transition_collection":
                data = self._service.list_design_transitions(project_id)
            elif route == "release_diagnosis_collection":
                data = self._service.list_release_diagnoses(project_id)
            elif item_id is not None and route == "release_diagnosis_item":
                data = self._service.get_release_diagnosis(project_id, item_id)
            elif route == "resolution_plan_collection":
                data = self._service.list_resolution_plans(project_id)
            elif item_id is not None and route == "resolution_plan_item":
                data = self._service.get_resolution_plan(project_id, item_id)
            elif item_id is not None and route == "evidence_item":
                data = self._service.get_release_evidence(project_id, item_id)
            elif item_id is not None and route == "decision_item":
                data = self._service.get_release_decision(project_id, item_id)
            else:
                raise APIError(
                    405,
                    "method_not_allowed",
                    "Method is not allowed.",
                    headers={"Allow": "POST"},
                )
            return 200, {"data": data}, {}
        if method != "POST":
            allowed = "GET" if route.endswith("_item") else "POST"
            raise APIError(
                405,
                "method_not_allowed",
                "Method is not allowed.",
                headers={"Allow": allowed},
            )
        self._validate_csrf(headers)
        payload = self._parse_json(headers, body)
        if route == "knowledge_source_collection":
            knowledge_command = IngestKnowledgeSourceCommand.model_validate(payload)
            context = self._mutation_context(headers, project_id)
            result = self._service.ingest_knowledge_source(
                project_id, knowledge_command, context
            )
        elif route == "conversation_collection":
            conversation_command = RunConversationCommand.model_validate(payload)
            context = self._mutation_context(headers, project_id)
            result = self._service.run_conversation(
                project_id, conversation_command, context
            )
        elif route == "cad_geometry_collection":
            geometry_command = IngestCADGeometryCommand.model_validate(payload)
            context = self._mutation_context(headers, project_id)
            result = self._service.ingest_cad_geometry(
                project_id, geometry_command, context
            )
        elif route == "snapshot_collection":
            snapshot_command = CaptureSnapshotCommand.model_validate(payload)
            context = self._mutation_context(headers, project_id)
            result = self._service.capture_snapshot(
                project_id, snapshot_command, context
            )
        elif route == "impact_collection":
            impact_command = AnalyzeChangeCommand.model_validate(payload)
            context = self._mutation_context(headers, project_id)
            result = self._service.analyze_change(project_id, impact_command, context)
        elif route == "preview_collection":
            preview_command = CreateChangePreviewCommand.model_validate(payload)
            context = self._mutation_context(headers, project_id)
            result = self._service.create_change_preview(
                project_id, preview_command, context
            )
        elif route == "external_evidence_plan_collection":
            external_plan_command = CreateExternalEvidencePlanCommand.model_validate(
                payload
            )
            context = self._mutation_context(headers, project_id)
            result = self._service.create_external_evidence_plan(
                project_id, external_plan_command, context
            )
        elif route == "external_evidence_plan_verification_collection":
            external_verification_command = (
                VerifyExternalEvidencePlanCommand.model_validate(payload)
            )
            context = self._mutation_context(headers, project_id)
            result = self._service.verify_external_evidence_plan(
                project_id, external_verification_command, context
            )
        elif route == "plan_verification_collection":
            verification_command = VerifyPlanCommand.model_validate(payload)
            context = self._mutation_context(headers, project_id)
            result = self._service.verify_plan(
                project_id, verification_command, context
            )
        elif route == "design_proposal_collection":
            design_command = CreateDesignProposalCommand.model_validate(payload)
            context = self._mutation_context(headers, project_id)
            result = self._service.create_design_proposal(
                project_id, design_command, context
            )
        elif route == "design_candidate_collection":
            candidate_command = ConfirmDesignCandidateCommand.model_validate(payload)
            context = self._mutation_context(headers, project_id)
            result = self._service.confirm_design_candidate(
                project_id, candidate_command, context
            )
        elif route == "design_simulation_collection":
            simulation_command = BindDesignSimulationCommand.model_validate(payload)
            context = self._mutation_context(headers, project_id)
            result = self._service.bind_design_simulation(
                project_id, simulation_command, context
            )
        elif route == "conversational_claim_collection":
            claim_command = RecordConversationalClaimCommand.model_validate(payload)
            context = self._mutation_context(headers, project_id)
            result = self._service.record_conversational_claim(
                project_id, claim_command, context
            )
        elif route == "design_transition_collection":
            transition_command = AppendDesignTransitionCommand.model_validate(payload)
            context = self._mutation_context(headers, project_id)
            result = self._service.append_design_transition(
                project_id, transition_command, context
            )
        elif route == "release_diagnosis_collection":
            diagnosis_command = CreateReleaseDiagnosisCommand.model_validate(payload)
            context = self._mutation_context(headers, project_id)
            result = self._service.create_release_diagnosis(
                project_id, diagnosis_command, context
            )
        elif route == "resolution_plan_collection":
            resolution_command = CreateResolutionPlanCommand.model_validate(payload)
            context = self._mutation_context(headers, project_id)
            result = self._service.create_resolution_plan(
                project_id, resolution_command, context
            )
        elif route == "evidence_collection":
            evidence_command = IngestReleaseEvidenceCommand.model_validate(payload)
            context = self._mutation_context(headers, project_id)
            result = self._service.ingest_release_evidence(
                project_id, evidence_command, context
            )
        elif route == "decision_collection":
            decision_command = EvaluateReleaseCommand.model_validate(payload)
            context = self._mutation_context(headers, project_id)
            result = self._service.evaluate_release(
                project_id, decision_command, context
            )
        else:
            raise APIError(
                405,
                "method_not_allowed",
                "Method is not allowed.",
                headers={"Allow": "GET"},
            )
        return (
            result.status,
            result.payload,
            {
                "ETag": f'"project:{project_id}:v{result.project_version}"',
                "Idempotency-Replayed": "true" if result.replayed else "false",
            },
        )

    def _options(
        self, target: str, headers: Sequence[tuple[str, str]]
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        route, _match = self._route_match(target)
        if route == "unknown" or route.endswith("_item"):
            raise APIError(404, "not_found", "Resource was not found.")
        requested_method = self._one_header(
            headers, "Access-Control-Request-Method", required=True
        )
        requested_headers = self._one_header(
            headers, "Access-Control-Request-Headers", required=True
        )
        allowed_headers = {
            "content-type",
            "idempotency-key",
            "if-match",
            "x-forge-csrf",
            "x-forge-org-id",
            "x-forge-actor-id",
        }
        supplied = {
            item.strip().lower() for item in (requested_headers or "").split(",")
        }
        if requested_method != "POST" or not supplied.issubset(allowed_headers):
            raise APIError(403, "preflight_forbidden", "Preflight is not allowed.")
        return 204, {}, {"Allow": "POST, OPTIONS"}

    def handle(
        self,
        method: str,
        target: str,
        headers: Sequence[tuple[str, str]],
        body: bytes = b"",
    ) -> APIResponse:
        content_type = "application/json"
        try:
            self._validate_target(target)
            self._validate_host_origin(method, headers)
            dashboard_asset = load_dashboard_asset(target)
            if dashboard_asset is not None:
                if method != "GET":
                    raise APIError(
                        405,
                        "method_not_allowed",
                        "Method is not allowed.",
                        headers={"Allow": "GET"},
                    )
                status = 200
                response_body = dashboard_asset.body
                content_type = dashboard_asset.content_type
                extra_headers = {
                    "Content-Security-Policy": (DASHBOARD_CONTENT_SECURITY_POLICY)
                }
            elif is_dashboard_target(target):
                raise APIError(404, "not_found", "Resource was not found.")
            elif method == "OPTIONS":
                status, payload, extra_headers = self._options(target, headers)
                response_body = b""
            else:
                status, payload, extra_headers = self._dispatch(
                    method, target, headers, body
                )
                response_body = _json_bytes(payload)
        except APIError as exc:
            status = exc.status
            extra_headers = exc.headers
            response_body = _json_bytes(
                {
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "fields": list(exc.fields),
                    }
                }
            )
        except AuthorizationDeniedError:
            status = 403
            extra_headers = {}
            response_body = _error_bytes(
                "permission_denied",
                "Identity is not allowed to perform this operation.",
            )
        except ValidationError as exc:
            status = 422
            extra_headers = {}
            response_body = _json_bytes(
                {
                    "error": {
                        "code": "validation_failed",
                        "message": "Request did not satisfy the API contract.",
                        "fields": list(self._validation_fields(exc)),
                    }
                }
            )
        except RecordNotFoundError, KeyError:
            status = 404
            extra_headers = {}
            response_body = _error_bytes("not_found", "Resource was not found.")
        except VersionConflictError, IdempotencyConflictError, IntegrityConflictError:
            status = 409
            extra_headers = {}
            response_body = _error_bytes(
                "conflict", "Request conflicts with immutable project state."
            )
        except StorageBusyError:
            status = 503
            extra_headers = {}
            response_body = _error_bytes(
                "storage_busy", "Storage is temporarily unavailable."
            )
        except ValueError:
            status = 422
            extra_headers = {}
            response_body = _error_bytes(
                "domain_validation_failed",
                "Request is inconsistent with engineering state.",
            )
        except CorruptRecordError:
            status = 500
            extra_headers = {}
            response_body = _error_bytes(
                "integrity_error", "Stored engineering state failed validation."
            )
        except Exception:
            status = 500
            extra_headers = {}
            response_body = _error_bytes(
                "internal_error", "The request could not be completed."
            )
        response_headers = dict(_SECURITY_HEADERS)
        response_headers.update(extra_headers)
        if status != 204:
            response_headers["Content-Type"] = content_type
        response_headers["Content-Length"] = str(len(response_body))
        return APIResponse(status=status, headers=response_headers, body=response_body)


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _error_bytes(code: str, message: str) -> bytes:
    return _json_bytes({"error": {"code": code, "message": message, "fields": []}})


class ForgeLoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    app: LoopbackAPI


class ForgeLoopbackHandler(BaseHTTPRequestHandler):
    server_version = "FORGE"
    sys_version = ""

    def version_string(self) -> str:
        return "FORGE"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _dispatch_request(self) -> None:
        server = self.server
        assert isinstance(server, ForgeLoopbackHTTPServer)
        raw_headers = tuple(self.headers.raw_items())
        body = b""
        lengths = tuple(
            value for name, value in raw_headers if name.lower() == "content-length"
        )
        if (
            len(lengths) == 1
            and re.fullmatch(r"0|[1-9][0-9]*", lengths[0]) is not None
            and int(lengths[0]) <= server.app.max_request_bytes
        ):
            body = self.rfile.read(int(lengths[0]))
        response = server.app.handle(self.command, self.path, raw_headers, body)
        self.send_response(response.status)
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.end_headers()
        if response.body:
            self.wfile.write(response.body)

    do_GET = _dispatch_request
    do_POST = _dispatch_request
    do_OPTIONS = _dispatch_request
    do_DELETE = _dispatch_request
    do_PATCH = _dispatch_request
    do_PUT = _dispatch_request


def create_loopback_server(
    service: ReleaseIntegrationService,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    csrf_secret: bytes | None = None,
    local_installation_id: str = "local-installation",
) -> ForgeLoopbackHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("FORGE API may bind only to 127.0.0.1")
    if csrf_secret is not None and not csrf_secret:
        raise ValueError("CSRF secret cannot be empty")
    if re.fullmatch(_SAFE_SEGMENT, local_installation_id) is None:
        raise ValueError("local installation ID must be opaque and safe")
    server = ForgeLoopbackHTTPServer((host, port), ForgeLoopbackHandler)
    try:
        bound_port = int(server.server_address[1])
        server.app = LoopbackAPI(
            service,
            port=bound_port,
            csrf_secret=(
                secrets.token_bytes(32) if csrf_secret is None else csrf_secret
            ),
            local_installation_id=local_installation_id,
        )
    except Exception:
        server.server_close()
        raise
    return server


__all__ = [
    "APIResponse",
    "ForgeLoopbackHTTPServer",
    "LoopbackAPI",
    "MAX_REQUEST_BYTES",
    "create_loopback_server",
]
