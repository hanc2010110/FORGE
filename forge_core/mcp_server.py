from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forge_core.agent_gateway import ForgeAgentGateway
from forge_core.connectors import ConnectorRegistry
from forge_core.github_integration import (
    GitHubCredentialStore,
    GitHubIntegrationService,
)
from forge_core.integration_hub import IntegrationHub
from forge_core.release_service import ReleaseIntegrationService
from forge_core.sqlite_store import SQLiteEvidenceStore

_PROTOCOL_VERSION = "2025-06-18"
_MAX_MESSAGE_BYTES = 2_000_000


class ForgeMCPServer:
    """Small dependency-free MCP stdio adapter for the FORGE agent gateway."""

    def __init__(self, gateway: ForgeAgentGateway) -> None:
        self._gateway = gateway

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if message.get("jsonrpc") != "2.0":
            return self._error(message.get("id"), -32600, "invalid JSON-RPC request")
        method = message.get("method")
        request_id = message.get("id")
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            return self._result(
                request_id,
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "forge-engineering-agent",
                        "version": "0.1.0",
                    },
                    "instructions": (
                        "Use FORGE as the evidence and policy authority. Obtain "
                        "explicit user approval before every non-read-only tool call."
                    ),
                },
            )
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(request_id, {"tools": self._gateway.list_tools()})
        if method == "tools/call":
            return self._call_tool(request_id, message.get("params"))
        if request_id is None:
            return None
        return self._error(request_id, -32601, "method not found")

    def _call_tool(self, request_id: object, params: object) -> dict[str, Any]:
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "tool params must be an object")
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return self._error(request_id, -32602, "invalid tool call params")
        try:
            payload = self._gateway.call_tool(name, arguments)
        except (KeyError, PermissionError, RuntimeError, TypeError, ValueError) as exc:
            return self._result(
                request_id,
                {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            )
        serialized = json.dumps(
            payload, allow_nan=False, ensure_ascii=False, separators=(",", ":")
        )
        return self._result(
            request_id,
            {
                "content": [{"type": "text", "text": serialized}],
                "structuredContent": payload,
                "isError": False,
            },
        )

    @staticmethod
    def _result(request_id: object, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: object, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }


def create_agent_runtime(
    database_path: Path, *, config_dir: Path | None = None
) -> tuple[ForgeMCPServer, SQLiteEvidenceStore]:
    store = SQLiteEvidenceStore(database_path)
    try:
        service = ReleaseIntegrationService(
            store, ConnectorRegistry(), clock=lambda: datetime.now(UTC)
        )
        github_dir = config_dir or database_path.parent / ".forge-local" / "github"
        github = GitHubIntegrationService(GitHubCredentialStore(github_dir))
        gateway = ForgeAgentGateway(
            service,
            integration_hub=IntegrationHub(status_providers={"github": github}),
        )
        return ForgeMCPServer(gateway), store
    except Exception:
        store.close()
        raise


def _write_message(message: dict[str, Any]) -> None:
    raw = json.dumps(
        message, allow_nan=False, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    sys.stdout.buffer.write(raw + b"\n")
    sys.stdout.buffer.flush()


def main() -> None:
    database_path = Path(os.environ.get("FORGE_DATABASE_PATH", "forge.db")).resolve()
    config_text = os.environ.get("FORGE_CONFIG_DIR")
    config_dir = (
        Path(config_text).expanduser().resolve() / "github" if config_text else None
    )
    server, store = create_agent_runtime(database_path, config_dir=config_dir)
    try:
        while raw := sys.stdin.buffer.readline(_MAX_MESSAGE_BYTES + 1):
            if len(raw) > _MAX_MESSAGE_BYTES:
                _write_message(
                    ForgeMCPServer._error(None, -32600, "MCP message is too large")
                )
                continue
            if not raw.strip():
                continue
            try:
                message = json.loads(raw)
            except UnicodeDecodeError, json.JSONDecodeError:
                _write_message(ForgeMCPServer._error(None, -32700, "parse error"))
                continue
            if not isinstance(message, dict):
                _write_message(
                    ForgeMCPServer._error(None, -32600, "request must be an object")
                )
                continue
            response = server.handle(message)
            if response is not None:
                _write_message(response)
    except KeyboardInterrupt:
        pass
    finally:
        store.close()


if __name__ == "__main__":
    main()


__all__ = ["ForgeMCPServer", "create_agent_runtime"]
