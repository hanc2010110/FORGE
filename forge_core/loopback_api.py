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
    CaptureSnapshotCommand,
    CreateProjectCommand,
    EvaluateReleaseCommand,
    IngestReleaseEvidenceCommand,
    MutationContext,
    ReleaseIntegrationService,
)

MAX_REQUEST_BYTES = 1_048_576
_SAFE_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
_PROJECT_ROUTE = re.compile(rf"^/api/v1/projects/(?P<project>{_SAFE_SEGMENT})$")
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
            not target.startswith("/api/v1/")
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
        )

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
            ("snapshot_collection", _SNAPSHOT_ROUTE),
            ("snapshot_item", _SNAPSHOT_ITEM_ROUTE),
            ("impact_collection", _IMPACT_ROUTE),
            ("impact_item", _IMPACT_ITEM_ROUTE),
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
        if method == "GET" and target == "/api/v1/session":
            token = self._token()
            return (
                200,
                {"csrf_token": token},
                {
                    "Set-Cookie": (
                        f"forge_csrf={token}; HttpOnly; SameSite=Strict; Path=/api/v1"
                    )
                },
            )
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
            if route == "project_item":
                data = self._service.get_project(project_id)
            elif item_id is not None and route == "snapshot_item":
                data = self._service.get_connector_snapshot(project_id, item_id)
            elif item_id is not None and route == "impact_item":
                data = self._service.get_change_assessment(project_id, item_id)
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
        if route == "snapshot_collection":
            snapshot_command = CaptureSnapshotCommand.model_validate(payload)
            context = self._mutation_context(headers, project_id)
            result = self._service.capture_snapshot(
                project_id, snapshot_command, context
            )
        elif route == "impact_collection":
            impact_command = AnalyzeChangeCommand.model_validate(payload)
            context = self._mutation_context(headers, project_id)
            result = self._service.analyze_change(project_id, impact_command, context)
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
        try:
            self._validate_target(target)
            self._validate_host_origin(method, headers)
            if method == "OPTIONS":
                status, payload, extra_headers = self._options(target, headers)
            else:
                status, payload, extra_headers = self._dispatch(
                    method, target, headers, body
                )
            response_body = b"" if status == 204 else _json_bytes(payload)
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
            response_headers["Content-Type"] = "application/json"
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
