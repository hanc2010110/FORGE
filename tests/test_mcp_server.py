from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import forge_core.mcp_server as mcp_server
from forge_core.mcp_server import ForgeMCPServer, create_agent_runtime


class MCPServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.server, self.store = create_agent_runtime(
            Path(self.temporary.name) / "forge.db",
            config_dir=Path(self.temporary.name) / "config" / "github",
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            message["params"] = params
        response = self.server.handle(message)
        assert response is not None
        return response

    def test_initialize_and_list_tools(self) -> None:
        initialized = self.request("initialize")
        self.assertEqual(
            initialized["result"]["serverInfo"]["name"],
            "forge-engineering-agent",
        )
        listed = self.request("tools/list")
        names = {item["name"] for item in listed["result"]["tools"]}
        self.assertIn("forge_get_capabilities", names)
        self.assertIn("forge_evaluate_release", names)
        self.assertNotIn("forge_control_device", names)

    def test_tool_call_returns_structured_content(self) -> None:
        response = self.request(
            "tools/call",
            {"name": "forge_get_capabilities", "arguments": {}},
        )
        result = response["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(
            result["structuredContent"]["product"], "FORGE Engineering Agent"
        )

    def test_protocol_and_tool_errors_are_bounded(self) -> None:
        self.assertIsNone(
            self.server.handle(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
        )
        self.assertEqual(self.request("ping")["result"], {})
        self.assertEqual(self.request("missing")["error"]["code"], -32601)
        invalid = self.server.handle({"jsonrpc": "1.0", "id": 1})
        assert invalid is not None
        self.assertEqual(invalid["error"]["code"], -32600)
        bad_params = self.request("tools/call", {"name": 1})
        self.assertEqual(bad_params["error"]["code"], -32602)
        unknown = self.request("tools/call", {"name": "unknown", "arguments": {}})
        self.assertTrue(unknown["result"]["isError"])
        missing_project = self.request(
            "tools/call",
            {
                "name": "forge_get_project_context",
                "arguments": {"project_id": "missing"},
            },
        )
        self.assertTrue(missing_project["result"]["isError"])

    def test_static_response_helpers(self) -> None:
        result = ForgeMCPServer._result("x", {"ok": True})
        self.assertEqual(result["id"], "x")
        error = ForgeMCPServer._error("x", -1, "bad")
        self.assertEqual(error["error"]["message"], "bad")

    def test_main_loop_rejects_bad_stdio_messages_and_closes_store(self) -> None:
        fake_server = _FakeServer()
        fake_store = _FakeStore()
        stdin = _FakeStdin(
            [
                b"x" * (mcp_server._MAX_MESSAGE_BYTES + 1),
                b"\n",
                b"not json\n",
                b"[]\n",
                b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n',
                b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n',
            ]
        )
        stdout = _FakeStdout()
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(mcp_server, "create_agent_runtime") as runtime,
            patch("sys.stdin", stdin),
            patch("sys.stdout", stdout),
            patch.dict(
                os.environ,
                {
                    "FORGE_DATABASE_PATH": str(Path(temporary) / "forge.db"),
                    "FORGE_CONFIG_DIR": str(Path(temporary) / "config"),
                },
            ),
        ):
            runtime.return_value = (fake_server, fake_store)
            mcp_server.main()

        self.assertTrue(fake_store.closed)
        config_dir = runtime.call_args.kwargs["config_dir"]
        self.assertEqual(
            config_dir,
            Path(temporary).joinpath("config", "github").resolve(),
        )
        output_lines = [
            json_line.decode("utf-8").strip() for json_line in stdout.buffer.writes
        ]
        self.assertIn("MCP message is too large", output_lines[0])
        self.assertIn("parse error", output_lines[1])
        self.assertIn("request must be an object", output_lines[2])
        self.assertIn('"id":2', output_lines[3])
        self.assertEqual(fake_server.methods, ["ping", "notifications/initialized"])


class _FakeServer:
    def __init__(self) -> None:
        self.methods: list[str] = []

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = str(message.get("method"))
        self.methods.append(method)
        if method == "notifications/initialized":
            return None
        return ForgeMCPServer._result(message.get("id"), {"ok": True})


class _FakeStore:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeBuffer:
    def __init__(self, lines: list[bytes] | None = None) -> None:
        self._lines = lines or []
        self.writes: list[bytes] = []

    def readline(self, _limit: int = -1) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)

    def write(self, value: bytes) -> int:
        self.writes.append(value)
        return len(value)

    def flush(self) -> None:
        return


class _FakeStdin:
    def __init__(self, lines: list[bytes]) -> None:
        self.buffer = _FakeBuffer(lines)


class _FakeStdout:
    def __init__(self) -> None:
        self.buffer = _FakeBuffer()


if __name__ == "__main__":
    unittest.main()
