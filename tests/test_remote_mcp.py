from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from forge_core.mcp_server import create_agent_runtime
from forge_core.remote_mcp import ForgeRemoteMCPTransport


class RemoteMCPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.server, self.store = create_agent_runtime(
            Path(self.temporary.name) / "forge.db"
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_streamable_http_post_reuses_mcp_handler(self) -> None:
        transport = ForgeRemoteMCPTransport(self.server)
        status, headers, body = transport.handle_http(
            method="POST",
            path="/mcp",
            headers={"host": "127.0.0.1:43128", "content-type": "application/json"},
            body=json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            ).encode(),
            client_host="127.0.0.1",
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json")
        names = {item["name"] for item in json.loads(body.decode())["result"]["tools"]}
        self.assertIn("forge_get_capabilities", names)

    def test_transport_rejects_remote_hosts_and_missing_bearer(self) -> None:
        transport = ForgeRemoteMCPTransport(self.server, bearer_token="dev-token")
        remote = transport.handle_http(
            method="POST",
            path="/mcp",
            headers={"host": "forge.example.com", "content-type": "application/json"},
            body=b"{}",
            client_host="203.0.113.10",
        )
        self.assertEqual(remote[0], 403)
        missing_token = transport.handle_http(
            method="POST",
            path="/mcp",
            headers={"host": "127.0.0.1:43128", "content-type": "application/json"},
            body=b"{}",
            client_host="127.0.0.1",
        )
        self.assertEqual(missing_token[0], 401)

    def test_transport_bounds_method_path_type_and_size(self) -> None:
        transport = ForgeRemoteMCPTransport(self.server, max_request_bytes=10)
        self.assertEqual(
            transport.handle_http(
                method="GET",
                path="/mcp",
                headers={"host": "127.0.0.1", "content-type": "application/json"},
                body=b"",
                client_host="127.0.0.1",
            )[0],
            405,
        )
        self.assertEqual(
            transport.handle_http(
                method="POST",
                path="/bad",
                headers={"host": "127.0.0.1", "content-type": "application/json"},
                body=b"{}",
                client_host="127.0.0.1",
            )[0],
            404,
        )
        self.assertEqual(
            transport.handle_http(
                method="POST",
                path="/mcp",
                headers={"host": "127.0.0.1", "content-type": "text/plain"},
                body=b"{}",
                client_host="127.0.0.1",
            )[0],
            415,
        )
        self.assertEqual(
            transport.handle_http(
                method="POST",
                path="/mcp",
                headers={"host": "127.0.0.1", "content-type": "application/json"},
                body=b"01234567890",
                client_host="127.0.0.1",
            )[0],
            413,
        )


if __name__ == "__main__":
    unittest.main()
