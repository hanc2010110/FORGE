from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import Field, SerializeAsAny, field_validator, model_validator

from forge_core.change_management import ConnectorSnapshot, ReleaseEvidenceKind
from forge_core.connectors import (
    ConnectorCaptureRequest,
    ConnectorRegistry,
)
from forge_core.hashing import canonical_sha256
from forge_core.impact_engine import analyze_change, connector_snapshot_hash
from forge_core.models import ContractModel
from forge_core.persistence import (
    IdempotencyRecord,
    ProjectRecord,
    StoredCostEvaluation,
    VersionConflictError,
)
from forge_core.release_persistence import (
    RawReleaseEvidence,
    StoredChangeImpactAssessment,
    StoredConnectorSnapshot,
    StoredRawReleaseEvidence,
    StoredReleaseDecision,
    StoredReleasePolicy,
)
from forge_core.release_readiness import (
    DEFAULT_RELEASE_READINESS_POLICY,
    EvidenceRejection,
    EvidenceRejectionReason,
    FirmwareBuildEvidence,
    ReleaseReadinessPolicy,
    TestExecutionEvidence,
    cost_evidence_matches_policy,
    evaluate_release_readiness,
)
from forge_core.sqlite_store import AtomicProjectWrite, SQLiteEvidenceStore

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class MutationContext(ContractModel):
    local_installation_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=128)
    expected_project_version: int | None = Field(default=None, ge=1)

    @field_validator("local_installation_id", "idempotency_key")
    @classmethod
    def identifiers_must_be_safe(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("mutation identifiers must be opaque and safe")
        return value


class CreateProjectCommand(ContractModel):
    project_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=200)
    release_policy: ReleaseReadinessPolicy = Field(
        default_factory=lambda: DEFAULT_RELEASE_READINESS_POLICY.model_copy()
    )


class CaptureSnapshotCommand(ContractModel):
    captures: tuple[ConnectorCaptureRequest, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def captures_must_describe_one_aggregate_snapshot(self) -> CaptureSnapshotCommand:
        first = self.captures[0]
        identities = {
            (
                item.project_id,
                item.snapshot_id,
                item.hardware_revision_id,
                item.captured_at,
            )
            for item in self.captures
        }
        if identities != {
            (
                first.project_id,
                first.snapshot_id,
                first.hardware_revision_id,
                first.captured_at,
            )
        }:
            raise ValueError("connector captures must describe one project snapshot")
        connector_ids = [item.connector_id for item in self.captures]
        if len(connector_ids) != len(set(connector_ids)):
            raise ValueError("aggregate snapshot cannot capture one connector twice")
        if self.captures != tuple(
            sorted(self.captures, key=lambda item: item.connector_id)
        ):
            raise ValueError("connector capture requests must be canonically ordered")
        return self


class AnalyzeChangeCommand(ContractModel):
    from_snapshot_id: str = Field(min_length=1)
    to_snapshot_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def snapshots_must_differ(self) -> AnalyzeChangeCommand:
        if self.from_snapshot_id == self.to_snapshot_id:
            raise ValueError("change analysis requires two different snapshots")
        return self


class IngestReleaseEvidenceCommand(ContractModel):
    analysis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evidence: RawReleaseEvidence


class EvaluateReleaseCommand(ContractModel):
    analysis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class _RequestFingerprint(ContractModel):
    operation: str
    project_id: str
    expected_project_version: int | None
    command: SerializeAsAny[ContractModel]


@dataclass(frozen=True, slots=True)
class ServiceMutationResult:
    status: int
    payload: dict[str, Any]
    response_json: str
    project_version: int
    replayed: bool


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class ReleaseIntegrationService:
    """Sole owner of change analysis, release verdicts, and persisted transitions."""

    def __init__(
        self,
        store: SQLiteEvidenceStore,
        connectors: ConnectorRegistry,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._connectors = connectors
        self._clock = clock

    def _mutation(
        self,
        operation: str,
        project_id: str,
        command: ContractModel,
        context: MutationContext,
        build: Callable[
            [datetime, int], tuple[dict[str, Any], Callable[[AtomicProjectWrite], None]]
        ],
    ) -> ServiceMutationResult:
        def replay_result(response_json: str) -> ServiceMutationResult:
            payload = json.loads(response_json)
            return ServiceMutationResult(
                status=201,
                payload=payload,
                response_json=response_json,
                project_version=int(payload["project_version"]),
                replayed=True,
            )

        fingerprint = _RequestFingerprint(
            operation=operation,
            project_id=project_id,
            expected_project_version=context.expected_project_version,
            command=command,
        )
        request_hash = canonical_sha256(fingerprint)
        replay = self._store.find_idempotency(
            operation,
            project_id,
            context.local_installation_id,
            context.idempotency_key,
            request_hash,
        )
        if replay is not None:
            return replay_result(replay)
        expected_version = context.expected_project_version
        if operation == "create_project":
            if expected_version is not None:
                raise ValueError("project creation cannot use an existing version")
            next_version = 1
        else:
            if expected_version is None:
                raise ValueError("project mutation requires an expected version")
            next_version = expected_version + 1
            if self._store.get_project(project_id).version != expected_version:
                replay = self._store.find_idempotency(
                    operation,
                    project_id,
                    context.local_installation_id,
                    context.idempotency_key,
                    request_hash,
                )
                if replay is not None:
                    return replay_result(replay)
                raise VersionConflictError("project version is stale")
        now = self._clock()
        payload, apply = build(now, next_version)
        payload = dict(payload) | {"project_version": next_version}
        response_json = _canonical_json(payload)
        idempotency = IdempotencyRecord(
            operation=operation,
            project_id=project_id,
            local_installation_id=context.local_installation_id,
            key=context.idempotency_key,
            request_hash=request_hash,
            response_json=response_json,
            created_at=now,
        )
        result = self._store.transact_idempotently(
            idempotency,
            expected_project_version=expected_version,
            updated_at=now,
            apply=apply,
        )
        if result.replayed:
            return replay_result(result.response_json)
        if result.project_version != next_version:
            raise RuntimeError("storage returned an impossible project version")
        return ServiceMutationResult(
            status=201,
            payload=payload,
            response_json=response_json,
            project_version=next_version,
            replayed=False,
        )

    def create_project(
        self, command: CreateProjectCommand, context: MutationContext
    ) -> ServiceMutationResult:
        def build(
            now: datetime, _next_version: int
        ) -> tuple[dict[str, Any], Callable[[AtomicProjectWrite], None]]:
            project = ProjectRecord(
                project_id=command.project_id,
                name=command.name,
                version=1,
                created_at=now,
                updated_at=now,
            )
            release_policy = StoredReleasePolicy(
                project_id=command.project_id,
                policy_hash=canonical_sha256(command.release_policy),
                policy=command.release_policy,
                stored_at=now,
            )

            def apply(atomic: AtomicProjectWrite) -> None:
                atomic.create_project(project)
                atomic.insert_release_policy(release_policy)

            return (
                {
                    "project": project.model_dump(mode="json"),
                    "release_policy": release_policy.model_dump(mode="json"),
                },
                apply,
            )

        return self._mutation(
            "create_project", command.project_id, command, context, build
        )

    def capture_snapshot(
        self,
        project_id: str,
        command: CaptureSnapshotCommand,
        context: MutationContext,
    ) -> ServiceMutationResult:
        if any(item.project_id != project_id for item in command.captures):
            raise ValueError("connector capture project does not match route")

        def build(
            now: datetime, _next_version: int
        ) -> tuple[dict[str, Any], Callable[[AtomicProjectWrite], None]]:
            bundles = tuple(self._connectors.capture(item) for item in command.captures)
            artifacts = tuple(
                sorted(
                    (
                        artifact
                        for bundle in bundles
                        for artifact in bundle.snapshot.artifacts
                    ),
                    key=lambda item: (item.source_system.value, item.artifact_id),
                )
            )
            projections = tuple(
                sorted(
                    (
                        projection
                        for bundle in bundles
                        for projection in bundle.projections
                    ),
                    key=lambda item: item.projection_hash,
                )
            )
            first = command.captures[0]
            snapshot = ConnectorSnapshot(
                snapshot_id=first.snapshot_id,
                project_id=project_id,
                hardware_revision_id=first.hardware_revision_id,
                captured_at=first.captured_at,
                artifacts=artifacts,
            )
            record = StoredConnectorSnapshot(
                project_id=project_id,
                snapshot_id=snapshot.snapshot_id,
                snapshot_hash=connector_snapshot_hash(snapshot),
                snapshot=snapshot,
                capture_bundles=bundles,
                interface_projections=projections,
                stored_at=now,
            )
            return (
                {"connector_snapshot": record.model_dump(mode="json")},
                lambda atomic: atomic.insert_connector_snapshot(record),
            )

        return self._mutation("capture_snapshot", project_id, command, context, build)

    def analyze_change(
        self,
        project_id: str,
        command: AnalyzeChangeCommand,
        context: MutationContext,
    ) -> ServiceMutationResult:
        def build(
            now: datetime, _next_version: int
        ) -> tuple[dict[str, Any], Callable[[AtomicProjectWrite], None]]:
            previous = self._store.get_connector_snapshot(
                project_id, command.from_snapshot_id
            )
            current = self._store.get_connector_snapshot(
                project_id, command.to_snapshot_id
            )
            assessment = analyze_change(
                previous.snapshot,
                current.snapshot,
                interface_projections=(
                    previous.interface_projections + current.interface_projections
                ),
            )
            record = StoredChangeImpactAssessment(
                project_id=project_id,
                analysis_hash=assessment.analysis_hash,
                from_snapshot_id=assessment.from_snapshot_id,
                to_snapshot_id=assessment.to_snapshot_id,
                assessment=assessment,
                stored_at=now,
            )
            return (
                {"change_assessment": record.model_dump(mode="json")},
                lambda atomic: atomic.insert_change_assessment(record),
            )

        return self._mutation("analyze_change", project_id, command, context, build)

    def ingest_release_evidence(
        self,
        project_id: str,
        command: IngestReleaseEvidenceCommand,
        context: MutationContext,
    ) -> ServiceMutationResult:
        def build(
            now: datetime, _next_version: int
        ) -> tuple[dict[str, Any], Callable[[AtomicProjectWrite], None]]:
            assessment = self._store.get_change_assessment(command.analysis_hash)
            if assessment.project_id != project_id:
                raise ValueError(
                    "release evidence assessment belongs to another project"
                )
            evidence = command.evidence
            if isinstance(evidence, StoredCostEvaluation):
                release_policy = self._store.get_release_policy(project_id).policy
                if not cost_evidence_matches_policy(evidence, release_policy):
                    raise ValueError(
                        "BOM cost evidence does not match release budget policy"
                    )
                evidence_kind = ReleaseEvidenceKind.BOM_COST
                evidence_id = evidence.evidence_id
            elif isinstance(evidence, FirmwareBuildEvidence):
                evidence_kind = ReleaseEvidenceKind.FIRMWARE_BUILD
                evidence_id = evidence.evidence_id
            else:
                evidence_kind = ReleaseEvidenceKind.TEST_RESULT
                evidence_id = evidence.evidence_id
            record = StoredRawReleaseEvidence(
                project_id=project_id,
                evidence_id=evidence_id,
                evidence_kind=evidence_kind,
                evidence_hash=canonical_sha256(evidence),
                change_analysis_hash=assessment.analysis_hash,
                snapshot_hash=assessment.assessment.to_snapshot_hash,
                hardware_revision_id=(assessment.assessment.to_hardware_revision_id),
                evidence=evidence,
                stored_at=now,
            )
            return (
                {"release_evidence": record.model_dump(mode="json")},
                lambda atomic: atomic.insert_release_evidence(record),
            )

        return self._mutation(
            "ingest_release_evidence", project_id, command, context, build
        )

    def evaluate_release(
        self,
        project_id: str,
        command: EvaluateReleaseCommand,
        context: MutationContext,
    ) -> ServiceMutationResult:
        def build(
            now: datetime, _next_version: int
        ) -> tuple[dict[str, Any], Callable[[AtomicProjectWrite], None]]:
            assessment = self._store.get_change_assessment(command.analysis_hash)
            if assessment.project_id != project_id:
                raise ValueError("release assessment belongs to another project")
            snapshot = self._store.get_connector_snapshot(
                project_id, assessment.to_snapshot_id
            )
            raw = self._store.list_release_evidence(
                project_id, analysis_hash=assessment.analysis_hash
            )
            costs = tuple(
                item.evidence
                for item in raw
                if isinstance(item.evidence, StoredCostEvaluation)
            )
            latest_cost: StoredCostEvaluation | None = None
            cost_rejections: list[EvidenceRejection] = []
            if costs:
                latest_at = max(item.evaluated_at for item in costs)
                latest = tuple(item for item in costs if item.evaluated_at == latest_at)
                cost_rejections.extend(
                    EvidenceRejection(
                        evidence_id=item.evidence_id,
                        reason=EvidenceRejectionReason.SUPERSEDED,
                    )
                    for item in costs
                    if item.evaluated_at < latest_at
                )
                if len(latest) == 1:
                    latest_cost = latest[0]
                else:
                    cost_rejections.extend(
                        EvidenceRejection(
                            evidence_id=item.evidence_id,
                            reason=(EvidenceRejectionReason.AMBIGUOUS_LATEST_TIMESTAMP),
                        )
                        for item in latest
                    )
            builds = tuple(
                item.evidence
                for item in raw
                if isinstance(item.evidence, FirmwareBuildEvidence)
            )
            tests = tuple(
                item.evidence
                for item in raw
                if isinstance(item.evidence, TestExecutionEvidence)
            )
            decision = evaluate_release_readiness(
                assessment.assessment,
                snapshot.snapshot,
                cost_evaluation=latest_cost,
                pre_rejected_cost_evidence=tuple(cost_rejections),
                firmware_builds=builds,
                test_results=tests,
                evaluated_at=now,
                policy=self._store.get_release_policy(project_id).policy,
            )
            history = self._store.list_release_decisions(project_id)
            previous_hash = history[-1].decision_hash if history else None
            record = StoredReleaseDecision(
                project_id=project_id,
                sequence=len(history) + 1,
                previous_decision_hash=previous_hash,
                decision_hash=decision.decision_hash,
                report_id=decision.report.report_id,
                decision=decision,
                stored_at=now,
            )
            return (
                {"release_decision": record.model_dump(mode="json")},
                lambda atomic: atomic.append_release_decision(record),
            )

        return self._mutation("evaluate_release", project_id, command, context, build)

    def get_project(self, project_id: str) -> dict[str, Any]:
        return self._store.get_project(project_id).model_dump(mode="json")

    def get_connector_snapshot(
        self, project_id: str, snapshot_id: str
    ) -> dict[str, Any]:
        return self._store.get_connector_snapshot(project_id, snapshot_id).model_dump(
            mode="json"
        )

    def get_change_assessment(
        self, project_id: str, analysis_hash: str
    ) -> dict[str, Any]:
        value = self._store.get_change_assessment(analysis_hash)
        if value.project_id != project_id:
            raise KeyError("change assessment not found")
        return value.model_dump(mode="json")

    def get_release_evidence(self, project_id: str, evidence_id: str) -> dict[str, Any]:
        value = self._store.get_release_evidence(project_id, evidence_id)
        return value.model_dump(mode="json")

    def get_release_decision(
        self, project_id: str, decision_hash: str
    ) -> dict[str, Any]:
        value = self._store.get_release_decision(decision_hash)
        if value.project_id != project_id:
            raise KeyError("release decision not found")
        return value.model_dump(mode="json")


__all__ = [
    "AnalyzeChangeCommand",
    "CaptureSnapshotCommand",
    "CreateProjectCommand",
    "EvaluateReleaseCommand",
    "IngestReleaseEvidenceCommand",
    "MutationContext",
    "ReleaseIntegrationService",
    "ServiceMutationResult",
]
