from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from typing import Any

from forge_core.mcp_server import ForgeMCPServer

DEFAULT_REMOTE_MCP_PATH = "/mcp"
DEFAULT_REMOTE_MCP_MAX_REQUEST_BYTES = 2_000_000
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


class ForgeRemoteMCPTransport:
    """Bounded Streamable-HTTP-shaped MCP development transport.

    This is intentionally not an auth/SSO product. It defaults to localhost and may
    require a caller-provided bearer token for development tunnels.
    """

    def __init__(
        self,
        server: ForgeMCPServer,
        *,
        path: str = DEFAULT_REMOTE_MCP_PATH,
        bearer_token: str | None = None,
        allow_non_localhost: bool = False,
        max_request_bytes: int = DEFAULT_REMOTE_MCP_MAX_REQUEST_BYTES,
    ) -> None:
        if not path.startswith("/") or "//" in path:
            raise ValueError("remote MCP path is invalid")
        if (
            max_request_bytes < 2
            or max_request_bytes > DEFAULT_REMOTE_MCP_MAX_REQUEST_BYTES
        ):
            raise ValueError("remote MCP request bound is invalid")
        if bearer_token is not None and (
            not bearer_token or "\r" in bearer_token or "\n" in bearer_token
        ):
            raise ValueError("remote MCP bearer token boundary is invalid")
        self._server = server
        self._path = path
        self._bearer_token = bearer_token
        self._allow_non_localhost = allow_non_localhost
        self._max_request_bytes = max_request_bytes

    def handle_http(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
        client_host: str,
    ) -> tuple[int, dict[str, str], bytes]:
        if headers.get("transfer-encoding", headers.get("Transfer-Encoding", "")):
            return self._json_error(
                400,
                "transfer_encoding_not_supported",
                "Use a fixed Content-Length for MCP messages.",
            )
        if path != self._path:
            return self._json_error(404, "not_found", "MCP endpoint not found.")
        if method != "POST":
            return self._json_error(
                405, "method_not_allowed", "Use POST for MCP messages.", allow="POST"
            )
        if not self._allow_non_localhost and client_host not in _LOCAL_HOSTS:
            return self._json_error(403, "localhost_required", "MCP is localhost-only.")
        content_type = headers.get("content-type", headers.get("Content-Type", ""))
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            return self._json_error(
                415, "unsupported_media_type", "Use application/json."
            )
        if len(body) > self._max_request_bytes:
            return self._json_error(
                413, "request_too_large", "MCP message is too large."
            )
        if self._bearer_token is not None:
            authorization = headers.get(
                "authorization", headers.get("Authorization", "")
            )
            expected = f"Bearer {self._bearer_token}"
            if not secrets.compare_digest(authorization, expected):
                return self._json_error(401, "unauthorized", "Missing bearer token.")
        try:
            message = json.loads(body.decode("utf-8"))
        except UnicodeDecodeError, json.JSONDecodeError:
            return self._json_error(400, "parse_error", "Invalid JSON payload.")
        if not isinstance(message, dict):
            return self._json_error(
                400, "invalid_request", "MCP request must be an object."
            )
        response = self._server.handle(message)
        if response is None:
            return 202, self._headers(), b"{}"
        raw = json.dumps(
            response, allow_nan=False, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return 200, self._headers(), raw

    @staticmethod
    def _headers() -> dict[str, str]:
        return {"content-type": "application/json", "cache-control": "no-store"}

    @staticmethod
    def _json_error(
        status: int,
        code: str,
        message: str,
        *,
        allow: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        headers = ForgeRemoteMCPTransport._headers()
        if allow is not None:
            headers["allow"] = allow
        payload: dict[str, Any] = {"error": {"code": code, "message": message}}
        return (
            status,
            headers,
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(),
        )


__all__ = ["DEFAULT_REMOTE_MCP_PATH", "ForgeRemoteMCPTransport"]
