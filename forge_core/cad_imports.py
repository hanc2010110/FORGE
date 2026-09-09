from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import Field, model_validator

from forge_core.cad_geometry import (
    CADGeometryAsset,
    STEPGeometrySummary,
    bytes_sha256,
    parse_step_summary,
    parse_stl_asset,
)
from forge_core.hashing import canonical_sha256
from forge_core.models import ContractModel

MAX_CAD_IMPORT_BYTES = 4_000_000
MAX_SOLIDWORKS_EXPORT_FILES = 16
MAX_SOLIDWORKS_MEMBER_BYTES = 2_000_000
_ZERO_HASH = "sha256:" + "0" * 64
_STEP_EXTENSIONS = {".step", ".stp"}
_STL_EXTENSIONS = {".stl"}
_SOLIDWORKS_EXTENSIONS = {".sldprt", ".sldasm"}


class CADImportKind(StrEnum):
    STEP_SUMMARY = "step_summary"
    STL_GEOMETRY = "stl_geometry"
    SOLIDWORKS_EXPORT_PACKAGE = "solidworks_export_package"


class ImportedCADAsset(ContractModel):
    schema_version: str = "1.0.0"
    kind: CADImportKind
    asset_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    source_name: str = Field(min_length=1)
    source_uri: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    captured_at: datetime
    source_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    stl_asset: CADGeometryAsset | None = None
    step_summary: STEPGeometrySummary | None = None
    import_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exactly_one_payload_and_hash_must_match(self) -> ImportedCADAsset:
        populated = [self.stl_asset is not None, self.step_summary is not None]
        if populated.count(True) != 1:
            raise ValueError("imported CAD asset must contain one parsed payload")
        if self.import_hash != imported_cad_asset_hash(
            self.model_copy(update={"import_hash": _ZERO_HASH})
        ):
            raise ValueError("imported CAD asset hash does not match payload")
        return self


class SolidWorksExportPackage(ContractModel):
    schema_version: str = "1.0.0"
    kind: CADImportKind = CADImportKind.SOLIDWORKS_EXPORT_PACKAGE
    asset_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    source_uri: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    captured_at: datetime
    manifest: Mapping[str, object]
    assets: tuple[ImportedCADAsset, ...] = Field(min_length=1)
    package_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    package_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def package_hash_must_match(self) -> SolidWorksExportPackage:
        if self.package_hash != solidworks_export_package_hash(
            self.model_copy(update={"package_hash": _ZERO_HASH})
        ):
            raise ValueError("SolidWorks export package hash does not match payload")
        return self


def imported_cad_asset_hash(asset: ImportedCADAsset) -> str:
    return canonical_sha256(asset.model_copy(update={"import_hash": _ZERO_HASH}))


def solidworks_export_package_hash(package: SolidWorksExportPackage) -> str:
    return canonical_sha256(package.model_copy(update={"package_hash": _ZERO_HASH}))


def import_cad_payload(
    *,
    asset_id: str,
    project_id: str,
    tenant_id: str,
    source_uri: str,
    source_version: str,
    captured_at: datetime,
    filename: str,
    content: bytes,
    max_bytes: int = MAX_CAD_IMPORT_BYTES,
) -> ImportedCADAsset:
    if not content:
        raise ValueError("CAD import payload is empty")
    if max_bytes < 1 or max_bytes > MAX_CAD_IMPORT_BYTES:
        raise ValueError("CAD import byte bound is outside the supported range")
    if len(content) > max_bytes:
        raise ValueError("CAD import payload exceeds byte bound")
    suffix = PurePosixPath(filename).suffix.casefold()
    if suffix in _SOLIDWORKS_EXTENSIONS:
        raise ValueError(
            "Native SolidWorks files require a STEP/STL export package; "
            "FORGE does not claim native SLDPRT/SLDASM parsing."
        )
    if suffix in _STEP_EXTENSIONS:
        summary = parse_step_summary(
            asset_id=asset_id,
            project_id=project_id,
            tenant_id=tenant_id,
            source_uri=source_uri,
            source_version=source_version,
            captured_at=captured_at,
            content=content,
        )
        return _build_imported_asset(
            kind=CADImportKind.STEP_SUMMARY,
            asset_id=asset_id,
            project_id=project_id,
            tenant_id=tenant_id,
            source_name=filename,
            source_uri=source_uri,
            source_version=source_version,
            captured_at=captured_at,
            source_sha256=bytes_sha256(content),
            step_summary=summary,
        )
    if suffix in _STL_EXTENSIONS:
        asset = parse_stl_asset(
            asset_id=asset_id,
            project_id=project_id,
            tenant_id=tenant_id,
            source_uri=source_uri,
            source_version=source_version,
            captured_at=captured_at,
            content=content,
        )
        return _build_imported_asset(
            kind=CADImportKind.STL_GEOMETRY,
            asset_id=asset_id,
            project_id=project_id,
            tenant_id=tenant_id,
            source_name=filename,
            source_uri=source_uri,
            source_version=source_version,
            captured_at=captured_at,
            source_sha256=bytes_sha256(content),
            stl_asset=asset,
        )
    raise ValueError("CAD import supports STEP, STL, or SolidWorks export ZIP only")


def import_solidworks_export_package(
    *,
    asset_id: str,
    project_id: str,
    tenant_id: str,
    source_uri: str,
    source_version: str,
    captured_at: datetime,
    content: bytes,
    max_bytes: int = MAX_CAD_IMPORT_BYTES,
    max_files: int = MAX_SOLIDWORKS_EXPORT_FILES,
    max_member_bytes: int = MAX_SOLIDWORKS_MEMBER_BYTES,
) -> SolidWorksExportPackage:
    if not content:
        raise ValueError("SolidWorks export package is empty")
    if max_bytes < 1 or max_bytes > MAX_CAD_IMPORT_BYTES:
        raise ValueError("SolidWorks export package byte bound is invalid")
    if len(content) > max_bytes:
        raise ValueError("SolidWorks export package exceeds byte bound")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ValueError("SolidWorks export package must be a ZIP archive") from exc
    with archive:
        infos = tuple(info for info in archive.infolist() if not info.is_dir())
        if not 1 <= len(infos) <= max_files:
            raise ValueError("SolidWorks export package file count is outside bounds")
        _validate_zip_infos(infos, max_member_bytes=max_member_bytes)
        manifest = _read_manifest(archive, infos)
        parsed: list[ImportedCADAsset] = []
        for info in sorted(infos, key=lambda item: item.filename.casefold()):
            if PurePosixPath(info.filename).name == "manifest.json":
                continue
            suffix = PurePosixPath(info.filename).suffix.casefold()
            if suffix not in _STEP_EXTENSIONS | _STL_EXTENSIONS:
                continue
            member = archive.read(info)
            parsed.append(
                import_cad_payload(
                    asset_id=f"{asset_id}-{len(parsed) + 1}",
                    project_id=project_id,
                    tenant_id=tenant_id,
                    source_uri=f"{source_uri}#{info.filename}",
                    source_version=source_version,
                    captured_at=captured_at,
                    filename=info.filename,
                    content=member,
                    max_bytes=max_member_bytes,
                )
            )
    if not parsed:
        raise ValueError("SolidWorks export package must include STEP or STL members")
    draft = SolidWorksExportPackage.model_construct(
        schema_version="1.0.0",
        kind=CADImportKind.SOLIDWORKS_EXPORT_PACKAGE,
        asset_id=asset_id,
        project_id=project_id,
        tenant_id=tenant_id,
        source_uri=source_uri,
        source_version=source_version,
        captured_at=captured_at,
        manifest=manifest,
        assets=tuple(parsed),
        package_sha256=bytes_sha256(content),
        package_hash=_ZERO_HASH,
    )
    return SolidWorksExportPackage.model_validate(
        draft.model_copy(
            update={"package_hash": solidworks_export_package_hash(draft)}
        ).model_dump()
    )


def _build_imported_asset(
    *,
    kind: CADImportKind,
    asset_id: str,
    project_id: str,
    tenant_id: str,
    source_name: str,
    source_uri: str,
    source_version: str,
    captured_at: datetime,
    source_sha256: str,
    stl_asset: CADGeometryAsset | None = None,
    step_summary: STEPGeometrySummary | None = None,
) -> ImportedCADAsset:
    draft = ImportedCADAsset.model_construct(
        schema_version="1.0.0",
        kind=kind,
        asset_id=asset_id,
        project_id=project_id,
        tenant_id=tenant_id,
        source_name=source_name,
        source_uri=source_uri,
        source_version=source_version,
        captured_at=captured_at,
        source_sha256=source_sha256,
        stl_asset=stl_asset,
        step_summary=step_summary,
        import_hash=_ZERO_HASH,
    )
    return ImportedCADAsset.model_validate(
        draft.model_copy(
            update={"import_hash": imported_cad_asset_hash(draft)}
        ).model_dump()
    )


def _validate_zip_infos(
    infos: tuple[zipfile.ZipInfo, ...], *, max_member_bytes: int
) -> None:
    seen: set[str] = set()
    total_uncompressed = 0
    for info in infos:
        path = PurePosixPath(info.filename)
        normalized = path.as_posix()
        if path.is_absolute() or ".." in path.parts or normalized.startswith("/"):
            raise ValueError("SolidWorks export package contains path traversal")
        key = normalized.casefold()
        if key in seen:
            raise ValueError("SolidWorks export package contains duplicate members")
        seen.add(key)
        if info.flag_bits & 0x1:
            raise ValueError("SolidWorks export package contains encrypted members")
        if info.file_size > max_member_bytes:
            raise ValueError("SolidWorks export package member exceeds byte bound")
        total_uncompressed += info.file_size
        if total_uncompressed > MAX_CAD_IMPORT_BYTES:
            raise ValueError("SolidWorks export package expands beyond byte bound")


def _read_manifest(
    archive: zipfile.ZipFile, infos: tuple[zipfile.ZipInfo, ...]
) -> Mapping[str, object]:
    matches = [
        info
        for info in infos
        if PurePosixPath(info.filename).name.casefold() == "manifest.json"
    ]
    if not matches:
        return {"source_system": "solidworks", "manifest": "absent"}
    if len(matches) > 1:
        raise ValueError("SolidWorks export package contains duplicate manifest")
    try:
        decoded = json.loads(archive.read(matches[0]).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("SolidWorks export package manifest must be JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("SolidWorks export package manifest must be an object")
    return decoded


__all__ = [
    "CADImportKind",
    "ImportedCADAsset",
    "SolidWorksExportPackage",
    "import_cad_payload",
    "import_solidworks_export_package",
]
