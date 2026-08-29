from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Literal, Protocol

from pydantic import Field, field_validator, model_validator

from forge_core.change_management import (
    ConnectorSnapshot,
    ExternalArtifactRef,
    SourceSystem,
)
from forge_core.constraints import InterfaceContract
from forge_core.hashing import canonical_sha256
from forge_core.impact_engine import (
    NormalizedInterfaceProjection,
    connector_snapshot_hash,
    interface_projection_hash,
)
from forge_core.models import ContractModel

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_CONNECTOR_SOURCES = {
    SourceSystem.CAD,
    SourceSystem.PLM,
    SourceSystem.GIT,
    SourceSystem.CI,
}


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


class _ManifestPayload(ContractModel):
    adapter_id: str
    adapter_version: str
    adapter_artifact_hash: str
    source_system: SourceSystem
    capabilities: tuple[str, ...]
    read_only: bool
    network_access: bool
    subprocess_access: bool
    filesystem_write: bool


class ReadOnlyAdapterManifest(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    adapter_artifact_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_system: SourceSystem
    capabilities: tuple[Literal["snapshot.read", "interface.read"], ...]
    read_only: Literal[True] = True
    network_access: Literal[False] = False
    subprocess_access: Literal[False] = False
    filesystem_write: Literal[False] = False
    manifest_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("adapter_id")
    @classmethod
    def adapter_id_must_be_opaque(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("adapter_id must be an opaque safe identifier")
        return value

    @field_validator("capabilities")
    @classmethod
    def capabilities_must_be_canonical(
        cls,
        value: tuple[Literal["snapshot.read", "interface.read"], ...],
    ) -> tuple[Literal["snapshot.read", "interface.read"], ...]:
        if not value or len(value) != len(set(value)) or value != tuple(sorted(value)):
            raise ValueError("adapter capabilities must be unique and ordered")
        if "snapshot.read" not in value:
            raise ValueError("connector adapter must support snapshot.read")
        return value

    @model_validator(mode="after")
    def manifest_must_be_read_only_and_hash_bound(self) -> ReadOnlyAdapterManifest:
        if self.source_system not in _CONNECTOR_SOURCES:
            raise ValueError("R0 connector source must be CAD, PLM, Git, or CI")
        payload = _ManifestPayload(
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            adapter_artifact_hash=self.adapter_artifact_hash,
            source_system=self.source_system,
            capabilities=self.capabilities,
            read_only=self.read_only,
            network_access=self.network_access,
            subprocess_access=self.subprocess_access,
            filesystem_write=self.filesystem_write,
        )
        if self.manifest_hash != canonical_sha256(payload):
            raise ValueError("manifest_hash does not reproduce adapter permissions")
        return self


def adapter_manifest_hash(
    *,
    adapter_id: str,
    adapter_version: str,
    adapter_artifact_hash: str,
    source_system: SourceSystem,
    capabilities: tuple[str, ...],
) -> str:
    return canonical_sha256(
        _ManifestPayload(
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            adapter_artifact_hash=adapter_artifact_hash,
            source_system=source_system,
            capabilities=capabilities,
            read_only=True,
            network_access=False,
            subprocess_access=False,
            filesystem_write=False,
        )
    )


class ConnectorCaptureRequest(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    connector_id: str = Field(min_length=1)
    capture_key: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    hardware_revision_id: str = Field(min_length=1)
    captured_at: datetime

    @field_validator(
        "connector_id",
        "capture_key",
        "project_id",
        "snapshot_id",
        "hardware_revision_id",
    )
    @classmethod
    def identifiers_must_not_be_paths(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("connector request identifiers must be opaque and safe")
        return value

    @field_validator("captured_at")
    @classmethod
    def capture_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "captured_at")


class _CapturePayload(ContractModel):
    manifest: ReadOnlyAdapterManifest
    request: ConnectorCaptureRequest
    snapshot: ConnectorSnapshot
    projections: tuple[NormalizedInterfaceProjection, ...]
    snapshot_hash: str


class ConnectorCaptureBundle(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    manifest: ReadOnlyAdapterManifest
    request: ConnectorCaptureRequest
    snapshot: ConnectorSnapshot
    projections: tuple[NormalizedInterfaceProjection, ...] = ()
    snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    capture_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def bundle_must_bind_source_request_and_projection(
        self,
    ) -> ConnectorCaptureBundle:
        if (
            self.request.connector_id != self.manifest.adapter_id
            or self.snapshot.project_id != self.request.project_id
            or self.snapshot.snapshot_id != self.request.snapshot_id
            or self.snapshot.hardware_revision_id != self.request.hardware_revision_id
            or self.snapshot.captured_at != self.request.captured_at
        ):
            raise ValueError("connector capture identity does not match request")
        if any(
            item.source_system is not self.manifest.source_system
            for item in self.snapshot.artifacts
        ):
            raise ValueError("connector capture contains another source system")
        if self.snapshot_hash != connector_snapshot_hash(self.snapshot):
            raise ValueError("snapshot_hash does not reproduce connector capture")
        domains = [item.source_ref.domain for item in self.projections]
        if len(domains) != len(set(domains)):
            raise ValueError("connector projections must be unique by domain")
        if self.projections != tuple(
            sorted(self.projections, key=lambda item: item.projection_hash)
        ):
            raise ValueError("connector projections must be canonically ordered")
        if self.projections and "interface.read" not in self.manifest.capabilities:
            raise ValueError("interface projections require interface.read capability")
        if any(
            item.snapshot_id != self.snapshot.snapshot_id
            or item.source_ref not in self.snapshot.artifacts
            for item in self.projections
        ):
            raise ValueError("connector projection is not bound to captured source")
        payload = _CapturePayload(
            manifest=self.manifest,
            request=self.request,
            snapshot=self.snapshot,
            projections=self.projections,
            snapshot_hash=self.snapshot_hash,
        )
        if self.capture_hash != canonical_sha256(payload):
            raise ValueError("capture_hash does not reproduce connector bundle")
        return self


def connector_capture_hash(
    manifest: ReadOnlyAdapterManifest,
    request: ConnectorCaptureRequest,
    snapshot: ConnectorSnapshot,
    projections: tuple[NormalizedInterfaceProjection, ...],
    snapshot_hash: str,
) -> str:
    return canonical_sha256(
        _CapturePayload(
            manifest=manifest,
            request=request,
            snapshot=snapshot,
            projections=projections,
            snapshot_hash=snapshot_hash,
        )
    )


class ReadOnlyConnectorAdapter(Protocol):
    @property
    def manifest(self) -> ReadOnlyAdapterManifest: ...

    def capture(self, request: ConnectorCaptureRequest) -> ConnectorCaptureBundle: ...


class ConnectorRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ReadOnlyConnectorAdapter] = {}
        self._manifests: dict[str, ReadOnlyAdapterManifest] = {}

    def register(self, adapter: ReadOnlyConnectorAdapter) -> None:
        manifest = ReadOnlyAdapterManifest.model_validate(
            adapter.manifest.model_dump(mode="python")
        )
        if manifest.adapter_id in self._adapters:
            raise ValueError("connector adapter is already registered")
        self._adapters[manifest.adapter_id] = adapter
        self._manifests[manifest.adapter_id] = manifest

    def manifest(self, adapter_id: str) -> ReadOnlyAdapterManifest:
        try:
            return self._manifests[adapter_id]
        except KeyError as exc:
            raise KeyError("unknown connector adapter") from exc

    def capture(self, request: ConnectorCaptureRequest) -> ConnectorCaptureBundle:
        try:
            adapter = self._adapters[request.connector_id]
            registered = self._manifests[request.connector_id]
        except KeyError as exc:
            raise KeyError("unknown connector adapter") from exc
        current_manifest = ReadOnlyAdapterManifest.model_validate(
            adapter.manifest.model_dump(mode="python")
        )
        if current_manifest != registered:
            raise ValueError("registered connector manifest changed")
        bundle = ConnectorCaptureBundle.model_validate(
            adapter.capture(request).model_dump(mode="python")
        )
        if bundle.manifest != registered or bundle.request != request:
            raise ValueError("connector returned an unbound capture bundle")
        return bundle


class InMemoryReadOnlyAdapter:
    def __init__(
        self,
        manifest: ReadOnlyAdapterManifest,
        captures: Mapping[str, tuple[ExternalArtifactRef, ...]],
        interface_contracts: Mapping[str, InterfaceContract] | None = None,
    ) -> None:
        self._manifest = manifest
        self._captures = MappingProxyType(
            {
                key: tuple(sorted(value, key=lambda item: item.artifact_id))
                for key, value in captures.items()
            }
        )
        self._contracts = MappingProxyType(dict(interface_contracts or {}))

    @property
    def manifest(self) -> ReadOnlyAdapterManifest:
        return self._manifest

    def capture(self, request: ConnectorCaptureRequest) -> ConnectorCaptureBundle:
        if request.connector_id != self.manifest.adapter_id:
            raise ValueError("capture request targets another connector")
        try:
            artifacts = self._captures[request.capture_key]
        except KeyError as exc:
            raise KeyError("unknown connector capture key") from exc
        snapshot = ConnectorSnapshot(
            snapshot_id=request.snapshot_id,
            project_id=request.project_id,
            hardware_revision_id=request.hardware_revision_id,
            captured_at=request.captured_at,
            artifacts=artifacts,
        )
        projections = tuple(
            sorted(
                (
                    NormalizedInterfaceProjection(
                        snapshot_id=snapshot.snapshot_id,
                        source_ref=source,
                        normalizer_version=self.manifest.adapter_version,
                        contract=self._contracts[source.artifact_id],
                        projection_hash=interface_projection_hash(
                            snapshot.snapshot_id,
                            source,
                            self.manifest.adapter_version,
                            self._contracts[source.artifact_id],
                        ),
                    )
                    for source in artifacts
                    if source.artifact_id in self._contracts
                ),
                key=lambda item: item.projection_hash,
            )
        )
        snapshot_hash = connector_snapshot_hash(snapshot)
        return ConnectorCaptureBundle(
            manifest=self.manifest,
            request=request,
            snapshot=snapshot,
            projections=projections,
            snapshot_hash=snapshot_hash,
            capture_hash=connector_capture_hash(
                self.manifest,
                request,
                snapshot,
                projections,
                snapshot_hash,
            ),
        )


__all__ = [
    "ConnectorCaptureBundle",
    "ConnectorCaptureRequest",
    "ConnectorRegistry",
    "InMemoryReadOnlyAdapter",
    "ReadOnlyAdapterManifest",
    "ReadOnlyConnectorAdapter",
    "adapter_manifest_hash",
    "connector_capture_hash",
]
