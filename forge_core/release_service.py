from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal

from pydantic import (
    Field,
    SerializeAsAny,
    field_serializer,
    field_validator,
    model_validator,
)

from forge_core.access_control import (
    LOCAL_ACTOR_ID,
    LOCAL_ORG_ID,
    Actor,
    AuditEvent,
    AuthorizationDeniedError,
    AuthorizationRequest,
    AuthorizationResult,
    Membership,
    Organization,
    Permission,
    ProjectAccess,
    Role,
    authorize,
    build_audit_event,
)
from forge_core.cad_geometry import StoredCADGeometryAsset, parse_stl_asset
from forge_core.change_management import (
    ArtifactDomain,
    ConnectorSnapshot,
    EvidenceTier,
    ExternalArtifactRef,
    ReleaseEvidenceKind,
    ReleaseStatus,
    SourceSystem,
)
from forge_core.change_planning import (
    AssetInput,
    AssetInputKind,
    ChangeScenario,
    ComponentSpecification,
    PlanIntentKind,
    PlannedChangeAction,
    PlannedComponentChange,
    change_scenario_hash,
    component_specification_hash,
)
from forge_core.connectors import (
    ConnectorCaptureRequest,
    ConnectorRegistry,
)
from forge_core.constraints import InterfaceContract, QuoteSnapshot
from forge_core.conversation_runtime import (
    ConversationRequest,
    ConversationRuntimeService,
)
from forge_core.conversational_design import (
    ClaimConfidence,
    DesignConversationState,
    DesignParameter,
    DesignStateTransition,
    EvidenceClaimKind,
    SimulationMetric,
    build_design_candidate,
    build_evidence_claim,
    build_simulation_binding,
)
from forge_core.conversational_persistence import (
    DesignCandidateApprovalRequest,
    StoredDesignCandidate,
    StoredDesignCandidateApproval,
    StoredDesignCandidateApprovalReceipt,
    StoredDesignStateTransition,
    StoredEvidenceClaim,
    StoredSimulationBinding,
    design_candidate_approval_hash,
    design_candidate_approval_receipt_hash,
)
from forge_core.external_evidence_planning import (
    EXTERNAL_EVIDENCE_TIERS,
    AcceptanceCriterion,
    ExternalEvidenceImportBinding,
    ExternalEvidenceRequirementHint,
    OperatingScenario,
    OperatingScenarioKind,
    build_required_external_evidence_plan,
    operating_scenario_hash,
    verify_external_evidence_plan,
)
from forge_core.hashing import canonical_sha256
from forge_core.impact_engine import analyze_change, connector_snapshot_hash
from forge_core.iteration_workflow import (
    DeterministicReleaseOutcome,
    next_step_for_release_outcome,
)
from forge_core.local_rag import (
    DeterministicLexicalRetriever,
    LocalExtractiveProvider,
    SourceKind,
    StoredKnowledgeSource,
    StoredRuntimeResult,
    build_knowledge_source,
    ingest_knowledge_source,
    local_prompt_manifest,
    local_provider_manifest,
)
from forge_core.models import ContractModel, Quantity, SourceRef
from forge_core.operations import SQLiteOperationsService
from forge_core.persistence import (
    IdempotencyRecord,
    ProjectRecord,
    RecordNotFoundError,
    StoredCostEvaluation,
    VersionConflictError,
)
from forge_core.planning_persistence import (
    StoredChangeImpactPreview,
    StoredExternalEvidencePlan,
    StoredExternalEvidencePlanVerification,
    StoredPlanVerification,
)
from forge_core.preview_engine import (
    preview_change_scenario,
    verify_plan_against_actual,
)
from forge_core.release_persistence import (
    RawReleaseEvidence,
    StoredAutomaticReverificationFailure,
    StoredAutomaticReverificationReceipt,
    StoredAutomaticReverificationTrigger,
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
from forge_core.resolution_persistence import (
    StoredDesignProposalSet,
    StoredReleaseDiagnosis,
    StoredResolutionPlan,
)
from forge_core.resolution_planning import (
    BlockerDiagnosis,
    DesignStrategy,
    DiagnosisItem,
    FixProposalSet,
    GoalPriority,
    ProposedChange,
    RequiredReverifyTest,
    TradeoffScores,
    build_blocker_diagnosis,
    build_design_alternative,
    build_design_proposal_set,
    build_engineering_goal,
    build_fix_proposal,
    build_fix_proposal_set,
    select_fix_proposal,
)
from forge_core.sqlite_store import AtomicProjectWrite, SQLiteEvidenceStore

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_ALL_DOMAINS = tuple(sorted(ArtifactDomain, key=lambda item: item.value))
_DEFAULT_PLANNED_ACTIONS = ("review-proposed-hardware-change",)


class MutationContext(ContractModel):
    local_installation_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=128)
    expected_project_version: int | None = Field(default=None, ge=1)
    org_id: str = LOCAL_ORG_ID
    actor_id: str = LOCAL_ACTOR_ID

    @field_validator("local_installation_id", "idempotency_key", "org_id", "actor_id")
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


class IngestKnowledgeSourceCommand(ContractModel):
    source_id: str = Field(min_length=1)
    source_kind: SourceKind
    source_uri: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    captured_at: datetime
    text: str = Field(min_length=1)


class RunConversationCommand(ContractModel):
    request_id: str = Field(min_length=1)
    user_message: str = Field(min_length=1, max_length=4000)
    requested_at: datetime


class IngestCADGeometryCommand(ContractModel):
    asset_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    source_uri: str = Field(min_length=1, max_length=512)
    source_version: str = Field(min_length=1, max_length=128)
    captured_at: datetime
    content_base64: str = Field(min_length=1, max_length=980_000)

    @field_validator("content_base64")
    @classmethod
    def content_must_be_canonical_base64(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("CAD content must be valid base64") from exc
        if base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError("CAD content must use canonical base64")
        return value


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


class ComponentSpecificationDraft(ContractModel):
    component_id: str = Field(min_length=1)
    manufacturer: str = Field(min_length=1)
    part_number: str = Field(min_length=1)
    quantity: int = Field(ge=1)
    source_ref: ExternalArtifactRef
    interface_contract: InterfaceContract | None = None
    quote: QuoteSnapshot | None = None
    attributes: Mapping[str, Quantity] = Field(default_factory=dict)

    @field_validator("attributes", mode="after")
    @classmethod
    def freeze_attributes(cls, value: Mapping[str, Quantity]) -> Mapping[str, Quantity]:
        return MappingProxyType(dict(sorted(value.items())))

    @field_serializer("attributes")
    def serialize_attributes(
        self, value: Mapping[str, Quantity]
    ) -> dict[str, Quantity]:
        return dict(value)


class PlannedComponentChangeDraft(ContractModel):
    change_id: str = Field(min_length=1)
    action: PlannedChangeAction
    before: ComponentSpecificationDraft | None = None
    after: ComponentSpecificationDraft | None = None
    rationale: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("rationale")
    @classmethod
    def rationale_must_be_unique_and_canonical(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("planned change rationale must be unique")
        ordered = tuple(sorted(value))
        if value != ordered:
            raise ValueError("planned change rationale must be in canonical order")
        return value

    @model_validator(mode="after")
    def action_must_match_endpoints(self) -> PlannedComponentChangeDraft:
        if self.action is PlannedChangeAction.ADD:
            if self.before is not None or self.after is None:
                raise ValueError("add planned change requires only an after component")
        elif self.action is PlannedChangeAction.REMOVE:
            if self.before is None or self.after is not None:
                raise ValueError(
                    "remove planned change requires only a before component"
                )
        elif self.before is None or self.after is None:
            raise ValueError("replace planned change requires before and after")
        if (
            self.before is not None
            and self.after is not None
            and self.before.component_id != self.after.component_id
        ):
            raise ValueError("replace planned change must preserve component_id")
        return self


class AssetInputDraft(ContractModel):
    asset_id: str = Field(min_length=1)
    kind: AssetInputKind
    source_system: SourceSystem
    source_locator: str = Field(min_length=1)
    source_revision: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    captured_at: datetime


class CreateChangePreviewCommand(ContractModel):
    scenario_id: str = Field(min_length=1)
    asset_input: AssetInputDraft
    intent_kind: PlanIntentKind = PlanIntentKind.CHANGE_IMPACT
    baseline_snapshot_id: str = Field(min_length=1)
    proposed_hardware_revision_id: str = Field(min_length=1)
    changes: tuple[PlannedComponentChangeDraft, ...] = Field(min_length=1)

    @field_validator(
        "scenario_id",
        "baseline_snapshot_id",
        "proposed_hardware_revision_id",
    )
    @classmethod
    def identifiers_must_be_safe(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("change preview identifiers must be opaque and safe")
        return value

    @model_validator(mode="after")
    def preview_only_materializes_change_impact_intents(
        self,
    ) -> CreateChangePreviewCommand:
        if self.intent_kind is not PlanIntentKind.CHANGE_IMPACT:
            raise ValueError("change preview requires change_impact intent")
        return self


class ExternalEvidenceRequestDraft(ContractModel):
    test_id: str = Field(min_length=1)
    required_tier: EvidenceTier
    acceptance_criteria: str = Field(min_length=1)

    @field_validator("test_id")
    @classmethod
    def test_id_must_be_safe(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("external evidence test_id must be opaque and safe")
        return value

    @model_validator(mode="after")
    def tier_must_be_external(self) -> ExternalEvidenceRequestDraft:
        if self.required_tier not in EXTERNAL_EVIDENCE_TIERS:
            raise ValueError("external evidence request requires an external tier")
        return self


class CreateExternalEvidencePlanCommand(ContractModel):
    scenario_id: str = Field(min_length=1)
    asset_input: AssetInputDraft
    baseline_snapshot_id: str = Field(min_length=1)
    scenario_text: str = Field(min_length=1)
    scenario_kind: OperatingScenarioKind = OperatingScenarioKind.DYNAMIC_SIMULATION
    external_tool_ref: str = Field(min_length=1)
    required_evidence: tuple[ExternalEvidenceRequestDraft, ...] = Field(min_length=1)

    @field_validator("scenario_id", "baseline_snapshot_id", "external_tool_ref")
    @classmethod
    def identifiers_must_be_safe(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError(
                "external evidence plan identifiers must be opaque and safe"
            )
        return value

    @model_validator(mode="after")
    def requests_must_be_unique(self) -> CreateExternalEvidencePlanCommand:
        identities = [
            (item.required_tier, item.test_id) for item in self.required_evidence
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("external evidence requests must be unique")
        return self


class VerifyPlanCommand(ContractModel):
    preview_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    actual_change_analysis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class VerifyExternalEvidencePlanCommand(ContractModel):
    evidence_plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    actual_change_analysis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    imported_evidence_ids: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("imported_evidence_ids")
    @classmethod
    def imported_ids_must_be_unique_and_safe(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("imported evidence IDs must be unique")
        if any(_SAFE_ID.fullmatch(item) is None for item in value):
            raise ValueError("imported evidence IDs must be opaque and safe")
        return value


class AutomaticReverificationApproval(ContractModel):
    approval_id: str = Field(min_length=1, max_length=128)
    analysis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    candidate_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    preview_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    plan_verification_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evidence_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    approved_by: str = Field(min_length=1, max_length=128)
    approved_at: datetime

    @field_validator("approval_id", "approved_by")
    @classmethod
    def approval_ids_must_be_safe(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError(
                "automatic reverification approval identifiers must be safe"
            )
        return value

    @field_validator("approved_at")
    @classmethod
    def approved_at_must_be_utc(cls, value: datetime) -> datetime:
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise ValueError("automatic reverification approval time must be UTC")
        if offset.total_seconds() != 0:
            raise ValueError("automatic reverification approval time must be UTC")
        return value


class IngestReleaseEvidenceCommand(ContractModel):
    analysis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evidence: RawReleaseEvidence
    automatic_reverification: AutomaticReverificationApproval | None = None

    @model_validator(mode="after")
    def automatic_reverification_must_bind_exact_evidence(
        self,
    ) -> IngestReleaseEvidenceCommand:
        approval = self.automatic_reverification
        if approval is None:
            return self
        if approval.analysis_hash != self.analysis_hash:
            raise ValueError(
                "automatic reverification approval analysis does not match"
            )
        if approval.evidence_hash != canonical_sha256(self.evidence):
            raise ValueError(
                "automatic reverification approval evidence does not match"
            )
        return self


class EvaluateReleaseCommand(ContractModel):
    analysis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    automatic_trigger_hash: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )


class CreateDesignProposalCommand(ContractModel):
    goal: str = Field(min_length=1, max_length=240)
    priority: GoalPriority
    constraints: str = Field(default="", max_length=800)
    preview_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ApproveDesignCandidateCommand(ContractModel):
    approval_id: str = Field(min_length=1)
    approval_nonce: str = Field(min_length=32, max_length=256)
    session_id: str = Field(default="session-1", min_length=1)
    candidate_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    parameters: tuple[DesignParameter, ...] = Field(min_length=1)
    requirements: tuple[str, ...] = Field(min_length=1)

    @field_validator("approval_id", "session_id", "candidate_id")
    @classmethod
    def approval_and_candidate_ids_must_be_safe(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("approval and candidate IDs must be opaque and safe")
        return value


class ConfirmDesignCandidateCommand(ContractModel):
    approval_id: str = Field(min_length=1)
    approval_nonce: str = Field(min_length=32, max_length=256)
    session_id: str = Field(default="session-1", min_length=1)
    candidate_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    parameters: tuple[DesignParameter, ...] = Field(min_length=1)
    requirements: tuple[str, ...] = Field(min_length=1)
    confirmed_by: str = Field(min_length=1)

    @field_validator("approval_id", "session_id", "candidate_id")
    @classmethod
    def candidate_id_must_be_safe(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("candidate_id must be opaque and safe")
        return value


class BindDesignSimulationCommand(ContractModel):
    session_id: str = Field(default="session-1", min_length=1)
    simulation_id: str = Field(min_length=1)
    candidate_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    tool_ref: SourceRef
    metrics: tuple[SimulationMetric, ...] = Field(min_length=1)

    @field_validator("session_id", "simulation_id")
    @classmethod
    def simulation_id_must_be_safe(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("simulation_id must be opaque and safe")
        return value


class RecordConversationalClaimCommand(ContractModel):
    session_id: str = Field(default="session-1", min_length=1)
    claim_id: str = Field(min_length=1)
    kind: EvidenceClaimKind
    statement: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    confidence: ClaimConfidence | None = None

    @field_validator("session_id", "claim_id")
    @classmethod
    def claim_id_must_be_safe(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("claim_id must be opaque and safe")
        return value


class AppendDesignTransitionCommand(ContractModel):
    session_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    from_state: DesignConversationState
    to_state: DesignConversationState
    candidate_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    simulation_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    evidence_refs: tuple[str, ...] = ()

    @field_validator("session_id")
    @classmethod
    def session_id_must_be_safe(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("session_id must be opaque and safe")
        return value


class CreateReleaseDiagnosisCommand(ContractModel):
    decision_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class CreateResolutionPlanCommand(ContractModel):
    diagnosis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    fix_proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class _RequestFingerprint(ContractModel):
    operation: str
    project_id: str
    expected_project_version: int | None
    org_id: str
    actor_id: str
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


def _nonce_hash(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _component_key(item: ComponentSpecification | None) -> tuple[str, str, str]:
    if item is None:
        return ("", "", "")
    return item.component_id, item.part_number, item.specification_hash


def _planned_change_key(
    item: PlannedComponentChange,
) -> tuple[str, str, tuple[str, str, str], tuple[str, str, str]]:
    return (
        item.change_id,
        item.action.value,
        _component_key(item.before),
        _component_key(item.after),
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

    _OPERATION_PERMISSIONS: Mapping[str, Permission] = MappingProxyType(
        {
            "capture_snapshot": Permission.MUTATE_EVIDENCE,
            "analyze_change": Permission.CREATE_PLAN,
            "create_change_preview": Permission.CREATE_PLAN,
            "create_design_proposal": Permission.CREATE_PLAN,
            "approve_design_candidate": Permission.ACCEPT_DESIGN,
            "confirm_design_candidate": Permission.ACCEPT_DESIGN,
            "bind_design_simulation": Permission.MUTATE_EVIDENCE,
            "record_conversational_claim": Permission.MUTATE_EVIDENCE,
            "append_design_transition": Permission.CREATE_PLAN,
            "create_external_evidence_plan": Permission.CREATE_PLAN,
            "verify_external_evidence_plan": Permission.VERIFY_EVIDENCE,
            "verify_plan": Permission.VERIFY_EVIDENCE,
            "ingest_release_evidence": Permission.MUTATE_EVIDENCE,
            "evaluate_release": Permission.DECIDE_RELEASE,
            "create_release_diagnosis": Permission.CREATE_PLAN,
            "create_resolution_plan": Permission.CREATE_PLAN,
            "ingest_knowledge_source": Permission.MUTATE_EVIDENCE,
            "run_conversation": Permission.CREATE_PLAN,
            "ingest_cad_geometry": Permission.MUTATE_EVIDENCE,
        }
    )

    def _authorization_result(
        self, request: AuthorizationRequest
    ) -> AuthorizationResult:
        try:
            actor = self._store.get_actor(request.actor_id)
            membership = self._store.get_membership(request.org_id, request.actor_id)
            project_access = self._store.get_project_access(
                request.org_id, request.project_id, request.actor_id
            )
        except RecordNotFoundError:
            return AuthorizationResult(
                allowed=False,
                reason="identity_or_access_not_found",
                role=None,
                permission=request.permission,
                request_hash=request.request_hash,
            )
        return authorize(
            actor=actor,
            membership=membership,
            project_access=project_access,
            request=request,
        )

    def _authorize(
        self,
        *,
        project_id: str,
        org_id: str,
        actor_id: str,
        permission: Permission,
        event_id: str,
        occurred_at: datetime,
        persist: bool,
    ) -> AuditEvent:
        request = AuthorizationRequest(
            org_id=org_id,
            project_id=project_id,
            actor_id=actor_id,
            permission=permission,
        )
        result = self._authorization_result(request)
        if not result.allowed:
            event_id = f"audit:{secrets.token_hex(24)}"
        event = build_audit_event(
            event_id=event_id,
            request=request,
            result=result,
            occurred_at=occurred_at,
        )
        if persist or not result.allowed:
            self._store.append_audit_event(event)
        if not result.allowed:
            raise AuthorizationDeniedError(result.reason)
        return event

    def authorize_project_read(
        self,
        project_id: str,
        *,
        org_id: str = LOCAL_ORG_ID,
        actor_id: str = LOCAL_ACTOR_ID,
    ) -> None:
        self._store.get_project(project_id)
        self._authorize(
            project_id=project_id,
            org_id=org_id,
            actor_id=actor_id,
            permission=Permission.READ_PROJECT,
            event_id=f"audit:{secrets.token_hex(24)}",
            occurred_at=self._clock(),
            persist=True,
        )

    def list_audit_events(
        self,
        project_id: str,
        *,
        org_id: str = LOCAL_ORG_ID,
        actor_id: str = LOCAL_ACTOR_ID,
    ) -> list[dict[str, Any]]:
        self.authorize_project_read(project_id, org_id=org_id, actor_id=actor_id)
        return [
            item.model_dump(mode="json")
            for item in self._store.list_audit_events(project_id)
        ]

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
            org_id=context.org_id,
            actor_id=context.actor_id,
            command=command,
        )
        request_hash = canonical_sha256(fingerprint)
        now = self._clock()
        audit_event: AuditEvent | None = None
        if operation != "create_project":
            permission = self._OPERATION_PERMISSIONS[operation]
            audit_event = self._authorize(
                project_id=project_id,
                org_id=context.org_id,
                actor_id=context.actor_id,
                permission=permission,
                event_id=f"audit:{request_hash.removeprefix('sha256:')}",
                occurred_at=now,
                persist=False,
            )
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

        def apply_with_audit(atomic: AtomicProjectWrite) -> None:
            apply(atomic)
            if audit_event is not None:
                atomic.append_audit_event(audit_event)

        result = self._store.transact_idempotently(
            idempotency,
            expected_project_version=expected_version,
            updated_at=now,
            apply=apply_with_audit,
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
            organization = Organization(
                org_id=context.org_id,
                name=(
                    "FORGE Local Organization"
                    if context.org_id == LOCAL_ORG_ID
                    else context.org_id
                ),
                created_at=now,
            )
            actor = Actor(
                actor_id=context.actor_id,
                display_name=(
                    "Local Operator"
                    if context.actor_id == LOCAL_ACTOR_ID
                    else context.actor_id
                ),
            )
            membership = Membership(
                org_id=context.org_id,
                actor_id=context.actor_id,
                role=Role.ADMIN,
                granted_by=context.actor_id,
                granted_at=now,
            )
            project_access = ProjectAccess(
                org_id=context.org_id,
                project_id=command.project_id,
                actor_id=context.actor_id,
            )
            authorization_request = AuthorizationRequest(
                org_id=context.org_id,
                project_id=command.project_id,
                actor_id=context.actor_id,
                permission=Permission.MANAGE_ORG,
            )
            authorization_result = AuthorizationResult(
                allowed=True,
                reason="local_project_bootstrap",
                role=Role.ADMIN,
                permission=Permission.MANAGE_ORG,
                request_hash=authorization_request.request_hash,
            )
            audit_event = build_audit_event(
                event_id=(
                    "audit:"
                    + canonical_sha256(authorization_request).removeprefix("sha256:")
                ),
                request=authorization_request,
                result=authorization_result,
                occurred_at=now,
            )

            def apply(atomic: AtomicProjectWrite) -> None:
                atomic.create_project(project)
                atomic.insert_release_policy(release_policy)
                atomic.insert_organization(organization)
                atomic.insert_actor(actor)
                atomic.insert_membership(membership)
                atomic.insert_project_access(project_access)
                atomic.append_audit_event(audit_event)

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

    def ingest_knowledge_source(
        self,
        project_id: str,
        command: IngestKnowledgeSourceCommand,
        context: MutationContext,
    ) -> ServiceMutationResult:
        def build(
            now: datetime, _next_version: int
        ) -> tuple[dict[str, Any], Callable[[AtomicProjectWrite], None]]:
            source = build_knowledge_source(
                source_id=command.source_id,
                tenant_id=context.org_id,
                project_id=project_id,
                source_kind=command.source_kind,
                source_uri=command.source_uri,
                source_version=command.source_version,
                captured_at=command.captured_at,
                text=command.text,
            )
            record = StoredKnowledgeSource(
                project_id=project_id,
                source_id=source.source_id,
                ingestion=ingest_knowledge_source(source),
                stored_at=now,
            )

            def apply(atomic: AtomicProjectWrite) -> None:
                atomic.insert_knowledge_source(record)

            return {"knowledge_source": record.model_dump(mode="json")}, apply

        return self._mutation(
            "ingest_knowledge_source", project_id, command, context, build
        )

    def run_conversation(
        self,
        project_id: str,
        command: RunConversationCommand,
        context: MutationContext,
    ) -> ServiceMutationResult:
        def build(
            now: datetime, _next_version: int
        ) -> tuple[dict[str, Any], Callable[[AtomicProjectWrite], None]]:
            chunks = tuple(
                chunk
                for record in self._store.list_knowledge_sources(project_id)
                if record.ingestion.source.tenant_id == context.org_id
                for chunk in record.ingestion.chunks
            )
            runtime = ConversationRuntimeService(
                retriever=DeterministicLexicalRetriever(chunks),
                provider=LocalExtractiveProvider(),
                prompt_manifest=local_prompt_manifest(),
                provider_manifest=local_provider_manifest(),
            ).run(
                ConversationRequest(
                    request_id=command.request_id,
                    tenant_id=context.org_id,
                    project_id=project_id,
                    user_id=context.actor_id,
                    user_message=command.user_message,
                    requested_at=command.requested_at,
                )
            )
            record = StoredRuntimeResult(
                project_id=project_id,
                request_id=command.request_id,
                runtime=runtime,
                stored_at=now,
            )

            def apply(atomic: AtomicProjectWrite) -> None:
                atomic.insert_runtime_result(record)

            return {"conversation": record.model_dump(mode="json")}, apply

        return self._mutation("run_conversation", project_id, command, context, build)

    def ingest_cad_geometry(
        self,
        project_id: str,
        command: IngestCADGeometryCommand,
        context: MutationContext,
    ) -> ServiceMutationResult:
        def build(
            now: datetime, _next_version: int
        ) -> tuple[dict[str, Any], Callable[[AtomicProjectWrite], None]]:
            asset = parse_stl_asset(
                asset_id=command.asset_id,
                project_id=project_id,
                tenant_id=context.org_id,
                source_uri=command.source_uri,
                source_version=command.source_version,
                captured_at=command.captured_at,
                content=base64.b64decode(command.content_base64, validate=True),
            )
            record = StoredCADGeometryAsset(
                project_id=project_id,
                asset_id=command.asset_id,
                asset=asset,
                stored_at=now,
            )

            def apply(atomic: AtomicProjectWrite) -> None:
                atomic.insert_cad_geometry(record)

            return {"cad_geometry": record.model_dump(mode="json")}, apply

        return self._mutation(
            "ingest_cad_geometry", project_id, command, context, build
        )

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

    def create_change_preview(
        self,
        project_id: str,
        command: CreateChangePreviewCommand,
        context: MutationContext,
    ) -> ServiceMutationResult:
        def build(
            now: datetime, _next_version: int
        ) -> tuple[dict[str, Any], Callable[[AtomicProjectWrite], None]]:
            baseline = self._store.get_connector_snapshot(
                project_id, command.baseline_snapshot_id
            )
            scenario = self._materialize_change_scenario(
                project_id, command, baseline, now
            )
            preview = preview_change_scenario(scenario, generated_at=now)
            record = StoredChangeImpactPreview(
                project_id=project_id,
                preview_hash=preview.preview_hash,
                scenario_id=scenario.scenario_id,
                scenario_hash=scenario.scenario_hash,
                baseline_snapshot_id=scenario.baseline_snapshot_id,
                baseline_snapshot_hash=scenario.baseline_snapshot_hash,
                scenario=scenario,
                preview=preview,
                stored_at=now,
            )
            return (
                {"change_preview": record.model_dump(mode="json")},
                lambda atomic: atomic.insert_change_preview(record),
            )

        return self._mutation(
            "create_change_preview", project_id, command, context, build
        )

    def create_design_proposal(
        self,
        project_id: str,
        command: CreateDesignProposalCommand,
        context: MutationContext,
    ) -> ServiceMutationResult:
        def build(
            now: datetime, _next_version: int
        ) -> tuple[dict[str, Any], Callable[[AtomicProjectWrite], None]]:
            stored_preview = self._store.get_change_preview(command.preview_hash)
            if stored_preview.project_id != project_id:
                raise ValueError("design proposal preview belongs to another project")
            preview = stored_preview.preview
            scenario = stored_preview.scenario
            constraints = tuple(
                sorted(
                    {
                        item.strip()
                        for item in re.split(r"[;\n]+", command.constraints)
                        if item.strip()
                    }
                )
            )
            goal = build_engineering_goal(
                command.goal,
                command.priority,
                (
                    SourceRef(kind="user", identifier=command.goal),
                    SourceRef(
                        kind="artifact",
                        identifier=preview.preview_id,
                        version=preview.planner_version,
                        hash=preview.preview_hash,
                    ),
                ),
                constraints,
            )
            change_refs = tuple(
                sorted(f"planned-change:{item.change_id}" for item in scenario.changes)
            )
            evidence_refs = tuple(
                sorted(
                    {f"preview:{preview.preview_hash}"}
                    | {
                        ref
                        for finding in preview.predicted_findings
                        for ref in finding.evidence_refs
                    }
                )
            )
            strategies = self._design_strategies(command.priority)
            alternatives = tuple(
                build_design_alternative(
                    alternative_id=f"alternative-{index:02d}-{strategy.value}",
                    strategy=strategy,
                    proposed_change_refs=change_refs,
                    affected_domains=preview.predicted_affected_domains,
                    tradeoff_scores=self._tradeoff_scores(strategy),
                    rationale=self._design_rationale(strategy, command.goal),
                    assumptions=preview.assumptions,
                    missing_information=preview.missing_information,
                    evidence_refs=evidence_refs,
                )
                for index, strategy in enumerate(strategies, start=1)
            )
            proposal = build_design_proposal_set(
                project_id=project_id,
                baseline_snapshot_id=stored_preview.baseline_snapshot_id,
                baseline_snapshot_hash=stored_preview.baseline_snapshot_hash,
                preview_hash=stored_preview.preview_hash,
                goal=goal,
                alternatives=alternatives,
            )
            record = StoredDesignProposalSet(
                project_id=project_id,
                proposal_hash=proposal.proposal_hash,
                preview_hash=proposal.preview_hash,
                proposal=proposal,
                stored_at=now,
            )
            return (
                {"design_proposal": self._design_proposal_payload(record)},
                lambda atomic: atomic.insert_design_proposal(record),
            )

        return self._mutation(
            "create_design_proposal", project_id, command, context, build
        )

    def approve_design_candidate(
        self,
        project_id: str,
        command: ApproveDesignCandidateCommand,
        context: MutationContext,
    ) -> ServiceMutationResult:
        """Record the authenticated user's approval without persisting the nonce."""

        def build(
            now: datetime, _next_version: int
        ) -> tuple[dict[str, Any], Callable[[AtomicProjectWrite], None]]:
            proposal = self._store.get_design_proposal(command.proposal_hash)
            if proposal.project_id != project_id:
                raise ValueError("design candidate proposal belongs to another project")
            request = DesignCandidateApprovalRequest(
                project_id=project_id,
                session_id=command.session_id,
                candidate_id=command.candidate_id,
                revision=command.revision,
                proposal_hash=command.proposal_hash,
                parameters=command.parameters,
                requirements=command.requirements,
                confirmed_by=context.actor_id,
            )
            request_hash = canonical_sha256(request)
            nonce_hash = _nonce_hash(command.approval_nonce)
            approval = StoredDesignCandidateApproval(
                approval_id=command.approval_id,
                approval_hash=design_candidate_approval_hash(
                    approval_id=command.approval_id,
                    project_id=project_id,
                    request_hash=request_hash,
                    nonce_hash=nonce_hash,
                    approved_by=context.actor_id,
                    approved_at=now,
                ),
                project_id=project_id,
                request_hash=request_hash,
                nonce_hash=nonce_hash,
                approved_by=context.actor_id,
                request=request,
                approved_at=now,
                stored_at=now,
            )
            return (
                {
                    "design_candidate_approval": {
                        **approval.model_dump(mode="json"),
                        "nonce_hash": "<redacted>",
                    }
                },
                lambda atomic: atomic.insert_design_candidate_approval(approval),
            )

        return self._mutation(
            "approve_design_candidate", project_id, command, context, build
        )

    def confirm_design_candidate(
        self,
        project_id: str,
        command: ConfirmDesignCandidateCommand,
        context: MutationContext,
    ) -> ServiceMutationResult:
        def build(
            now: datetime, _next_version: int
        ) -> tuple[dict[str, Any], Callable[[AtomicProjectWrite], None]]:
            if command.confirmed_by != context.actor_id:
                raise AuthorizationDeniedError(
                    "design candidate confirmer must match authenticated actor"
                )
            proposal = self._store.get_design_proposal(command.proposal_hash)
            if proposal.project_id != project_id:
                raise ValueError("design candidate proposal belongs to another project")
            approval = self._store.get_design_candidate_approval(command.approval_id)
            expected_request = DesignCandidateApprovalRequest(
                project_id=project_id,
                session_id=command.session_id,
                candidate_id=command.candidate_id,
                revision=command.revision,
                proposal_hash=command.proposal_hash,
                parameters=command.parameters,
                requirements=command.requirements,
                confirmed_by=context.actor_id,
            )
            if (
                approval.project_id != project_id
                or approval.approved_by != context.actor_id
                or approval.request != expected_request
                or approval.request_hash != canonical_sha256(expected_request)
            ):
                raise AuthorizationDeniedError(
                    "design candidate approval does not cover this exact request"
                )
            if not secrets.compare_digest(
                approval.nonce_hash, _nonce_hash(command.approval_nonce)
            ):
                raise AuthorizationDeniedError(
                    "design candidate approval nonce is invalid"
                )
            try:
                self._store.get_design_candidate_approval_receipt(approval.approval_id)
            except RecordNotFoundError:
                pass
            else:
                raise AuthorizationDeniedError(
                    "design candidate approval has already been consumed"
                )
            candidate = build_design_candidate(
                project_id=project_id,
                candidate_id=command.candidate_id,
                revision=command.revision,
                baseline_snapshot_hash=proposal.proposal.baseline_snapshot_hash,
                proposal_hash=proposal.proposal_hash,
                parameters=command.parameters,
                requirements=command.requirements,
                confirmed_by=context.actor_id,
                confirmed_at=now,
            )
            record = StoredDesignCandidate(
                project_id=project_id,
                session_id=command.session_id,
                candidate_hash=candidate.candidate_hash,
                candidate_id=candidate.candidate_id,
                revision=candidate.revision,
                proposal_hash=candidate.proposal_hash,
                candidate=candidate,
                stored_at=now,
            )
            receipt = StoredDesignCandidateApprovalReceipt(
                receipt_hash=design_candidate_approval_receipt_hash(
                    approval_id=approval.approval_id,
                    approval_hash=approval.approval_hash,
                    project_id=project_id,
                    request_hash=approval.request_hash,
                    candidate_hash=candidate.candidate_hash,
                    consumed_by=context.actor_id,
                    consumed_at=now,
                ),
                approval_id=approval.approval_id,
                approval_hash=approval.approval_hash,
                project_id=project_id,
                request_hash=approval.request_hash,
                candidate_hash=candidate.candidate_hash,
                consumed_by=context.actor_id,
                consumed_at=now,
            )
            return (
                {
                    "design_candidate": record.model_dump(mode="json"),
                    "approval_receipt": receipt.model_dump(mode="json"),
                },
                lambda atomic: atomic.consume_design_candidate_approval(
                    record, receipt
                ),
            )

        return self._mutation(
            "confirm_design_candidate", project_id, command, context, build
        )

    def bind_design_simulation(
        self,
        project_id: str,
        command: BindDesignSimulationCommand,
        context: MutationContext,
    ) -> ServiceMutationResult:
        def build(
            now: datetime, _next_version: int
        ) -> tuple[dict[str, Any], Callable[[AtomicProjectWrite], None]]:
            candidate_record = self._store.get_design_candidate(command.candidate_hash)
            if candidate_record.project_id != project_id:
                raise ValueError("design candidate belongs to another project")
            if candidate_record.session_id != command.session_id:
                raise ValueError("design candidate belongs to another session")
            simulation = build_simulation_binding(
                simulation_id=command.simulation_id,
                candidate=candidate_record.candidate,
                tool_ref=command.tool_ref,
                metrics=command.metrics,
                created_at=now,
            )
            record = StoredSimulationBinding(
                project_id=project_id,
                session_id=command.session_id,
                simulation_hash=simulation.simulation_hash,
                simulation_id=simulation.simulation_id,
                candidate_hash=simulation.candidate_hash,
                simulation=simulation,
                stored_at=now,
            )
            return (
                {"simulation_binding": record.model_dump(mode="json")},
                lambda atomic: atomic.insert_simulation_binding(record),
            )

        return self._mutation(
            "bind_design_simulation", project_id, command, context, build
        )

    def record_conversational_claim(
        self,
        project_id: str,
        command: RecordConversationalClaimCommand,
        context: MutationContext,
    ) -> ServiceMutationResult:
        def build(
            now: datetime, _next_version: int
        ) -> tuple[dict[str, Any], Callable[[AtomicProjectWrite], None]]:
            self._validate_conversational_claim_evidence(
                project_id,
                command.session_id,
                command.kind,
                command.evidence_refs,
            )
            claim = build_evidence_claim(
                claim_id=command.claim_id,
                kind=command.kind,
                statement=command.statement,
                evidence_refs=command.evidence_refs,
                confidence=command.confidence,
            )
            record = StoredEvidenceClaim(
                project_id=project_id,
                session_id=command.session_id,
                claim_hash=claim.claim_hash,
                claim_id=claim.claim_id,
                claim=claim,
                stored_at=now,
            )
            return (
                {"evidence_claim": record.model_dump(mode="json")},
                lambda atomic: atomic.insert_evidence_claim(record),
            )

        return self._mutation(
            "record_conversational_claim", project_id, command, context, build
        )

    def _validate_conversational_claim_evidence(
        self,
        project_id: str,
        session_id: str,
        kind: EvidenceClaimKind,
        evidence_refs: tuple[str, ...],
    ) -> None:
        resolved_kinds: set[str] = set()
        try:
            for evidence_ref in evidence_refs:
                ref_kind, separator, identifier = evidence_ref.partition(":")
                if not separator or not identifier:
                    raise ValueError(
                        "conversational claim evidence references must use "
                        "a supported kind:identifier form"
                    )
                resolved_kinds.add(ref_kind)
                if ref_kind == "simulation":
                    simulation = self._store.get_simulation_binding(identifier)
                    if (
                        simulation.project_id != project_id
                        or simulation.session_id != session_id
                    ):
                        raise ValueError(
                            "simulation claim evidence belongs to another "
                            "project or session"
                        )
                elif ref_kind == "release-evidence":
                    release_evidence = self._store.get_release_evidence(
                        project_id, identifier
                    )
                    if kind is EvidenceClaimKind.MEASURED:
                        evidence = release_evidence.evidence
                        if not isinstance(evidence, TestExecutionEvidence) or (
                            evidence.tier
                            not in {
                                EvidenceTier.BENCH,
                                EvidenceTier.HIL,
                                EvidenceTier.PHYSICAL_DEVICE,
                            }
                        ):
                            raise ValueError(
                                "measured claims require bench, HIL, or "
                                "physical-device test evidence"
                            )
                elif ref_kind == "candidate":
                    candidate = self._store.get_design_candidate(identifier)
                    if (
                        candidate.project_id != project_id
                        or candidate.session_id != session_id
                    ):
                        raise ValueError(
                            "candidate claim evidence belongs to another "
                            "project or session"
                        )
                elif ref_kind == "proposal":
                    proposal = self._store.get_design_proposal(identifier)
                    if proposal.project_id != project_id:
                        raise ValueError(
                            "proposal claim evidence belongs to another project"
                        )
                elif ref_kind == "preview":
                    preview = self._store.get_change_preview(identifier)
                    if preview.project_id != project_id:
                        raise ValueError(
                            "preview claim evidence belongs to another project"
                        )
                else:
                    raise ValueError(
                        "conversational claim evidence reference kind is unsupported"
                    )
        except RecordNotFoundError as exc:
            raise ValueError(
                "conversational claim evidence reference was not found"
            ) from exc

        if kind is EvidenceClaimKind.SIMULATED and resolved_kinds != {"simulation"}:
            raise ValueError("simulated claims require only simulation evidence")
        if kind is EvidenceClaimKind.MEASURED and resolved_kinds != {
            "release-evidence"
        }:
            raise ValueError("measured claims require only release evidence")

    def append_design_transition(
        self,
        project_id: str,
        command: AppendDesignTransitionCommand,
        context: MutationContext,
    ) -> ServiceMutationResult:
        def build(
            now: datetime, _next_version: int
        ) -> tuple[dict[str, Any], Callable[[AtomicProjectWrite], None]]:
            if command.to_state is DesignConversationState.VERIFIED:
                self._validate_verified_transition_evidence(
                    project_id, command.evidence_refs
                )
            transition = DesignStateTransition(
                session_id=command.session_id,
                sequence=command.sequence,
                from_state=command.from_state,
                to_state=command.to_state,
                candidate_hash=command.candidate_hash,
                simulation_hash=command.simulation_hash,
                evidence_refs=command.evidence_refs,
                occurred_at=now,
            )
            record = StoredDesignStateTransition(
                project_id=project_id,
                session_id=transition.session_id,
                sequence=transition.sequence,
                transition_hash=canonical_sha256(transition),
                from_state=transition.from_state,
                to_state=transition.to_state,
                candidate_hash=transition.candidate_hash,
                simulation_hash=transition.simulation_hash,
                transition=transition,
                stored_at=now,
            )
            return (
                {"design_transition": record.model_dump(mode="json")},
                lambda atomic: atomic.append_design_state_transition(record),
            )

        return self._mutation(
            "append_design_transition", project_id, command, context, build
        )

    def _validate_verified_transition_evidence(
        self, project_id: str, evidence_refs: tuple[str, ...]
    ) -> None:
        try:
            for evidence_ref in evidence_refs:
                ref_kind, separator, evidence_id = evidence_ref.partition(":")
                if ref_kind != "release-evidence" or not separator or not evidence_id:
                    raise ValueError(
                        "verified transitions require release-evidence references"
                    )
                self._store.get_release_evidence(project_id, evidence_id)
        except RecordNotFoundError as exc:
            raise ValueError(
                "verified transition release evidence was not found in the project"
            ) from exc

    @staticmethod
    def _design_strategies(priority: GoalPriority) -> tuple[DesignStrategy, ...]:
        preferred = {
            GoalPriority.COST: DesignStrategy.COST_OPTIMIZED,
            GoalPriority.MINIMAL_CHANGE: DesignStrategy.MINIMAL_CHANGE,
            GoalPriority.PERFORMANCE: DesignStrategy.PERFORMANCE,
            GoalPriority.RELIABILITY: DesignStrategy.PERFORMANCE,
        }[priority]
        fallback = (
            DesignStrategy.COST_OPTIMIZED
            if preferred is DesignStrategy.MINIMAL_CHANGE
            else DesignStrategy.MINIMAL_CHANGE
        )
        return preferred, fallback

    @staticmethod
    def _tradeoff_scores(strategy: DesignStrategy) -> TradeoffScores:
        values = {
            DesignStrategy.MINIMAL_CHANGE: (70, 90, 55, 75, 75),
            DesignStrategy.PERFORMANCE: (40, 45, 95, 60, 90),
            DesignStrategy.COST_OPTIMIZED: (95, 60, 55, 65, 80),
            DesignStrategy.CUSTOM: (50, 50, 50, 50, 50),
        }[strategy]
        return TradeoffScores(
            cost=values[0],
            change=values[1],
            performance=values[2],
            risk=values[3],
            goal_satisfaction=values[4],
        )

    @staticmethod
    def _design_rationale(strategy: DesignStrategy, goal: str) -> str:
        return (
            f"Planning-only {strategy.value} alternative for goal: {goal}. "
            "The operator must select and verify actual source changes."
        )

    def create_external_evidence_plan(
        self,
        project_id: str,
        command: CreateExternalEvidencePlanCommand,
        context: MutationContext,
    ) -> ServiceMutationResult:
        def build(
            now: datetime, _next_version: int
        ) -> tuple[dict[str, Any], Callable[[AtomicProjectWrite], None]]:
            baseline = self._store.get_connector_snapshot(
                project_id, command.baseline_snapshot_id
            )
            asset_input = AssetInput(**command.asset_input.model_dump(mode="python"))
            source_refs = tuple(
                sorted(
                    baseline.snapshot.artifacts,
                    key=lambda item: (
                        item.source_system.value,
                        item.artifact_id,
                        item.source_revision,
                        item.content_hash,
                    ),
                )
            )
            scenario_hash = operating_scenario_hash(
                scenario_id=command.scenario_id,
                project_id=project_id,
                kind=command.scenario_kind,
                asset_input=asset_input,
                baseline_snapshot_id=baseline.snapshot_id,
                baseline_snapshot_hash=baseline.snapshot_hash,
                scenario_text=command.scenario_text,
                source_refs=source_refs,
                created_at=now,
            )
            scenario = OperatingScenario(
                scenario_id=command.scenario_id,
                project_id=project_id,
                kind=command.scenario_kind,
                asset_input=asset_input,
                baseline_snapshot_id=baseline.snapshot_id,
                baseline_snapshot_hash=baseline.snapshot_hash,
                scenario_text=command.scenario_text,
                source_refs=source_refs,
                created_at=now,
                scenario_hash=scenario_hash,
            )
            hint_source_refs = (f"operating_scenario:{scenario.scenario_hash}",)
            hints = tuple(
                ExternalEvidenceRequirementHint(
                    test_id=item.test_id,
                    required_tier=item.required_tier,
                    acceptance_criteria=(
                        AcceptanceCriterion(
                            criterion_id=(
                                f"operator-{item.required_tier.value}-acceptance"
                            ),
                            metric="operator-defined-acceptance",
                            operator="exists",
                            expected=item.acceptance_criteria,
                            source_refs=hint_source_refs,
                        ),
                    ),
                )
                for item in sorted(
                    command.required_evidence,
                    key=lambda value: (value.required_tier.value, value.test_id),
                )
            )
            plan = build_required_external_evidence_plan(
                scenario,
                source_refs,
                generated_at=now,
                external_tool_ref=command.external_tool_ref,
                required_evidence_hints=hints,
            )
            record = StoredExternalEvidencePlan(
                project_id=project_id,
                plan_hash=plan.plan_hash,
                scenario_id=scenario.scenario_id,
                scenario_hash=scenario.scenario_hash,
                baseline_snapshot_id=scenario.baseline_snapshot_id,
                baseline_snapshot_hash=scenario.baseline_snapshot_hash,
                plan=plan,
                stored_at=now,
            )
            return (
                {"external_evidence_plan": record.model_dump(mode="json")},
                lambda atomic: atomic.insert_external_evidence_plan(record),
            )

        return self._mutation(
            "create_external_evidence_plan", project_id, command, context, build
        )

    def verify_plan(
        self,
        project_id: str,
        command: VerifyPlanCommand,
        context: MutationContext,
    ) -> ServiceMutationResult:
        def build(
            now: datetime, _next_version: int
        ) -> tuple[dict[str, Any], Callable[[AtomicProjectWrite], None]]:
            preview = self._store.get_change_preview(command.preview_hash)
            assessment = self._store.get_change_assessment(
                command.actual_change_analysis_hash
            )
            if preview.project_id != project_id or assessment.project_id != project_id:
                raise ValueError("plan verification inputs belong to another project")
            verification = verify_plan_against_actual(
                preview.preview,
                preview.scenario,
                assessment.assessment,
                verified_at=now,
            )
            record = StoredPlanVerification(
                project_id=project_id,
                verification_hash=verification.verification_hash,
                scenario_id=verification.scenario_id,
                preview_hash=verification.preview_hash,
                actual_change_analysis_hash=verification.actual_change_analysis_hash,
                verification=verification,
                stored_at=now,
            )
            return (
                {"plan_verification": record.model_dump(mode="json")},
                lambda atomic: atomic.insert_plan_verification(record),
            )

        return self._mutation("verify_plan", project_id, command, context, build)

    def verify_external_evidence_plan(
        self,
        project_id: str,
        command: VerifyExternalEvidencePlanCommand,
        context: MutationContext,
    ) -> ServiceMutationResult:
        def build(
            now: datetime, _next_version: int
        ) -> tuple[dict[str, Any], Callable[[AtomicProjectWrite], None]]:
            stored_plan = self._store.get_external_evidence_plan(
                command.evidence_plan_hash
            )
            assessment = self._store.get_change_assessment(
                command.actual_change_analysis_hash
            )
            if (
                stored_plan.project_id != project_id
                or assessment.project_id != project_id
            ):
                raise ValueError(
                    "external evidence verification inputs belong to another project"
                )
            if (
                stored_plan.baseline_snapshot_id
                != assessment.assessment.from_snapshot_id
                or stored_plan.baseline_snapshot_hash
                != assessment.assessment.from_snapshot_hash
            ):
                raise ValueError(
                    "external evidence plan baseline does not match "
                    "actual change analysis"
                )
            required_by_test = {
                (item.tier, item.test_id): item.required_evidence_id
                for item in stored_plan.plan.required_evidence
            }
            bindings: list[ExternalEvidenceImportBinding] = []
            for evidence_id in command.imported_evidence_ids:
                raw = self._store.get_release_evidence(project_id, evidence_id)
                if raw.change_analysis_hash != command.actual_change_analysis_hash:
                    raise ValueError(
                        "imported evidence belongs to another change analysis"
                    )
                if not isinstance(raw.evidence, TestExecutionEvidence):
                    raise ValueError(
                        "external evidence verification requires test evidence"
                    )
                required_id = required_by_test.get(
                    (raw.evidence.tier, raw.evidence.test_id),
                    f"unexpected:{raw.evidence.evidence_id}",
                )
                bindings.append(
                    ExternalEvidenceImportBinding(
                        required_evidence_id=required_id,
                        evidence=raw.evidence,
                        evidence_hash=raw.evidence_hash,
                    )
                )
            verification = verify_external_evidence_plan(
                stored_plan.plan,
                tuple(bindings),
                actual_change_analysis_hash=command.actual_change_analysis_hash,
                verified_at=now,
                verification_id=(
                    "external-verification:"
                    f"{canonical_sha256(command).removeprefix('sha256:')}"
                ),
            )
            record = StoredExternalEvidencePlanVerification(
                project_id=project_id,
                verification_hash=verification.verification_hash,
                plan_hash=stored_plan.plan_hash,
                actual_change_analysis_hash=command.actual_change_analysis_hash,
                verification=verification,
                stored_at=now,
            )
            return (
                {"external_evidence_plan_verification": record.model_dump(mode="json")},
                lambda atomic: atomic.insert_external_evidence_plan_verification(
                    record
                ),
            )

        return self._mutation(
            "verify_external_evidence_plan", project_id, command, context, build
        )

    def _materialize_change_scenario(
        self,
        project_id: str,
        command: CreateChangePreviewCommand,
        baseline: StoredConnectorSnapshot,
        created_at: datetime,
    ) -> ChangeScenario:
        if baseline.project_id != project_id:
            raise ValueError("change preview baseline belongs to another project")
        baseline_refs = set(baseline.snapshot.artifacts)
        changes = tuple(
            sorted(
                (
                    self._materialize_planned_change(item, baseline_refs)
                    for item in command.changes
                ),
                key=_planned_change_key,
            )
        )
        asset_input = AssetInput(**command.asset_input.model_dump(mode="python"))
        scenario_hash = change_scenario_hash(
            scenario_id=command.scenario_id,
            project_id=project_id,
            asset_input=asset_input,
            intent_kind=command.intent_kind,
            baseline_snapshot_id=baseline.snapshot_id,
            baseline_snapshot_hash=baseline.snapshot_hash,
            proposed_hardware_revision_id=command.proposed_hardware_revision_id,
            created_at=created_at,
            changes=changes,
        )
        return ChangeScenario(
            scenario_id=command.scenario_id,
            project_id=project_id,
            asset_input=asset_input,
            intent_kind=command.intent_kind,
            baseline_snapshot_id=baseline.snapshot_id,
            baseline_snapshot_hash=baseline.snapshot_hash,
            proposed_hardware_revision_id=command.proposed_hardware_revision_id,
            created_at=created_at,
            changes=changes,
            scenario_hash=scenario_hash,
        )

    def _materialize_planned_change(
        self,
        draft: PlannedComponentChangeDraft,
        baseline_refs: set[ExternalArtifactRef],
    ) -> PlannedComponentChange:
        before = (
            self._materialize_component(
                draft.before, baseline_refs, require_baseline=True
            )
            if draft.before is not None
            else None
        )
        after = (
            self._materialize_component(
                draft.after, baseline_refs, require_baseline=False
            )
            if draft.after is not None
            else None
        )
        return PlannedComponentChange(
            change_id=draft.change_id,
            action=draft.action,
            before=before,
            after=after,
            affected_domains=_ALL_DOMAINS,
            required_actions=_DEFAULT_PLANNED_ACTIONS,
            rationale=draft.rationale,
        )

    def _materialize_component(
        self,
        draft: ComponentSpecificationDraft,
        baseline_refs: set[ExternalArtifactRef],
        *,
        require_baseline: bool,
    ) -> ComponentSpecification:
        if require_baseline and draft.source_ref not in baseline_refs:
            raise ValueError("planned before component is not in baseline snapshot")
        specification_hash = component_specification_hash(
            component_id=draft.component_id,
            manufacturer=draft.manufacturer,
            part_number=draft.part_number,
            quantity=draft.quantity,
            source_ref=draft.source_ref,
            interface_contract=draft.interface_contract,
            quote=draft.quote,
            attributes=draft.attributes,
        )
        return ComponentSpecification(
            component_id=draft.component_id,
            manufacturer=draft.manufacturer,
            part_number=draft.part_number,
            quantity=draft.quantity,
            source_ref=draft.source_ref,
            interface_contract=draft.interface_contract,
            quote=draft.quote,
            attributes=draft.attributes,
            specification_hash=specification_hash,
        )

    def ingest_release_evidence(
        self,
        project_id: str,
        command: IngestReleaseEvidenceCommand,
        context: MutationContext,
    ) -> ServiceMutationResult:
        approval = command.automatic_reverification
        if approval is not None:
            if approval.approved_by != context.actor_id:
                raise ValueError(
                    "automatic reverification approval actor does not match"
                )
            if approval.approved_at > self._clock():
                raise ValueError(
                    "automatic reverification approval cannot be from the future"
                )
            self._authorize(
                project_id=project_id,
                org_id=context.org_id,
                actor_id=context.actor_id,
                permission=Permission.DECIDE_RELEASE,
                event_id=f"audit:{secrets.token_hex(24)}",
                occurred_at=self._clock(),
                persist=False,
            )

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
            approval = command.automatic_reverification
            trigger: StoredAutomaticReverificationTrigger | None = None
            if approval is not None:
                self._require_automatic_reverification_lineage(project_id, approval)
                draft = StoredAutomaticReverificationTrigger.model_construct(
                    trigger_hash="sha256:" + "0" * 64,
                    project_id=project_id,
                    analysis_hash=approval.analysis_hash,
                    candidate_hash=approval.candidate_hash,
                    preview_hash=approval.preview_hash,
                    plan_verification_hash=approval.plan_verification_hash,
                    evidence_hash=approval.evidence_hash,
                    approval_id=approval.approval_id,
                    approved_by=approval.approved_by,
                    approved_at=approval.approved_at,
                    local_installation_id=context.local_installation_id,
                    org_id=context.org_id,
                    actor_id=context.actor_id,
                    created_at=now,
                )
                trigger = StoredAutomaticReverificationTrigger.model_validate(
                    draft.model_copy(
                        update={"trigger_hash": canonical_sha256(draft)}
                    ).model_dump(mode="python")
                )

            def apply(atomic: AtomicProjectWrite) -> None:
                atomic.insert_release_evidence(record)
                if trigger is not None:
                    atomic.insert_automatic_reverification_trigger(trigger)

            payload: dict[str, Any] = {
                "release_evidence": record.model_dump(mode="json")
            }
            if trigger is not None:
                payload["automatic_reverification_trigger"] = trigger.model_dump(
                    mode="json"
                )
            return (
                payload,
                apply,
            )

        ingested = self._mutation(
            "ingest_release_evidence", project_id, command, context, build
        )
        if approval is None:
            return ingested
        trigger = StoredAutomaticReverificationTrigger.model_validate(
            ingested.payload["automatic_reverification_trigger"]
        )
        evaluation = self._process_automatic_reverification_trigger(trigger)
        stored_decision = evaluation.payload["release_decision"]
        decision = stored_decision["decision"]
        status_value = str(decision["report"]["status"])
        outcome_status: Literal["READY", "BLOCKED"]
        if status_value == ReleaseStatus.READY.value:
            outcome_status = "READY"
        elif status_value == ReleaseStatus.BLOCKED.value:
            outcome_status = "BLOCKED"
        else:
            raise RuntimeError("release evaluator returned an impossible status")
        outcome = DeterministicReleaseOutcome(
            status=outcome_status,
            decision_hash=str(stored_decision["decision_hash"]),
        )
        next_step = next_step_for_release_outcome(outcome)
        payload = dict(ingested.payload) | {
            "automatic_reverification": {
                "approval_id": approval.approval_id,
                "candidate_hash": approval.candidate_hash,
                "preview_hash": approval.preview_hash,
                "plan_verification_hash": approval.plan_verification_hash,
                "evidence_hash": approval.evidence_hash,
                "release_status": outcome.status,
                "release_decision_hash": outcome.decision_hash,
                "trigger_hash": trigger.trigger_hash,
                "receipt_hash": evaluation.payload["automatic_reverification_receipt"][
                    "receipt_hash"
                ],
                "next_step": next_step.model_dump(mode="json"),
            },
            "project_version": evaluation.project_version,
        }
        return ServiceMutationResult(
            status=201,
            payload=payload,
            response_json=_canonical_json(payload),
            project_version=evaluation.project_version,
            replayed=ingested.replayed and evaluation.replayed,
        )

    def _process_automatic_reverification_trigger(
        self, trigger: StoredAutomaticReverificationTrigger
    ) -> ServiceMutationResult:
        try:
            receipt = self._store.get_automatic_reverification_receipt(
                trigger.trigger_hash
            )
        except RecordNotFoundError:
            receipt = None
        if receipt is not None:
            decision = self._store.get_release_decision(receipt.decision_hash)
            project_version = self._store.get_project(trigger.project_id).version
            payload = {
                "release_decision": decision.model_dump(mode="json"),
                "automatic_reverification_receipt": receipt.model_dump(mode="json"),
                "project_version": project_version,
            }
            return ServiceMutationResult(
                status=201,
                payload=payload,
                response_json=_canonical_json(payload),
                project_version=project_version,
                replayed=True,
            )

        auto_key = "auto-reverify-" + trigger.trigger_hash.removeprefix("sha256:")[:48]
        for attempt in range(3):
            project_version = self._store.get_project(trigger.project_id).version
            try:
                return self.evaluate_release(
                    trigger.project_id,
                    EvaluateReleaseCommand(
                        analysis_hash=trigger.analysis_hash,
                        automatic_trigger_hash=trigger.trigger_hash,
                    ),
                    MutationContext(
                        local_installation_id=trigger.local_installation_id,
                        idempotency_key=auto_key,
                        expected_project_version=project_version,
                        org_id=trigger.org_id,
                        actor_id=trigger.actor_id,
                    ),
                )
            except VersionConflictError:
                try:
                    receipt = self._store.get_automatic_reverification_receipt(
                        trigger.trigger_hash
                    )
                except RecordNotFoundError:
                    if attempt == 2:
                        raise
                    continue
                return self._process_automatic_reverification_trigger(trigger)
        raise RuntimeError("automatic reverification retry loop exhausted")

    def _require_automatic_reverification_lineage(
        self, project_id: str, approval: AutomaticReverificationApproval
    ) -> None:
        candidate = self._store.get_design_candidate(approval.candidate_hash)
        proposal = self._store.get_design_proposal(candidate.proposal_hash)
        verification = self._store.get_plan_verification(
            approval.plan_verification_hash
        )
        if (
            candidate.project_id != project_id
            or proposal.project_id != project_id
            or verification.project_id != project_id
        ):
            raise ValueError(
                "automatic reverification lineage belongs to another project"
            )
        if (
            proposal.preview_hash != approval.preview_hash
            or verification.preview_hash != approval.preview_hash
            or verification.actual_change_analysis_hash != approval.analysis_hash
        ):
            raise ValueError(
                "automatic reverification approval does not bind candidate, "
                "preview, and analysis"
            )
        if not verification.verification.matches_plan:
            raise ValueError(
                "automatic reverification requires a successful plan verification"
            )
        if (
            candidate.candidate.confirmed_at > approval.approved_at
            or verification.verification.verified_at > approval.approved_at
        ):
            raise ValueError(
                "automatic reverification approval predates its verified design lineage"
            )

    def process_pending_automatic_reverifications(
        self, project_id: str | None = None
    ) -> tuple[ServiceMutationResult, ...]:
        completed: list[ServiceMutationResult] = []
        for trigger in self._store.list_pending_automatic_reverification_triggers(
            project_id
        ):
            try:
                completed.append(
                    self._process_automatic_reverification_trigger(trigger)
                )
            except Exception:
                failures = tuple(
                    item
                    for item in self._store.list_automatic_reverification_failures(
                        trigger.project_id
                    )
                    if item.trigger_hash == trigger.trigger_hash
                )
                draft = StoredAutomaticReverificationFailure.model_construct(
                    failure_hash="sha256:" + "0" * 64,
                    trigger_hash=trigger.trigger_hash,
                    project_id=trigger.project_id,
                    attempt=len(failures) + 1,
                    error_code="automatic_reverification_failed",
                    failed_at=self._clock(),
                )
                failure = StoredAutomaticReverificationFailure.model_validate(
                    draft.model_copy(
                        update={"failure_hash": canonical_sha256(draft)}
                    ).model_dump(mode="python")
                )
                self._store.append_automatic_reverification_failure(failure)
        return tuple(completed)

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
            trigger: StoredAutomaticReverificationTrigger | None = None
            if command.automatic_trigger_hash is not None:
                trigger = self._store.get_automatic_reverification_trigger(
                    command.automatic_trigger_hash
                )
                if (
                    trigger.project_id != project_id
                    or trigger.analysis_hash != command.analysis_hash
                ):
                    raise ValueError(
                        "automatic reverification trigger targets another release"
                    )
                approval = AutomaticReverificationApproval(
                    approval_id=trigger.approval_id,
                    analysis_hash=trigger.analysis_hash,
                    candidate_hash=trigger.candidate_hash,
                    preview_hash=trigger.preview_hash,
                    plan_verification_hash=trigger.plan_verification_hash,
                    evidence_hash=trigger.evidence_hash,
                    approved_by=trigger.approved_by,
                    approved_at=trigger.approved_at,
                )
                self._require_automatic_reverification_lineage(project_id, approval)
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
            receipt: StoredAutomaticReverificationReceipt | None = None
            if trigger is not None:
                receipt_status: Literal["READY", "BLOCKED"] = (
                    "READY"
                    if decision.report.status is ReleaseStatus.READY
                    else "BLOCKED"
                )
                receipt_draft = StoredAutomaticReverificationReceipt.model_construct(
                    receipt_hash="sha256:" + "0" * 64,
                    trigger_hash=trigger.trigger_hash,
                    project_id=project_id,
                    analysis_hash=trigger.analysis_hash,
                    candidate_hash=trigger.candidate_hash,
                    preview_hash=trigger.preview_hash,
                    plan_verification_hash=trigger.plan_verification_hash,
                    decision_hash=record.decision_hash,
                    release_status=receipt_status,
                    completed_at=now,
                )
                receipt = StoredAutomaticReverificationReceipt.model_validate(
                    receipt_draft.model_copy(
                        update={"receipt_hash": canonical_sha256(receipt_draft)}
                    ).model_dump(mode="python")
                )

            def apply(atomic: AtomicProjectWrite) -> None:
                atomic.append_release_decision(record)
                if receipt is not None:
                    atomic.insert_automatic_reverification_receipt(receipt)

            payload: dict[str, Any] = {
                "release_decision": record.model_dump(mode="json")
            }
            if receipt is not None:
                payload["automatic_reverification_receipt"] = receipt.model_dump(
                    mode="json"
                )
            return (
                payload,
                apply,
            )

        return self._mutation("evaluate_release", project_id, command, context, build)

    def create_release_diagnosis(
        self,
        project_id: str,
        command: CreateReleaseDiagnosisCommand,
        context: MutationContext,
    ) -> ServiceMutationResult:
        def build(
            now: datetime, _next_version: int
        ) -> tuple[dict[str, Any], Callable[[AtomicProjectWrite], None]]:
            stored_decision = self._store.get_release_decision(command.decision_hash)
            if stored_decision.project_id != project_id:
                raise ValueError(
                    "release diagnosis decision belongs to another project"
                )
            if stored_decision.decision.report.status is not ReleaseStatus.BLOCKED:
                raise ValueError("release diagnosis requires a BLOCKED decision")
            diagnosis = build_blocker_diagnosis(stored_decision.decision)
            fix_sets = tuple(
                sorted(
                    (
                        self._build_fix_set(project_id, diagnosis, item)
                        for item in diagnosis.items
                    ),
                    key=lambda item: item.diagnosis_item_hash,
                )
            )
            record = StoredReleaseDiagnosis(
                project_id=project_id,
                diagnosis_hash=diagnosis.diagnosis_hash,
                decision_hash=stored_decision.decision_hash,
                diagnosis=diagnosis,
                fix_proposal_sets=fix_sets,
                stored_at=now,
            )
            return (
                {"release_diagnosis": self._release_diagnosis_payload(record)},
                lambda atomic: atomic.insert_release_diagnosis(record),
            )

        return self._mutation(
            "create_release_diagnosis", project_id, command, context, build
        )

    def _build_fix_set(
        self,
        project_id: str,
        diagnosis: BlockerDiagnosis,
        item: DiagnosisItem,
    ) -> FixProposalSet:
        reverify = self._reverify_requirement(item)
        domains = tuple(sorted(item.affected_domains, key=lambda domain: domain.value))
        targeted_changes = tuple(
            ProposedChange(
                change_ref=f"{item.item_id}:{domain.value}:targeted",
                domain=domain,
                description=(
                    f"Resolve {item.blocker_code} in {domain.value} and update linked "
                    "engineering artifacts."
                ),
            )
            for domain in domains
        )
        evidence_changes = (
            ProposedChange(
                change_ref=f"{item.item_id}:test:evidence-first",
                domain=ArtifactDomain.TEST,
                description=(
                    f"Collect the required verification for {item.blocker_code} before "
                    "claiming a root cause or applying a broader design change."
                ),
            ),
        )
        targeted = build_fix_proposal(
            fix_id=f"{item.item_id}-targeted-remediation",
            diagnosis_item=item,
            affected_domains=domains,
            tradeoff_scores=TradeoffScores(
                cost=55, change=65, performance=75, risk=70, goal_satisfaction=85
            ),
            changes=targeted_changes,
            required_reverify_tests=(reverify,),
            rationale=(
                "Targeted planning recommendation bound to the blocker. It does not "
                "write source data or satisfy release evidence."
            ),
        )
        evidence_first = build_fix_proposal(
            fix_id=f"{item.item_id}-evidence-first",
            diagnosis_item=item,
            affected_domains=(ArtifactDomain.TEST,),
            tradeoff_scores=TradeoffScores(
                cost=80, change=90, performance=45, risk=85, goal_satisfaction=65
            ),
            changes=evidence_changes,
            required_reverify_tests=(reverify,),
            rationale=(
                "Evidence-first planning recommendation for an uncertain blocker. "
                "Further verification is required before remediation is selected."
            ),
        )
        return build_fix_proposal_set(
            project_id=project_id,
            diagnosis=diagnosis,
            diagnosis_item=item,
            proposals=(targeted, evidence_first),
        )

    @staticmethod
    def _reverify_requirement(item: DiagnosisItem) -> RequiredReverifyTest:
        parts = item.blocker_code.split(":")
        if len(parts) == 4 and parts[0] == "retest":
            try:
                tier = EvidenceTier(parts[2])
            except ValueError:
                tier = EvidenceTier.STATIC
            return RequiredReverifyTest(
                test_id=parts[1],
                required_tier=tier,
                reason=f"Re-verify release blocker {item.blocker_code}",
            )
        rule_id = item.blocker_code.removeprefix("finding:")
        return RequiredReverifyTest(
            test_id=f"verify-{rule_id}",
            required_tier=EvidenceTier.STATIC,
            reason=f"Verify remediation for release blocker {item.blocker_code}",
        )

    def create_resolution_plan(
        self,
        project_id: str,
        command: CreateResolutionPlanCommand,
        context: MutationContext,
    ) -> ServiceMutationResult:
        def build(
            now: datetime, _next_version: int
        ) -> tuple[dict[str, Any], Callable[[AtomicProjectWrite], None]]:
            diagnosis = self._store.get_release_diagnosis(command.diagnosis_hash)
            if diagnosis.project_id != project_id:
                raise ValueError("resolution diagnosis belongs to another project")
            matches = tuple(
                (fix_set, fix)
                for fix_set in diagnosis.fix_proposal_sets
                for fix in fix_set.proposals
                if fix.fix_hash == command.fix_proposal_hash
            )
            if len(matches) != 1:
                raise ValueError("fix proposal is not part of the stored diagnosis")
            fix_set, fix = matches[0]
            selection = select_fix_proposal(
                fix_set=fix_set,
                selected_fix_id=fix.fix_id,
                source_decision_hash=diagnosis.decision_hash,
                selected_by=context.local_installation_id,
                selected_at=now,
            )
            record = StoredResolutionPlan(
                project_id=project_id,
                plan_hash=selection.selection_hash,
                diagnosis_hash=diagnosis.diagnosis_hash,
                fix_proposal_hash=fix.fix_hash,
                selection=selection,
                stored_at=now,
            )
            return (
                {"resolution_plan": self._resolution_plan_payload(record)},
                lambda atomic: atomic.insert_resolution_plan(record),
            )

        return self._mutation(
            "create_resolution_plan", project_id, command, context, build
        )

    @staticmethod
    def _design_proposal_payload(record: StoredDesignProposalSet) -> dict[str, Any]:
        proposal = record.proposal
        alternatives = []
        for item in proposal.alternatives:
            payload = item.model_dump(mode="json")
            payload.update(
                {
                    "title": item.strategy.value.replace("_", " ").title(),
                    "expected_impact": (
                        ", ".join(domain.value for domain in item.affected_domains)
                    ),
                    "risk": item.tradeoff_scores.risk,
                    "proposal_hash": item.alternative_hash,
                    "required_evidence": list(item.missing_information),
                }
            )
            alternatives.append(payload)
        return proposal.model_dump(mode="json") | {
            "goal": proposal.goal.goal_statement,
            "alternatives": alternatives,
            "stored_at": record.stored_at.isoformat(),
        }

    @staticmethod
    def _release_diagnosis_payload(
        record: StoredReleaseDiagnosis,
    ) -> dict[str, Any]:
        blockers = []
        for item in record.diagnosis.items:
            payload = item.model_dump(mode="json")
            payload["likely_cause"] = (
                item.hypotheses[0].statement
                if item.hypotheses
                else "Evidence is insufficient; additional verification is required."
            )
            blockers.append(payload)
        fixes = []
        for fix_set in record.fix_proposal_sets:
            for fix in fix_set.proposals:
                payload = fix.model_dump(mode="json")
                payload.update(
                    {
                        "fix_proposal_hash": fix.fix_hash,
                        "title": fix.fix_id.replace("-", " ").title(),
                        "summary": fix.rationale,
                        "expected_effect": ", ".join(
                            item.description for item in fix.changes
                        ),
                        "required_verification": [
                            f"{item.test_id}:{item.required_tier.value}"
                            for item in fix.required_reverify_tests
                        ],
                    }
                )
                fixes.append(payload)
        return record.diagnosis.model_dump(mode="json") | {
            "blocker_diagnoses": blockers,
            "fix_recommendations": fixes,
            "stored_at": record.stored_at.isoformat(),
        }

    @staticmethod
    def _resolution_plan_payload(record: StoredResolutionPlan) -> dict[str, Any]:
        fix = record.selection.selected_fix
        return record.model_dump(mode="json") | {
            "plan_draft": {
                "title": f"Resolve {fix.blocker_code}",
                "goal": fix.rationale,
                "priority": GoalPriority.RELIABILITY.value,
                "constraints": [
                    "planning-only",
                    "no-source-writeback",
                    "re-verification-required",
                ],
                "rationale": fix.rationale,
                "change_id": fix.changes[0].change_ref,
            }
        }

    def get_project(self, project_id: str) -> dict[str, Any]:
        return self._store.get_project(project_id).model_dump(mode="json")

    def list_connectors(self) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json") for item in self._connectors.list_manifests()
        ]

    def operational_health(self) -> dict[str, Any]:
        report = (
            SQLiteOperationsService(self._store.path)
            .health(checked_at=self._clock())
            .model_dump(mode="json")
        )
        pending = self._store.list_pending_automatic_reverification_triggers()
        pending_hashes = {item.trigger_hash for item in pending}
        failures = tuple(
            item
            for item in self._store.list_automatic_reverification_failures()
            if item.trigger_hash in pending_hashes
        )
        report["pending_automatic_reverifications"] = len(pending)
        report["automatic_reverification_failures"] = len(failures)
        if report["status"] == "READY" and (pending or failures):
            report["status"] = "DEGRADED"
        return report

    def list_knowledge_sources(self, project_id: str) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self._store.list_knowledge_sources(project_id)
        ]

    def get_knowledge_source(self, project_id: str, source_id: str) -> dict[str, Any]:
        return self._store.get_knowledge_source(project_id, source_id).model_dump(
            mode="json"
        )

    def list_conversation_results(self, project_id: str) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self._store.list_runtime_results(project_id)
        ]

    def get_conversation_result(
        self, project_id: str, request_id: str
    ) -> dict[str, Any]:
        return self._store.get_runtime_result(project_id, request_id).model_dump(
            mode="json"
        )

    def list_cad_geometries(self, project_id: str) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self._store.list_cad_geometries(project_id)
        ]

    def get_cad_geometry(self, project_id: str, asset_id: str) -> dict[str, Any]:
        return self._store.get_cad_geometry(project_id, asset_id).model_dump(
            mode="json"
        )

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

    def list_change_previews(self, project_id: str) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self._store.list_change_previews(project_id)
        ]

    def get_change_preview(self, project_id: str, preview_hash: str) -> dict[str, Any]:
        value = self._store.get_change_preview(preview_hash)
        if value.project_id != project_id:
            raise KeyError("change preview not found")
        return value.model_dump(mode="json")

    def list_design_proposals(self, project_id: str) -> list[dict[str, Any]]:
        return [
            self._design_proposal_payload(item)
            for item in self._store.list_design_proposals(project_id)
        ]

    def get_design_proposal(
        self, project_id: str, proposal_hash: str
    ) -> dict[str, Any]:
        value = self._store.get_design_proposal(proposal_hash)
        if value.project_id != project_id:
            raise KeyError("design proposal not found")
        return self._design_proposal_payload(value)

    def list_design_candidates(self, project_id: str) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self._store.list_design_candidates(project_id)
        ]

    def get_design_candidate(
        self, project_id: str, candidate_hash: str
    ) -> dict[str, Any]:
        value = self._store.get_design_candidate(candidate_hash)
        if value.project_id != project_id:
            raise KeyError("design candidate not found")
        return value.model_dump(mode="json")

    def list_simulation_bindings(self, project_id: str) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self._store.list_simulation_bindings(project_id)
        ]

    def get_simulation_binding(
        self, project_id: str, simulation_hash: str
    ) -> dict[str, Any]:
        value = self._store.get_simulation_binding(simulation_hash)
        if value.project_id != project_id:
            raise KeyError("simulation binding not found")
        return value.model_dump(mode="json")

    def list_conversational_claims(self, project_id: str) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self._store.list_evidence_claims(project_id)
        ]

    def get_conversational_claim(
        self, project_id: str, claim_hash: str
    ) -> dict[str, Any]:
        value = self._store.get_evidence_claim(claim_hash)
        if value.project_id != project_id:
            raise KeyError("conversational evidence claim not found")
        return value.model_dump(mode="json")

    def list_design_transitions(
        self, project_id: str, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self._store.list_design_state_transitions(
                project_id, session_id
            )
        ]

    def list_plan_verifications(self, project_id: str) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self._store.list_plan_verifications(project_id)
        ]

    def get_plan_verification(
        self, project_id: str, verification_hash: str
    ) -> dict[str, Any]:
        value = self._store.get_plan_verification(verification_hash)
        if value.project_id != project_id:
            raise KeyError("plan verification not found")
        return value.model_dump(mode="json")

    def list_external_evidence_plans(self, project_id: str) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self._store.list_external_evidence_plans(project_id)
        ]

    def get_external_evidence_plan(
        self, project_id: str, plan_hash: str
    ) -> dict[str, Any]:
        value = self._store.get_external_evidence_plan(plan_hash)
        if value.project_id != project_id:
            raise KeyError("external evidence plan not found")
        return value.model_dump(mode="json")

    def list_external_evidence_plan_verifications(
        self, project_id: str
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self._store.list_external_evidence_plan_verifications(
                project_id
            )
        ]

    def get_external_evidence_plan_verification(
        self, project_id: str, verification_hash: str
    ) -> dict[str, Any]:
        value = self._store.get_external_evidence_plan_verification(verification_hash)
        if value.project_id != project_id:
            raise KeyError("external evidence plan verification not found")
        return value.model_dump(mode="json")

    def get_release_evidence(self, project_id: str, evidence_id: str) -> dict[str, Any]:
        value = self._store.get_release_evidence(project_id, evidence_id)
        return value.model_dump(mode="json")

    def list_release_evidence(self, project_id: str) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self._store.list_release_evidence(project_id)
        ]

    def list_release_decisions(self, project_id: str) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self._store.list_release_decisions(project_id)
        ]

    def get_release_decision(
        self, project_id: str, decision_hash: str
    ) -> dict[str, Any]:
        value = self._store.get_release_decision(decision_hash)
        if value.project_id != project_id:
            raise KeyError("release decision not found")
        return value.model_dump(mode="json")

    def list_release_diagnoses(self, project_id: str) -> list[dict[str, Any]]:
        return [
            self._release_diagnosis_payload(item)
            for item in self._store.list_release_diagnoses(project_id)
        ]

    def get_release_diagnosis(
        self, project_id: str, diagnosis_hash: str
    ) -> dict[str, Any]:
        value = self._store.get_release_diagnosis(diagnosis_hash)
        if value.project_id != project_id:
            raise KeyError("release diagnosis not found")
        return self._release_diagnosis_payload(value)

    def list_resolution_plans(self, project_id: str) -> list[dict[str, Any]]:
        return [
            self._resolution_plan_payload(item)
            for item in self._store.list_resolution_plans(project_id)
        ]

    def get_resolution_plan(self, project_id: str, plan_hash: str) -> dict[str, Any]:
        value = self._store.get_resolution_plan(plan_hash)
        if value.project_id != project_id:
            raise KeyError("resolution plan not found")
        return self._resolution_plan_payload(value)


__all__ = [
    "AnalyzeChangeCommand",
    "AutomaticReverificationApproval",
    "AppendDesignTransitionCommand",
    "BindDesignSimulationCommand",
    "CaptureSnapshotCommand",
    "ComponentSpecificationDraft",
    "CreateChangePreviewCommand",
    "ConfirmDesignCandidateCommand",
    "CreateDesignProposalCommand",
    "CreateExternalEvidencePlanCommand",
    "CreateProjectCommand",
    "CreateReleaseDiagnosisCommand",
    "CreateResolutionPlanCommand",
    "EvaluateReleaseCommand",
    "ExternalEvidenceRequestDraft",
    "IngestReleaseEvidenceCommand",
    "MutationContext",
    "PlannedComponentChangeDraft",
    "RecordConversationalClaimCommand",
    "ReleaseIntegrationService",
    "ServiceMutationResult",
    "VerifyPlanCommand",
    "VerifyExternalEvidencePlanCommand",
]
