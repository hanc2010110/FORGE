from __future__ import annotations

import io
import unittest
import zipfile
from datetime import UTC, datetime

from forge_core.cad_imports import (
    CADImportKind,
    import_cad_payload,
    import_solidworks_export_package,
)

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)


STEP_FIXTURE = b"""ISO-10303-21;
HEADER;
FILE_SCHEMA(('AUTOMOTIVE_DESIGN_CC2'));
ENDSEC;
DATA;
#1=PRODUCT('UPPER_ARM','Upper Arm','',(#2));
#20=CARTESIAN_POINT('',(0.,0.,0.));
#21=CARTESIAN_POINT('',(100.,20.,10.));
ENDSEC;
END-ISO-10303-21;
"""


STL_FIXTURE = b"""solid bracket
facet normal 0 0 1
  outer loop
    vertex 0 0 0
    vertex 1 0 0
    vertex 0 1 0
  endloop
endfacet
endsolid bracket
"""


def sw_export_zip(*, extra: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            b'{"source_system":"solidworks","export_version":"2026-sp1"}',
        )
        archive.writestr("upper_arm.step", STEP_FIXTURE)
        archive.writestr("upper_arm.stl", STL_FIXTURE)
        for name, payload in (extra or {}).items():
            archive.writestr(name, payload)
    return buffer.getvalue()


class CADImportTests(unittest.TestCase):
    def test_imports_step_as_source_bound_asset(self) -> None:
        result = import_cad_payload(
            asset_id="upper-arm-step",
            project_id="robot-arm",
            tenant_id="local-org",
            source_uri="file:///upper_arm.step",
            source_version="rev-c",
            captured_at=NOW,
            filename="upper_arm.step",
            content=STEP_FIXTURE,
        )
        self.assertEqual(result.kind, CADImportKind.STEP_SUMMARY)
        step_summary = result.step_summary
        if step_summary is None:
            raise AssertionError("expected STEP summary")
        self.assertEqual(result.source_sha256[:7], "sha256:")
        self.assertEqual(result.source_version, "rev-c")
        self.assertEqual(step_summary.product_names, ("UPPER_ARM",))

    def test_imports_stl_as_source_bound_asset(self) -> None:
        result = import_cad_payload(
            asset_id="upper-arm-stl",
            project_id="robot-arm",
            tenant_id="local-org",
            source_uri="file:///upper_arm.stl",
            source_version="rev-c",
            captured_at=NOW,
            filename="upper_arm.stl",
            content=STL_FIXTURE,
        )
        self.assertEqual(result.kind, CADImportKind.STL_GEOMETRY)
        stl_asset = result.stl_asset
        if stl_asset is None:
            raise AssertionError("expected STL asset")
        self.assertEqual(stl_asset.triangle_count, 1)

    def test_solidworks_native_requires_export_package(self) -> None:
        with self.assertRaisesRegex(ValueError, "export package"):
            import_cad_payload(
                asset_id="native",
                project_id="robot-arm",
                tenant_id="local-org",
                source_uri="file:///arm.SLDPRT",
                source_version="rev-c",
                captured_at=NOW,
                filename="arm.SLDPRT",
                content=b"not parseable",
            )

    def test_imports_solidworks_export_package_with_hashes(self) -> None:
        package = import_solidworks_export_package(
            asset_id="sw-export",
            project_id="robot-arm",
            tenant_id="local-org",
            source_uri="file:///sw-export.zip",
            source_version="rev-d",
            captured_at=NOW,
            content=sw_export_zip(),
        )
        self.assertEqual(package.kind, CADImportKind.SOLIDWORKS_EXPORT_PACKAGE)
        self.assertEqual(package.manifest["source_system"], "solidworks")
        self.assertEqual(len(package.assets), 2)
        self.assertEqual(package.assets[0].source_name, "upper_arm.step")
        self.assertEqual(package.assets[1].source_name, "upper_arm.stl")
        self.assertEqual(package.package_sha256[:7], "sha256:")

    def test_solidworks_package_rejects_unsafe_zip_members(self) -> None:
        with self.assertRaisesRegex(ValueError, "path traversal"):
            import_solidworks_export_package(
                asset_id="sw-export",
                project_id="robot-arm",
                tenant_id="local-org",
                source_uri="file:///sw-export.zip",
                source_version="rev-d",
                captured_at=NOW,
                content=sw_export_zip(extra={"../evil.step": STEP_FIXTURE}),
            )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            import_solidworks_export_package(
                asset_id="sw-export",
                project_id="robot-arm",
                tenant_id="local-org",
                source_uri="file:///sw-export.zip",
                source_version="rev-d",
                captured_at=NOW,
                content=sw_export_zip(extra={"UPPER_ARM.STEP": STEP_FIXTURE}),
            )

    def test_import_rejects_empty_oversized_unknown_and_bad_zip_payloads(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty"):
            import_cad_payload(
                asset_id="bad",
                project_id="robot-arm",
                tenant_id="local-org",
                source_uri="file:///bad.step",
                source_version="rev-x",
                captured_at=NOW,
                filename="bad.step",
                content=b"",
            )
        with self.assertRaisesRegex(ValueError, "byte bound"):
            import_cad_payload(
                asset_id="bad",
                project_id="robot-arm",
                tenant_id="local-org",
                source_uri="file:///bad.step",
                source_version="rev-x",
                captured_at=NOW,
                filename="bad.step",
                content=STEP_FIXTURE,
                max_bytes=0,
            )
        with self.assertRaisesRegex(ValueError, "supports STEP"):
            import_cad_payload(
                asset_id="bad",
                project_id="robot-arm",
                tenant_id="local-org",
                source_uri="file:///bad.step",
                source_version="rev-x",
                captured_at=NOW,
                filename="bad.obj",
                content=b"obj",
            )
        with self.assertRaisesRegex(ValueError, "ZIP"):
            import_solidworks_export_package(
                asset_id="bad",
                project_id="robot-arm",
                tenant_id="local-org",
                source_uri="file:///bad.step",
                source_version="rev-x",
                captured_at=NOW,
                content=b"not zip",
            )

    def test_solidworks_package_absent_manifest_and_no_cad_paths(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("upper_arm.step", STEP_FIXTURE)
        package = import_solidworks_export_package(
            asset_id="sw-no-manifest",
            project_id="robot-arm",
            tenant_id="local-org",
            source_uri="file:///sw-no-manifest.zip",
            source_version="rev-d",
            captured_at=NOW,
            content=buffer.getvalue(),
        )
        self.assertEqual(package.manifest["manifest"], "absent")

        empty_cad = io.BytesIO()
        with zipfile.ZipFile(
            empty_cad, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("manifest.json", b'{"source_system":"solidworks"}')
            archive.writestr("notes.txt", b"ignored")
        with self.assertRaisesRegex(ValueError, "must include STEP or STL"):
            import_solidworks_export_package(
                asset_id="sw-no-cad",
                project_id="robot-arm",
                tenant_id="local-org",
                source_uri="file:///sw-no-cad.zip",
                source_version="rev-d",
                captured_at=NOW,
                content=empty_cad.getvalue(),
            )


if __name__ == "__main__":
    unittest.main()
