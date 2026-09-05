from __future__ import annotations

import struct
import unittest
from datetime import UTC, datetime
from typing import cast

from pydantic import ValidationError

from forge_core.cad_geometry import (
    MAX_STL_BYTES,
    CADGeometryAsset,
    parse_stl_asset,
)

NOW = datetime(2026, 9, 3, 12, tzinfo=UTC)


def ascii_stl() -> bytes:
    return b"""solid bracket
facet normal 0 0 1
  outer loop
    vertex 0 0 0
    vertex 1 0 0
    vertex 0 1 0
  endloop
endfacet
endsolid bracket
"""


def binary_stl(*, normal_z: float = 1.0, declared_count: int = 1) -> bytes:
    header = b"FORGE binary STL".ljust(80, b"\x00")
    record = struct.pack(
        "<12fH",
        0.0,
        0.0,
        normal_z,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        7,
    )
    return header + struct.pack("<I", declared_count) + record


class CADGeometryTests(unittest.TestCase):
    def parse(self, payload: bytes, **overrides: object) -> CADGeometryAsset:
        params: dict[str, object] = {
            "asset_id": "upper-arm-mesh",
            "tenant_id": "local-org",
            "project_id": "robot-arm",
            "source_uri": "file:///upper-arm.stl",
            "source_version": "rev-a",
            "captured_at": NOW,
            "content": payload,
        }
        params.update(overrides)
        return parse_stl_asset(
            asset_id=str(params["asset_id"]),
            tenant_id=str(params["tenant_id"]),
            project_id=str(params["project_id"]),
            source_uri=str(params["source_uri"]),
            source_version=str(params["source_version"]),
            captured_at=cast(datetime, params["captured_at"]),
            content=cast(bytes, params["content"]),
            max_bytes=cast(int, params.get("max_bytes", MAX_STL_BYTES)),
            max_triangles=cast(int, params.get("max_triangles", 20_000)),
        )

    def test_ascii_stl_is_source_bound_and_hash_checked(self) -> None:
        asset = self.parse(ascii_stl())
        self.assertEqual(asset.format, "stl-ascii")
        self.assertEqual(asset.triangle_count, 1)
        self.assertEqual(asset.content_sha256[:7], "sha256:")
        self.assertEqual(asset.bounds.maximum.x, 1)

        with self.assertRaises(ValidationError):
            CADGeometryAsset.model_validate(
                asset.model_copy(
                    update={"asset_hash": "sha256:" + "1" * 64}
                ).model_dump()
            )

    def test_binary_stl_is_exact_length_and_preserves_attribute_count(self) -> None:
        asset = self.parse(binary_stl())
        triangle = asset.triangles[0]
        self.assertEqual(asset.format, "stl-binary")
        self.assertEqual(triangle.attribute_byte_count, 7)
        self.assertEqual(asset.bounds.minimum.z, 0)

    def test_binary_stl_rejects_trailing_or_missing_bytes(self) -> None:
        with self.assertRaisesRegex(ValueError, "length"):
            self.parse(binary_stl() + b"\x00")
        with self.assertRaisesRegex(ValueError, "length"):
            self.parse(binary_stl()[:-1])

    def test_ascii_stl_rejects_malformed_structure(self) -> None:
        with self.assertRaisesRegex(ValueError, "endloop"):
            self.parse(ascii_stl().replace(b"endloop", b"endfacet", 1))

    def test_stl_rejects_non_finite_coordinates(self) -> None:
        with self.assertRaises(ValidationError):
            self.parse(binary_stl(normal_z=float("nan")))

    def test_stl_respects_byte_and_triangle_bounds(self) -> None:
        with self.assertRaisesRegex(ValueError, "byte bound"):
            self.parse(ascii_stl(), max_bytes=len(ascii_stl()) - 1)
        with self.assertRaisesRegex(ValueError, "triangle bound"):
            self.parse(binary_stl(), max_triangles=0)
        with self.assertRaisesRegex(ValueError, "byte bound"):
            self.parse(b"x" * (MAX_STL_BYTES + 1))

    def test_source_metadata_requires_utc_timestamp(self) -> None:
        with self.assertRaises(ValidationError):
            self.parse(ascii_stl(), captured_at=datetime(2026, 9, 3, 12))


if __name__ == "__main__":
    unittest.main()
