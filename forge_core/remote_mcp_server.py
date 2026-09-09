from __future__ import annotations

import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from forge_core.mcp_server import create_agent_runtime
from forge_core.remote_mcp import (
    DEFAULT_REMOTE_MCP_MAX_REQUEST_BYTES,
    ForgeRemoteMCPTransport,
)

DEFAULT_REMOTE_MCP_PORT = 43128
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def create_remote_mcp_server(
    database_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_REMOTE_MCP_PORT,
    bearer_token: str | None = None,
) -> tuple[ThreadingHTTPServer, Any]:
    _validate_host(host)
    _validate_port(port)
    server, store = create_agent_runtime(database_path, config_dir=_github_config_dir())
    try:
        transport = ForgeRemoteMCPTransport(server, bearer_token=bearer_token)
    except Exception:
        store.close()
        raise

    class Handler(BaseHTTPRequestHandler):
        server_version = "FORGERemoteMCP/0.1"

        def do_POST(self) -> None:
            self._handle()

        def do_GET(self) -> None:
            self._handle()

        def _handle(self) -> None:
            if self.headers.get("Transfer-Encoding"):
                self._write(400, {"error": {"code": "transfer_encoding_not_supported"}})
                return
            length_text = self.headers.get("Content-Length")
            if length_text is None:
                self._write(411, {"error": {"code": "content_length_required"}})
                return
            try:
                length = int(length_text)
            except ValueError:
                self._write(400, {"error": {"code": "invalid_content_length"}})
                return
            if length < 0:
                self._write(400, {"error": {"code": "invalid_content_length"}})
                return
            if length > DEFAULT_REMOTE_MCP_MAX_REQUEST_BYTES:
                self._write(413, {"error": {"code": "request_too_large"}})
                return
            body = self.rfile.read(max(0, length))
            status, headers, payload = transport.handle_http(
                method=self.command,
                path=self.path,
                headers={key: value for key, value in self.headers.items()},
                body=body,
                client_host=self.client_address[0],
            )
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _write(self, status: int, payload: dict[str, object]) -> None:
            raw = json_dumps(payload)
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: object) -> None:
            return

    try:
        return ThreadingHTTPServer((host, port), Handler), store
    except Exception:
        store.close()
        raise


def main() -> None:
    database_path = Path(os.environ.get("FORGE_DATABASE_PATH", "forge.db")).resolve()
    port = _parse_port(os.environ.get("FORGE_REMOTE_MCP_PORT"))
    token = os.environ.get("FORGE_REMOTE_MCP_TOKEN")
    httpd, store = create_remote_mcp_server(
        database_path, port=port, bearer_token=token
    )
    bound_host, bound_port = httpd.server_address[:2]
    if isinstance(bound_host, bytes):
        bound_host = bound_host.decode("ascii", errors="replace")
    print(
        f"FORGE remote MCP listening on http://{bound_host}:{bound_port}/mcp",
        file=sys.stdout,
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        close = store.close
        close()


def _validate_host(host: str) -> None:
    if host not in _LOCAL_HOSTS:
        raise ValueError("remote MCP server is localhost-only")


def _validate_port(port: int) -> None:
    if port < 0 or port > 65_535:
        raise ValueError("remote MCP port is outside TCP bounds")


def _parse_port(raw: str | None) -> int:
    if raw is None:
        return DEFAULT_REMOTE_MCP_PORT
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError("FORGE_REMOTE_MCP_PORT must be an integer") from exc
    _validate_port(port)
    return port


def _github_config_dir() -> Path | None:
    root = os.environ.get("FORGE_CONFIG_DIR")
    if not root:
        return None
    return Path(root).expanduser().resolve() / "github"


def json_dumps(payload: dict[str, object]) -> bytes:
    import json

    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


if __name__ == "__main__":
    main()


__all__ = ["DEFAULT_REMOTE_MCP_PORT", "create_remote_mcp_server"]
