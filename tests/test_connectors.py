from __future__ import annotations

import socket
import subprocess
import unittest
from datetime import UTC, datetime
from typing import Literal
from unittest.mock import patch

from pydantic import ValidationError

from forge_core.change_management import ArtifactDomain, SourceSystem
from forge_core.connectors import (
    ConnectorCaptureBundle,
    ConnectorCaptureRequest,
    ConnectorRegistry,
    InMemoryReadOnlyAdapter,
    ReadOnlyAdapterManifest,
    adapter_manifest_hash,
)
from tests.test_release_readiness import artifact, interface_contract

NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)


def manifest() -> ReadOnlyAdapterManifest:
    adapter_id = "fake-git"
    adapter_version = "1.0.0"
    adapter_artifact_hash = "sha256:" + "8" * 64
    source_system = SourceSystem.GIT
    capabilities: tuple[Literal["snapshot.read", "interface.read"], ...] = (
        "interface.read",
        "snapshot.read",
    )
    return ReadOnlyAdapterManifest(
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        adapter_artifact_hash=adapter_artifact_hash,
        source_system=source_system,
        capabilities=capabilities,
        manifest_hash=adapter_manifest_hash(
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            adapter_artifact_hash=adapter_artifact_hash,
            source_system=source_system,
            capabilities=capabilities,
        ),
    )


def adapter() -> InMemoryReadOnlyAdapter:
    firmware = artifact(
        "controller-firmware",
        ArtifactDomain.FIRMWARE,
        SourceSystem.GIT,
        "commit-42",
        3,
        captured_at=NOW,
    )
    protocol = artifact(
        "controller-protocol",
        ArtifactDomain.PROTOCOL,
        SourceSystem.GIT,
        "schema-3",
        5,
        captured_at=NOW,
    )
    return InMemoryReadOnlyAdapter(
        manifest(),
        {"revision-12": (protocol, firmware)},
        {
            firmware.artifact_id: interface_contract("firmware"),
            protocol.artifact_id: interface_contract("protocol"),
        },
    )


def request() -> ConnectorCaptureRequest:
    return ConnectorCaptureRequest(
        connector_id="fake-git",
        capture_key="revision-12",
        project_id="project-1",
        snapshot_id="snapshot-12-git",
        hardware_revision_id="HW-12",
        captured_at=NOW,
    )


class ReadOnlyConnectorTests(unittest.TestCase):
    def test_manifest_is_hash_bound_and_cannot_request_write_capabilities(self) -> None:
        value = manifest()
        self.assertTrue(value.read_only)
        self.assertFalse(value.network_access)
        self.assertFalse(value.subprocess_access)
        self.assertFalse(value.filesystem_write)
        with self.assertRaises(ValidationError):
            ReadOnlyAdapterManifest.model_validate(
                value.model_dump(mode="python")
                | {"manifest_hash": "sha256:" + "0" * 64}
            )
        with self.assertRaises(ValidationError):
            ReadOnlyAdapterManifest.model_validate(
                value.model_dump(mode="python") | {"read_only": False}
            )

    def test_capture_is_deterministic_and_uses_no_io_process_or_network(self) -> None:
        registry = ConnectorRegistry()
        registry.register(adapter())
        with (
            patch("builtins.open") as open_call,
            patch.object(socket, "socket") as socket_call,
            patch.object(subprocess, "run") as run_call,
            patch.object(subprocess, "Popen") as popen_call,
        ):
            first = registry.capture(request())
            second = registry.capture(request())

        self.assertEqual(first, second)
        self.assertEqual(first.capture_hash, second.capture_hash)
        self.assertEqual(len(first.projections), 2)
        open_call.assert_not_called()
        socket_call.assert_not_called()
        run_call.assert_not_called()
        popen_call.assert_not_called()

    def test_registry_rejects_duplicates_unknowns_and_forged_capture(self) -> None:
        registry = ConnectorRegistry()
        value = adapter()
        registry.register(value)
        with self.assertRaises(ValueError):
            registry.register(value)
        with self.assertRaises(KeyError):
            registry.capture(
                request().model_copy(update={"connector_id": "missing-adapter"})
            )
        captured = value.capture(request())
        with self.assertRaises(ValidationError):
            ConnectorCaptureBundle.model_validate(
                captured.model_dump(mode="python")
                | {"snapshot_hash": "sha256:" + "0" * 64}
            )

    def test_request_has_no_path_surface_and_rejects_traversal_or_local_time(
        self,
    ) -> None:
        self.assertNotIn("path", ConnectorCaptureRequest.model_fields)
        for unsafe in ("../escape", "a/b", "a\\b", "bad\nvalue"):
            with self.subTest(value=unsafe), self.assertRaises(ValidationError):
                ConnectorCaptureRequest.model_validate(
                    request().model_dump(mode="python") | {"capture_key": unsafe}
                )
        with self.assertRaises(ValidationError):
            ConnectorCaptureRequest.model_validate(
                request().model_dump(mode="python")
                | {"captured_at": NOW.replace(tzinfo=None)}
            )


if __name__ == "__main__":
    unittest.main()
