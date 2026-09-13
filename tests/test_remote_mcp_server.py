from __future__ import annotations

import http.client
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

import forge_core.remote_mcp_server as remote_mcp_server
from forge_core.remote_mcp_server import create_remote_mcp_server


class RemoteMCPServerTests(unittest.TestCase):
    def test_server_serves_initialize_on_actual_loopback_with_bearer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server, store = create_remote_mcp_server(
                Path(temporary) / "forge.db",
                port=0,
                bearer_token="dev-token",
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = _server_address(server.server_address)
                conn = http.client.HTTPConnection(host, port, timeout=5)
                conn.request(
                    "POST",
                    "/mcp",
                    body=json.dumps(
                        {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
                    ).encode(),
                    headers={
                        "Authorization": "Bearer dev-token",
                        "Content-Type": "application/json",
                    },
                )
                response = conn.getresponse()
                payload = json.loads(response.read().decode())
                conn.close()

                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("cache-control"), "no-store")
                self.assertEqual(
                    payload["result"]["serverInfo"]["name"],
                    "forge-engineering-agent",
                )
            finally:
                server.shutdown()
                server.server_close()
                store.close()

    def test_server_rejects_missing_bearer_before_mcp_handler(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server, store = create_remote_mcp_server(
                Path(temporary) / "forge.db",
                port=0,
                bearer_token="dev-token",
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = _server_address(server.server_address)
                conn = http.client.HTTPConnection(host, port, timeout=5)
                conn.request(
                    "POST",
                    "/mcp",
                    body=b"{}",
                    headers={"Content-Type": "application/json"},
                )
                response = conn.getresponse()
                payload = json.loads(response.read().decode())
                conn.close()

                self.assertEqual(response.status, 401)
                self.assertEqual(response.getheader("cache-control"), "no-store")
                self.assertEqual(payload["error"]["code"], "unauthorized")
            finally:
                server.shutdown()
                server.server_close()
                store.close()

    def test_server_rejects_bad_length_and_chunked_before_reading_body(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server, store = create_remote_mcp_server(
                Path(temporary) / "forge.db", port=0, bearer_token="dev-token"
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = _server_address(server.server_address)
                self.assertIn(
                    b"411",
                    _raw_http(
                        host,
                        port,
                        b"POST /mcp HTTP/1.1\r\n"
                        b"Host: 127.0.0.1\r\n"
                        b"Content-Type: application/json\r\n"
                        b"\r\n",
                    ),
                )
                self.assertIn(
                    b"400",
                    _raw_http(
                        host,
                        port,
                        b"POST /mcp HTTP/1.1\r\n"
                        b"Host: 127.0.0.1\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Transfer-Encoding: chunked\r\n"
                        b"\r\n",
                    ),
                )
                self.assertIn(
                    b"413",
                    _raw_http(
                        host,
                        port,
                        b"POST /mcp HTTP/1.1\r\n"
                        b"Host: 127.0.0.1\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Content-Length: 2000001\r\n"
                        b"\r\n",
                    ),
                )
            finally:
                server.shutdown()
                server.server_close()
                store.close()

    def test_server_rejects_non_loopback_bind_and_invalid_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "localhost-only"):
                create_remote_mcp_server(
                    Path(temporary) / "forge.db",
                    host="0.0.0.0",
                    port=0,
                    bearer_token="dev-token",
                )
            with self.assertRaisesRegex(ValueError, "TCP bounds"):
                create_remote_mcp_server(
                    Path(temporary) / "forge.db",
                    port=65_536,
                    bearer_token="dev-token",
                )

    def test_main_prints_bound_port_and_closes_server_and_store(self) -> None:
        fake_server = _FakeHTTPServer()
        fake_store = _FakeStore()
        stdout = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(remote_mcp_server, "create_remote_mcp_server") as create,
            patch("sys.stdout", stdout),
            patch.dict(
                os.environ,
                {
                    "FORGE_DATABASE_PATH": str(Path(temporary) / "forge.db"),
                    "FORGE_REMOTE_MCP_PORT": "0",
                    "FORGE_REMOTE_MCP_TOKEN": "dev-token",
                },
            ),
        ):
            create.return_value = (fake_server, fake_store)
            remote_mcp_server.main()

        self.assertTrue(fake_server.served)
        self.assertTrue(fake_server.closed)
        self.assertTrue(fake_store.closed)
        self.assertIn("http://127.0.0.1:54321/mcp", stdout.getvalue())
        self.assertEqual(create.call_args.kwargs["port"], 0)
        self.assertEqual(create.call_args.kwargs["bearer_token"], "dev-token")

    def test_main_rejects_invalid_port_before_creating_server(self) -> None:
        with (
            patch.object(remote_mcp_server, "create_remote_mcp_server") as create,
            patch.dict(os.environ, {"FORGE_REMOTE_MCP_PORT": "not-a-port"}),
            self.assertRaisesRegex(ValueError, "must be an integer"),
        ):
            remote_mcp_server.main()
        create.assert_not_called()

    def test_main_requires_bearer_token(self) -> None:
        with (
            patch.object(remote_mcp_server, "create_remote_mcp_server") as create,
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(ValueError, "FORGE_REMOTE_MCP_TOKEN is required"),
        ):
            remote_mcp_server.main()
        create.assert_not_called()


def _raw_http(host: str, port: int, request: bytes) -> bytes:
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(request)
        sock.shutdown(socket.SHUT_WR)
        return sock.recv(4096)


def _server_address(address: object) -> tuple[str, int]:
    host, port = cast(tuple[str, int], address)
    return host, port


class _FakeHTTPServer:
    def __init__(self) -> None:
        self.server_address = ("127.0.0.1", 54321)
        self.served = False
        self.closed = False

    def serve_forever(self) -> None:
        self.served = True
        raise KeyboardInterrupt

    def server_close(self) -> None:
        self.closed = True


class _FakeStore:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


if __name__ == "__main__":
    unittest.main()
