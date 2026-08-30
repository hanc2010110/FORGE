from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from forge_core.change_management import (
    ArtifactDomain,
    ConnectorSnapshot,
    EvidenceTier,
    ExternalArtifactRef,
    SourceSystem,
)
from forge_core.connectors import (
    ConnectorCaptureRequest,
    ConnectorRegistry,
    InMemoryReadOnlyAdapter,
    ReadOnlyAdapterManifest,
    adapter_manifest_hash,
)
from forge_core.constraints import (
    BOMLine,
    InterfaceContract,
    InterfaceSignal,
    QuoteSnapshot,
    evaluate_cost,
)
from forge_core.impact_engine import ChangeImpactAssessment
from forge_core.models import Quantity, SourceRef, Verdict
from forge_core.persistence import StoredCostEvaluation
from forge_core.release_persistence import RawReleaseEvidence
from forge_core.release_readiness import FirmwareBuildEvidence, TestExecutionEvidence
from forge_core.release_service import (
    AnalyzeChangeCommand,
    CaptureSnapshotCommand,
    CreateProjectCommand,
    EvaluateReleaseCommand,
    IngestReleaseEvidenceCommand,
    MutationContext,
    ReleaseIntegrationService,
)
from forge_core.sqlite_store import SQLiteEvidenceStore

NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)
PROJECT_ID = "controller-release-demo"
BUILD_HASH = "sha256:" + "b" * 64


def _artifact(
    artifact_id: str,
    domain: ArtifactDomain,
    source_system: SourceSystem,
    revision: str,
    digit: int,
    captured_at: datetime,
) -> ExternalArtifactRef:
    return ExternalArtifactRef(
        artifact_id=artifact_id,
        domain=domain,
        source_system=source_system,
        source_revision=revision,
        content_hash="sha256:" + str(digit) * 64,
        captured_at=captured_at,
    )


def _interface_contract() -> InterfaceContract:
    source = SourceRef(kind="user", identifier="forge-release-demo")
    return InterfaceContract(
        contract_id="controller-interface",
        protocol_schema_hash="sha256:" + "9" * 64,
        signals=(
            InterfaceSignal(
                name="fan_enable",
                pin="J3-4",
                direction="output",
                voltage_min=Quantity(
                    value=0, unit="V", dimension="voltage", source=source
                ),
                voltage_max=Quantity(
                    value=3.3, unit="V", dimension="voltage", source=source
                ),
                command_min=Quantity(
                    value=0, unit="%", dimension="duty", source=source
                ),
                command_max=Quantity(
                    value=100, unit="%", dimension="duty", source=source
                ),
                safe_value=Quantity(value=0, unit="%", dimension="duty", source=source),
            ),
        ),
    )


def _manifest(
    adapter_id: str, source_system: Literal[SourceSystem.PLM, SourceSystem.GIT]
) -> ReadOnlyAdapterManifest:
    capabilities: tuple[Literal["snapshot.read", "interface.read"], ...] = (
        "interface.read",
        "snapshot.read",
    )
    digit = "1" if source_system is SourceSystem.PLM else "2"
    artifact_hash = "sha256:" + digit * 64
    return ReadOnlyAdapterManifest(
        adapter_id=adapter_id,
        adapter_version="1.0.0",
        adapter_artifact_hash=artifact_hash,
        source_system=source_system,
        capabilities=capabilities,
        manifest_hash=adapter_manifest_hash(
            adapter_id=adapter_id,
            adapter_version="1.0.0",
            adapter_artifact_hash=artifact_hash,
            source_system=source_system,
            capabilities=capabilities,
        ),
    )


def _connector_registry() -> ConnectorRegistry:
    previous_at = NOW - timedelta(hours=2)
    current_at = NOW - timedelta(hours=1)
    old_hardware = _artifact(
        "controller-board",
        ArtifactDomain.HARDWARE,
        SourceSystem.PLM,
        "board-11",
        1,
        previous_at,
    )
    current_hardware = _artifact(
        "controller-board",
        ArtifactDomain.HARDWARE,
        SourceSystem.PLM,
        "board-12",
        2,
        current_at,
    )
    bom = _artifact(
        "controller-bom",
        ArtifactDomain.BOM,
        SourceSystem.PLM,
        "bom-12",
        4,
        previous_at,
    )
    firmware = _artifact(
        "controller-firmware",
        ArtifactDomain.FIRMWARE,
        SourceSystem.GIT,
        "commit-42",
        3,
        previous_at,
    )
    protocol = _artifact(
        "controller-protocol",
        ArtifactDomain.PROTOCOL,
        SourceSystem.GIT,
        "protocol-3",
        5,
        previous_at,
    )
    documentation = _artifact(
        "release-notes",
        ArtifactDomain.DOCUMENTATION,
        SourceSystem.GIT,
        "doc-12",
        6,
        previous_at,
    )
    contract = _interface_contract()
    registry = ConnectorRegistry()
    registry.register(
        InMemoryReadOnlyAdapter(
            _manifest("demo-git", SourceSystem.GIT),
            {
                "before": (firmware, protocol, documentation),
                "after": (firmware, protocol, documentation),
            },
            {
                firmware.artifact_id: contract,
                protocol.artifact_id: contract,
            },
        )
    )
    registry.register(
        InMemoryReadOnlyAdapter(
            _manifest("demo-plm", SourceSystem.PLM),
            {"before": (old_hardware, bom), "after": (current_hardware, bom)},
            {old_hardware.artifact_id: contract},
        )
    )
    return registry


def _capture_command(
    capture_key: str,
    snapshot_id: str,
    hardware_revision_id: str,
    captured_at: datetime,
) -> CaptureSnapshotCommand:
    return CaptureSnapshotCommand(
        captures=tuple(
            ConnectorCaptureRequest(
                connector_id=connector_id,
                capture_key=capture_key,
                project_id=PROJECT_ID,
                snapshot_id=snapshot_id,
                hardware_revision_id=hardware_revision_id,
                captured_at=captured_at,
            )
            for connector_id in ("demo-git", "demo-plm")
        )
    )


def _context(key: str, version: int | None) -> MutationContext:
    return MutationContext(
        local_installation_id="forge-release-demo",
        idempotency_key=key,
        expected_project_version=version,
    )


def _cost_evidence(snapshot: ConnectorSnapshot) -> StoredCostEvaluation:
    bom = (BOMLine(part_number="FAN-120", quantity=2),)
    quote = QuoteSnapshot(
        quote_id="quote-2026-08-29",
        part_number="FAN-120",
        supplier="Demo Supplier",
        region="KR",
        currency="USD",
        unit_price=Decimal("10.00"),
        minimum_quantity=1,
        observed_at=NOW - timedelta(hours=2),
        expires_at=NOW + timedelta(days=1),
        shipping_included=True,
        shipping_cost=None,
        tax_included=True,
        tax_cost=None,
        source_url="https://supplier.example/FAN-120",
        source_hash="sha256:" + "c" * 64,
    )
    evaluated_at = NOW - timedelta(hours=1)
    evaluation = evaluate_cost(
        bom,
        (quote,),
        currency="USD",
        budget_limit=Decimal("30.00"),
        reserve_rate=Decimal("0.10"),
        evaluated_at=evaluated_at,
    )
    bom_ref = next(
        artifact
        for artifact in snapshot.artifacts
        if artifact.domain is ArtifactDomain.BOM
    )
    return StoredCostEvaluation(
        project_id=PROJECT_ID,
        evidence_id="cost-current",
        revision_id=snapshot.hardware_revision_id,
        bom_artifact_hash=bom_ref.content_hash,
        dependency_hash="sha256:" + "d" * 64,
        bom=bom,
        quotes=(quote,),
        evaluated_at=evaluated_at,
        evaluation=evaluation,
    )


def _build_evidence(
    assessment: ChangeImpactAssessment, snapshot: ConnectorSnapshot
) -> FirmwareBuildEvidence:
    protocol_schema_hash = assessment.target_protocol_schema_hash
    if protocol_schema_hash is None:
        raise RuntimeError("release demo requires a target protocol schema")
    firmware = next(
        artifact
        for artifact in snapshot.artifacts
        if artifact.domain is ArtifactDomain.FIRMWARE
    )
    return FirmwareBuildEvidence(
        evidence_id="build-current",
        project_id=PROJECT_ID,
        hardware_revision_id=snapshot.hardware_revision_id,
        snapshot_hash=assessment.to_snapshot_hash,
        change_analysis_hash=assessment.analysis_hash,
        build_id="ci-build-42",
        firmware_artifact_id=firmware.artifact_id,
        firmware_source_revision=firmware.source_revision,
        firmware_source_hash=firmware.content_hash,
        toolchain_id="arm-none-eabi-gcc",
        toolchain_version="15.1",
        toolchain_hash="sha256:" + "e" * 64,
        protocol_schema_hash=protocol_schema_hash,
        firmware_build_artifact_hash=BUILD_HASH,
        verdict=Verdict.PASS,
        result_ref="ci://builds/42",
        started_at=NOW - timedelta(minutes=10),
        completed_at=NOW - timedelta(minutes=5),
    )


def _test_evidence(
    assessment: ChangeImpactAssessment, snapshot: ConnectorSnapshot
) -> tuple[TestExecutionEvidence, ...]:
    runtime_build_tests = {
        "hil-regression",
        "physical-device-smoke",
        "protocol-conformance",
    }
    evidence: list[TestExecutionEvidence] = []
    for requirement in assessment.required_retests:
        if requirement.test_id in {"firmware-build", "bom-provenance-validation"}:
            continue
        tier = requirement.required_tier
        evidence.append(
            TestExecutionEvidence(
                evidence_id=f"test-{requirement.test_id}-{tier.value}",
                project_id=PROJECT_ID,
                hardware_revision_id=snapshot.hardware_revision_id,
                snapshot_hash=assessment.to_snapshot_hash,
                change_analysis_hash=assessment.analysis_hash,
                test_id=requirement.test_id,
                tier=tier,
                verdict=Verdict.PASS,
                source_system=SourceSystem.CI,
                source_revision="ci-run-100",
                source_hash="sha256:" + "f" * 64,
                result_ref=f"ci://tests/{requirement.test_id}",
                recorded_at=NOW - timedelta(minutes=1),
                firmware_build_artifact_hash=(
                    BUILD_HASH if requirement.test_id in runtime_build_tests else None
                ),
                fixture_id=(
                    "fixture-1"
                    if tier in {EvidenceTier.BENCH, EvidenceTier.HIL}
                    else None
                ),
                device_instance_id=(
                    "device-1" if tier is EvidenceTier.PHYSICAL_DEVICE else None
                ),
            )
        )
    return tuple(evidence)


def run_release_demo(database_path: Path) -> dict[str, Any]:
    store = SQLiteEvidenceStore(database_path)
    try:
        service = ReleaseIntegrationService(
            store, _connector_registry(), clock=lambda: NOW
        )
        service.create_project(
            CreateProjectCommand(project_id=PROJECT_ID, name="Controller Release Demo"),
            _context("create-project", None),
        )
        service.capture_snapshot(
            PROJECT_ID,
            _capture_command(
                "before", "snapshot-11", "HW-11", NOW - timedelta(hours=2)
            ),
            _context("snapshot-before", 1),
        )
        service.capture_snapshot(
            PROJECT_ID,
            _capture_command("after", "snapshot-12", "HW-12", NOW - timedelta(hours=1)),
            _context("snapshot-after", 2),
        )
        analyzed = service.analyze_change(
            PROJECT_ID,
            AnalyzeChangeCommand(
                from_snapshot_id="snapshot-11", to_snapshot_id="snapshot-12"
            ),
            _context("analyze-change", 3),
        )
        analysis_hash = str(analyzed.payload["change_assessment"]["analysis_hash"])
        assessment_record = store.get_change_assessment(analysis_hash)
        snapshot_record = store.get_connector_snapshot(PROJECT_ID, "snapshot-12")
        evidence: tuple[RawReleaseEvidence, ...] = (
            _cost_evidence(snapshot_record.snapshot),
            _build_evidence(assessment_record.assessment, snapshot_record.snapshot),
            *_test_evidence(assessment_record.assessment, snapshot_record.snapshot),
        )
        version = 4
        evidence_ids: list[str] = []
        for index, item in enumerate(evidence, start=1):
            ingested = service.ingest_release_evidence(
                PROJECT_ID,
                IngestReleaseEvidenceCommand(
                    analysis_hash=analysis_hash, evidence=item
                ),
                _context(f"evidence-{index}", version),
            )
            version = ingested.project_version
            evidence_ids.append(
                str(ingested.payload["release_evidence"]["evidence_id"])
            )
        release_context = _context("evaluate-release", version)
        released = service.evaluate_release(
            PROJECT_ID,
            EvaluateReleaseCommand(analysis_hash=analysis_hash),
            release_context,
        )
        replay = service.evaluate_release(
            PROJECT_ID,
            EvaluateReleaseCommand(analysis_hash=analysis_hash),
            release_context,
        )
        decision_hash = str(released.payload["release_decision"]["decision_hash"])
        stored_decision = store.get_release_decision(decision_hash)
        assessment = assessment_record.assessment
        impact = assessment.impact
        if impact is None:
            raise RuntimeError("release demo hardware change produced no impact")
        return {
            "product": "FORGE engineering change release verification",
            "connector_boundary": {
                "read_only": True,
                "replaces_source_systems": False,
                "connected_systems": ["PLM", "Git", "CI", "supplier evidence"],
            },
            "trace": {
                "project_id": PROJECT_ID,
                "hardware_revision": {"from": "HW-11", "to": "HW-12"},
                "analysis_hash": analysis_hash,
                "changed_domains": [domain.value for domain in impact.changed_domains],
                "affected_domains": [
                    domain.value for domain in impact.affected_domains
                ],
                "required_retests": [
                    {
                        "test_id": item.test_id,
                        "required_tier": item.required_tier.value,
                    }
                    for item in assessment.required_retests
                ],
                "ingested_evidence_ids": evidence_ids,
            },
            "release_decision": stored_decision.model_dump(mode="json"),
            "idempotency_replayed": replay.replayed,
        }
    finally:
        store.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="forge-release-demo-") as temporary:
        result = run_release_demo(Path(temporary) / "forge.db")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
