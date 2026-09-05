from __future__ import annotations

import hashlib
import math
import re
import struct
from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field, field_validator, model_validator

from forge_core.hashing import canonical_sha256
from forge_core.models import ContractModel

MAX_STL_BYTES = 1_000_000
MAX_STL_TRIANGLES = 20_000
_ZERO_HASH = "sha256:" + "0" * 64
_FLOAT = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_FACET_RE = re.compile(
    rf"^facet\s+normal\s+({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})$",
    re.IGNORECASE,
)
_VERTEX_RE = re.compile(
    rf"^vertex\s+({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})$",
    re.IGNORECASE,
)


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


def _finite_float(value: float, field_name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    return value


class STLVertex(ContractModel):
    x: float
    y: float
    z: float

    @field_validator("x", "y", "z")
    @classmethod
    def coordinates_must_be_finite(cls, value: float) -> float:
        return _finite_float(value, "STL vertex coordinate")


class STLTriangle(ContractModel):
    normal: STLVertex
    vertices: tuple[STLVertex, STLVertex, STLVertex]
    attribute_byte_count: int = Field(ge=0, le=65535)


class STLBounds(ContractModel):
    minimum: STLVertex
    maximum: STLVertex

    @model_validator(mode="after")
    def minimum_must_not_exceed_maximum(self) -> STLBounds:
        if (
            self.minimum.x > self.maximum.x
            or self.minimum.y > self.maximum.y
            or self.minimum.z > self.maximum.z
        ):
            raise ValueError("STL bounds minimum must not exceed maximum")
        return self


class STLGeometry(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    geometry_format: Literal["stl-ascii", "stl-binary"]
    triangle_count: int = Field(ge=1, le=MAX_STL_TRIANGLES)
    bounds: STLBounds
    triangles: tuple[STLTriangle, ...] = Field(
        min_length=1, max_length=MAX_STL_TRIANGLES
    )
    geometry_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def triangle_count_and_hash_must_match(self) -> STLGeometry:
        if self.triangle_count != len(self.triangles):
            raise ValueError("STL triangle count does not match payload")
        if self.geometry_hash != stl_geometry_hash(
            self.model_copy(update={"geometry_hash": _ZERO_HASH})
        ):
            raise ValueError("STL geometry hash does not match payload")
        return self


class CADGeometryAsset(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    asset_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    project_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    source_uri: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    captured_at: datetime
    content_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    format: Literal["stl-ascii", "stl-binary"]
    triangle_count: int = Field(ge=1, le=MAX_STL_TRIANGLES)
    bounds: STLBounds
    triangles: tuple[STLTriangle, ...] = Field(
        min_length=1, max_length=MAX_STL_TRIANGLES
    )
    geometry_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    asset_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("captured_at")
    @classmethod
    def captured_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "captured_at")

    @model_validator(mode="after")
    def counts_and_hashes_must_match(self) -> CADGeometryAsset:
        if self.triangle_count != len(self.triangles):
            raise ValueError("CAD geometry triangle count does not match payload")
        if self.geometry_hash != cad_geometry_hash(
            self.model_copy(update={"geometry_hash": _ZERO_HASH})
        ):
            raise ValueError("CAD geometry hash does not match payload")
        if self.asset_hash != stl_asset_hash(
            self.model_copy(update={"asset_hash": _ZERO_HASH})
        ):
            raise ValueError("CAD geometry asset hash does not match payload")
        return self


class StoredCADGeometryAsset(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    asset: CADGeometryAsset
    stored_at: datetime

    @field_validator("stored_at")
    @classmethod
    def stored_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "stored_at")

    @model_validator(mode="after")
    def identity_must_match(self) -> StoredCADGeometryAsset:
        if (
            self.project_id != self.asset.project_id
            or self.asset_id != self.asset.asset_id
        ):
            raise ValueError("stored CAD geometry identity does not match asset")
        return self


def bytes_sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def stl_geometry_hash(geometry: STLGeometry) -> str:
    return canonical_sha256(geometry.model_copy(update={"geometry_hash": _ZERO_HASH}))


def cad_geometry_hash(asset: CADGeometryAsset) -> str:
    geometry = STLGeometry.model_construct(
        schema_version="1.0.0",
        geometry_format=asset.format,
        triangle_count=asset.triangle_count,
        bounds=asset.bounds,
        triangles=asset.triangles,
        geometry_hash=_ZERO_HASH,
    )
    return stl_geometry_hash(geometry)


def stl_asset_hash(asset: CADGeometryAsset) -> str:
    return canonical_sha256(asset.model_copy(update={"asset_hash": _ZERO_HASH}))


def parse_stl_asset(
    *,
    asset_id: str,
    project_id: str,
    tenant_id: str,
    source_uri: str,
    source_version: str,
    captured_at: datetime,
    content: bytes,
    max_bytes: int = MAX_STL_BYTES,
    max_triangles: int = MAX_STL_TRIANGLES,
) -> CADGeometryAsset:
    if not content:
        raise ValueError("STL payload is empty")
    if max_bytes < 1 or max_bytes > MAX_STL_BYTES:
        raise ValueError("STL byte bound is outside the supported range")
    if len(content) > max_bytes:
        raise ValueError("STL payload exceeds byte bound")
    if max_triangles < 1 or max_triangles > MAX_STL_TRIANGLES:
        raise ValueError("STL triangle bound is outside the supported range")

    geometry = _parse_stl_payload(content, max_triangles=max_triangles)
    draft = CADGeometryAsset.model_construct(
        schema_version="1.0.0",
        asset_id=asset_id,
        project_id=project_id,
        tenant_id=tenant_id,
        source_uri=source_uri,
        source_version=source_version,
        captured_at=captured_at,
        content_sha256=bytes_sha256(content),
        format=geometry.geometry_format,
        triangle_count=geometry.triangle_count,
        bounds=geometry.bounds,
        triangles=geometry.triangles,
        geometry_hash=geometry.geometry_hash,
        asset_hash=_ZERO_HASH,
    )
    return CADGeometryAsset.model_validate(
        draft.model_copy(update={"asset_hash": stl_asset_hash(draft)}).model_dump()
    )


def _parse_stl_payload(payload: bytes, *, max_triangles: int) -> STLGeometry:
    stripped = payload.lstrip()
    if stripped[:5].lower() == b"solid":
        try:
            return _parse_ascii_stl(payload, max_triangles=max_triangles)
        except UnicodeDecodeError as exc:
            raise ValueError("ASCII STL payload is not valid UTF-8") from exc
        except ValueError:
            if _has_binary_shape(payload):
                return _parse_binary_stl(payload, max_triangles=max_triangles)
            raise

    if len(payload) >= 84:
        return _parse_binary_stl(payload, max_triangles=max_triangles)
    raise ValueError("STL payload is neither binary STL nor strict ASCII STL")


def _has_binary_shape(payload: bytes) -> bool:
    if len(payload) < 84:
        return False
    triangle_count = struct.unpack_from("<I", payload, 80)[0]
    return bool(len(payload) == 84 + triangle_count * 50)


def _parse_binary_stl(payload: bytes, *, max_triangles: int) -> STLGeometry:
    if len(payload) < 84:
        raise ValueError("binary STL payload is too short")
    triangle_count = struct.unpack_from("<I", payload, 80)[0]
    expected_length = 84 + triangle_count * 50
    if len(payload) != expected_length:
        raise ValueError("binary STL payload length does not match triangle count")
    if triangle_count < 1:
        raise ValueError("binary STL must contain at least one triangle")
    if triangle_count > max_triangles:
        raise ValueError("binary STL exceeds triangle bound")

    triangles: list[STLTriangle] = []
    offset = 84
    for _ in range(triangle_count):
        values = struct.unpack_from("<12fH", payload, offset)
        offset += 50
        normal = _vertex_from_values((values[0], values[1], values[2]))
        vertices = (
            _vertex_from_values((values[3], values[4], values[5])),
            _vertex_from_values((values[6], values[7], values[8])),
            _vertex_from_values((values[9], values[10], values[11])),
        )
        triangles.append(
            STLTriangle(
                normal=normal,
                vertices=vertices,
                attribute_byte_count=values[12],
            )
        )
    return _build_geometry("stl-binary", tuple(triangles))


def _parse_ascii_stl(payload: bytes, *, max_triangles: int) -> STLGeometry:
    text = payload.decode("utf-8")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 7 or not lines[0].casefold().startswith("solid"):
        raise ValueError("ASCII STL must start with solid and contain one facet")
    if not lines[-1].casefold().startswith("endsolid"):
        raise ValueError("ASCII STL must end with endsolid")

    triangles: list[STLTriangle] = []
    index = 1
    while index < len(lines) - 1:
        facet = _FACET_RE.fullmatch(lines[index])
        if facet is None:
            raise ValueError("ASCII STL facet normal is malformed")
        normal = _vertex_from_strings((facet.group(1), facet.group(2), facet.group(3)))
        index += 1
        if index >= len(lines) - 1 or lines[index].casefold() != "outer loop":
            raise ValueError("ASCII STL facet is missing outer loop")
        index += 1
        vertices: list[STLVertex] = []
        for _ in range(3):
            if index >= len(lines) - 1:
                raise ValueError("ASCII STL facet is missing vertices")
            vertex = _VERTEX_RE.fullmatch(lines[index])
            if vertex is None:
                raise ValueError("ASCII STL vertex is malformed")
            vertices.append(
                _vertex_from_strings(
                    (vertex.group(1), vertex.group(2), vertex.group(3))
                )
            )
            index += 1
        if index >= len(lines) - 1 or lines[index].casefold() != "endloop":
            raise ValueError("ASCII STL facet is missing endloop")
        index += 1
        if index >= len(lines) - 1 or lines[index].casefold() != "endfacet":
            raise ValueError("ASCII STL facet is missing endfacet")
        index += 1
        if len(triangles) >= max_triangles:
            raise ValueError("ASCII STL exceeds triangle bound")
        triangles.append(
            STLTriangle(
                normal=normal,
                vertices=(vertices[0], vertices[1], vertices[2]),
                attribute_byte_count=0,
            )
        )

    if not triangles:
        raise ValueError("ASCII STL must contain at least one triangle")
    return _build_geometry("stl-ascii", tuple(triangles))


def _vertex_from_strings(values: tuple[str, str, str]) -> STLVertex:
    return _vertex_from_values((float(values[0]), float(values[1]), float(values[2])))


def _vertex_from_values(values: tuple[float, float, float]) -> STLVertex:
    return STLVertex(x=values[0], y=values[1], z=values[2])


def _build_geometry(
    geometry_format: Literal["stl-ascii", "stl-binary"],
    triangles: tuple[STLTriangle, ...],
) -> STLGeometry:
    bounds = _calculate_bounds(triangles)
    draft = STLGeometry.model_construct(
        schema_version="1.0.0",
        geometry_format=geometry_format,
        triangle_count=len(triangles),
        bounds=bounds,
        triangles=triangles,
        geometry_hash=_ZERO_HASH,
    )
    return STLGeometry.model_validate(
        draft.model_copy(
            update={"geometry_hash": stl_geometry_hash(draft)}
        ).model_dump()
    )


def _calculate_bounds(triangles: tuple[STLTriangle, ...]) -> STLBounds:
    vertices = [vertex for triangle in triangles for vertex in triangle.vertices]
    if not vertices:
        raise ValueError("STL geometry has no vertices")
    return STLBounds(
        minimum=STLVertex(
            x=min(vertex.x for vertex in vertices),
            y=min(vertex.y for vertex in vertices),
            z=min(vertex.z for vertex in vertices),
        ),
        maximum=STLVertex(
            x=max(vertex.x for vertex in vertices),
            y=max(vertex.y for vertex in vertices),
            z=max(vertex.z for vertex in vertices),
        ),
    )
