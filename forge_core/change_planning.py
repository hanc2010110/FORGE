from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from pydantic import Field, field_serializer, field_validator, model_validator

from forge_core.change_management import (
    ArtifactDomain,
    ExternalArtifactRef,
    RetestRequirement,
    RetestStatus,
    SourceSystem,
)
from forge_core.constraints import InterfaceContract, QuoteSnapshot
from forge_core.hashing import canonical_sha256
from forge_core.models import ContractModel, Quantity


class PlannedChangeAction(StrEnum):
    ADD = "add"
    REMOVE = "remove"
    REPLACE = "replace"


class PreviewRecommendation(StrEnum):
    CHANGES_REQUIRED = "changes_required"
    INCOMPATIBLE = "incompatible"
    INDETERMINATE = "indeterminate"


class PreviewFindingSeverity(StrEnum):
    RISK = "risk"
    INCOMPATIBILITY = "incompatibility"
    MISSING_INFORMATION = "missing_information"


class PlanDeviationKind(StrEnum):
    MISSING_PLANNED_CHANGE = "missing_planned_change"
    UNPLANNED_ACTUAL_CHANGE = "unplanned_actual_change"
    DOMAIN_MISMATCH = "domain_mismatch"
    RETEST_GAP = "retest_gap"
    FINDING_GAP = "finding_gap"


class AssetInputKind(StrEnum):
    CONNECTED_DEVICE = "connected_device"
    CAD_MODEL = "cad_model"
    DESIGN_DRAWING = "design_drawing"
    PLM_SNAPSHOT = "plm_snapshot"
    MANUAL_SNAPSHOT = "manual_snapshot"


class PlanIntentKind(StrEnum):
    CHANGE_IMPACT = "change_impact"
    OPERATING_SCENARIO = "operating_scenario"


PLANNER_VERSION = "forge-change-preview-1.0.0"


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


def _unique_sorted_strings(value: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if len(value) != len(set(value)):
        raise ValueError(f"{field_name} must be unique")
    ordered = tuple(sorted(value))
    if value != ordered:
        raise ValueError(f"{field_name} must be in canonical order")
    return value


def _unique_sorted_domains(
    value: tuple[ArtifactDomain, ...], field_name: str
) -> tuple[ArtifactDomain, ...]:
    if len(value) != len(set(value)):
        raise ValueError(f"{field_name} must be unique")
    ordered = tuple(sorted(value, key=lambda item: item.value))
    if value != ordered:
        raise ValueError(f"{field_name} must be in canonical order")
    return value


def _component_key(item: ComponentSpecification) -> tuple[str, str, str]:
    return item.component_id, item.part_number, item.specification_hash


def _planned_change_key(
    item: PlannedComponentChange,
) -> tuple[str, str, tuple[str, str, str], tuple[str, str, str]]:
    before_key = ("", "", "")
    after_key = ("", "", "")
    if item.before is not None:
        before_key = _component_key(item.before)
    if item.after is not None:
        after_key = _component_key(item.after)
    return item.change_id, item.action.value, before_key, after_key


def _preview_finding_key(item: PreviewFinding) -> tuple[str, str]:
    return item.finding_id, item.rule_id


def _retest_key(item: RetestRequirement) -> tuple[str, str, str]:
    return item.retest_id, item.test_id, item.required_tier.value


def _deviation_key(item: PlanDeviation) -> tuple[str, str]:
    return item.deviation_id, item.kind.value


class ComponentSpecification(ContractModel):
    """Source-bound component data used for pre-change planning."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    component_id: str = Field(min_length=1)
    manufacturer: str = Field(min_length=1)
    part_number: str = Field(min_length=1)
    quantity: int = Field(ge=1)
    source_ref: ExternalArtifactRef
    interface_contract: InterfaceContract | None = None
    quote: QuoteSnapshot | None = None
    attributes: Mapping[str, Quantity] = Field(default_factory=dict)
    specification_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("attributes", mode="after")
    @classmethod
    def freeze_attributes(cls, value: Mapping[str, Quantity]) -> Mapping[str, Quantity]:
        return MappingProxyType(dict(sorted(value.items())))

    @field_serializer("attributes")
    def serialize_attributes(
        self, value: Mapping[str, Quantity]
    ) -> dict[str, Quantity]:
        return dict(value)

    @model_validator(mode="after")
    def component_specification_must_be_source_bound(
        self,
    ) -> ComponentSpecification:
        if self.source_ref.domain not in {ArtifactDomain.HARDWARE, ArtifactDomain.BOM}:
            raise ValueError("component specification must bind hardware or BOM source")
        if self.quote is not None and self.quote.part_number != self.part_number:
            raise ValueError("component quote part_number must match specification")
        expected = component_specification_hash(
            component_id=self.component_id,
            manufacturer=self.manufacturer,
            part_number=self.part_number,
            quantity=self.quantity,
            source_ref=self.source_ref,
            interface_contract=self.interface_contract,
            quote=self.quote,
            attributes=self.attributes,
        )
        if self.specification_hash != expected:
            raise ValueError("component specification hash does not match its payload")
        return self


class AssetInput(ContractModel):
    """Read-only machine, robot, CAD, drawing, or PLM source for a plan."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    asset_id: str = Field(min_length=1)
    kind: AssetInputKind
    source_system: SourceSystem
    source_locator: str = Field(min_length=1)
    source_revision: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    captured_at: datetime
    read_only: Literal[True] = True

    @field_validator("captured_at")
    @classmethod
    def captured_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "captured_at")

    @model_validator(mode="after")
    def source_system_must_match_asset_kind(self) -> AssetInput:
        allowed = {
            AssetInputKind.CONNECTED_DEVICE: {SourceSystem.MANUAL, SourceSystem.PLM},
            AssetInputKind.CAD_MODEL: {SourceSystem.CAD},
            AssetInputKind.DESIGN_DRAWING: {SourceSystem.CAD, SourceSystem.MANUAL},
            AssetInputKind.PLM_SNAPSHOT: {SourceSystem.PLM},
            AssetInputKind.MANUAL_SNAPSHOT: {SourceSystem.MANUAL},
        }
        if self.source_system not in allowed[self.kind]:
            raise ValueError("asset input source system does not match its kind")
        return self


class PlannedComponentChange(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    change_id: str = Field(min_length=1)
    action: PlannedChangeAction
    before: ComponentSpecification | None = None
    after: ComponentSpecification | None = None
    affected_domains: tuple[ArtifactDomain, ...] = Field(min_length=1)
    required_actions: tuple[str, ...] = Field(min_length=1)
    rationale: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("affected_domains")
    @classmethod
    def affected_domains_must_be_unique_and_canonical(
        cls, value: tuple[ArtifactDomain, ...]
    ) -> tuple[ArtifactDomain, ...]:
        return _unique_sorted_domains(value, "affected domains")

    @field_validator("required_actions", "rationale")
    @classmethod
    def strings_must_be_unique_and_canonical(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        return _unique_sorted_strings(value, "planned change strings")

    @model_validator(mode="after")
    def action_must_match_endpoints(self) -> PlannedComponentChange:
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
        if self.before is not None and self.after is not None:
            if self.before.component_id != self.after.component_id:
                raise ValueError("replace planned change must preserve component_id")
            if self.before.specification_hash == self.after.specification_hash:
                raise ValueError("replace planned change must change the specification")
        return self


class ChangeScenario(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    scenario_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    asset_input: AssetInput
    intent_kind: PlanIntentKind = PlanIntentKind.CHANGE_IMPACT
    baseline_snapshot_id: str = Field(min_length=1)
    baseline_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    proposed_hardware_revision_id: str = Field(min_length=1)
    created_at: datetime
    changes: tuple[PlannedComponentChange, ...] = Field(min_length=1)
    scenario_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("created_at")
    @classmethod
    def created_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "created_at")

    @model_validator(mode="after")
    def scenario_changes_and_hash_must_be_canonical(self) -> ChangeScenario:
        change_ids = [item.change_id for item in self.changes]
        if len(change_ids) != len(set(change_ids)):
            raise ValueError("scenario change IDs must be unique")
        ordered = tuple(sorted(self.changes, key=_planned_change_key))
        if self.changes != ordered:
            raise ValueError("scenario changes must be in canonical order")
        expected = change_scenario_hash(
            scenario_id=self.scenario_id,
            project_id=self.project_id,
            asset_input=self.asset_input,
            intent_kind=self.intent_kind,
            baseline_snapshot_id=self.baseline_snapshot_id,
            baseline_snapshot_hash=self.baseline_snapshot_hash,
            proposed_hardware_revision_id=self.proposed_hardware_revision_id,
            created_at=self.created_at,
            changes=self.changes,
        )
        if self.scenario_hash != expected:
            raise ValueError("change scenario hash does not match its payload")
        return self


class PreviewFinding(ContractModel):
    finding_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    severity: PreviewFindingSeverity
    summary: str = Field(min_length=1)
    affected_domains: tuple[ArtifactDomain, ...] = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("affected_domains")
    @classmethod
    def affected_domains_must_be_unique_and_canonical(
        cls, value: tuple[ArtifactDomain, ...]
    ) -> tuple[ArtifactDomain, ...]:
        return _unique_sorted_domains(value, "preview finding affected domains")

    @field_validator("evidence_refs")
    @classmethod
    def evidence_refs_must_be_unique_and_canonical(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        return _unique_sorted_strings(value, "preview finding evidence refs")


class ChangeImpactPreview(ContractModel):
    """Pre-release impact forecast; this is not release evidence or a release gate."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    preview_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    scenario_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    planner_version: str = Field(min_length=1)
    generated_at: datetime
    recommendation: PreviewRecommendation
    predicted_affected_domains: tuple[ArtifactDomain, ...] = Field(min_length=1)
    predicted_required_actions: tuple[str, ...] = Field(min_length=1)
    predicted_findings: tuple[PreviewFinding, ...] = Field(default_factory=tuple)
    predicted_retests: tuple[RetestRequirement, ...] = Field(default_factory=tuple)
    assumptions: tuple[str, ...] = Field(default_factory=tuple)
    missing_information: tuple[str, ...] = Field(default_factory=tuple)
    preview_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("generated_at")
    @classmethod
    def generated_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "generated_at")

    @field_validator("predicted_affected_domains")
    @classmethod
    def affected_domains_must_be_unique_and_canonical(
        cls, value: tuple[ArtifactDomain, ...]
    ) -> tuple[ArtifactDomain, ...]:
        return _unique_sorted_domains(value, "preview affected domains")

    @field_validator("predicted_required_actions", "assumptions", "missing_information")
    @classmethod
    def preview_strings_must_be_unique_and_canonical(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        return _unique_sorted_strings(value, "preview strings")

    @model_validator(mode="after")
    def preview_shape_and_hash_must_be_canonical(self) -> ChangeImpactPreview:
        finding_ids = [item.finding_id for item in self.predicted_findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("preview finding IDs must be unique")
        if self.predicted_findings != tuple(
            sorted(self.predicted_findings, key=_preview_finding_key)
        ):
            raise ValueError("preview findings must be in canonical order")
        retest_ids = [item.retest_id for item in self.predicted_retests]
        if len(retest_ids) != len(set(retest_ids)):
            raise ValueError("preview retest IDs must be unique")
        if self.predicted_retests != tuple(
            sorted(self.predicted_retests, key=_retest_key)
        ):
            raise ValueError("preview retests must be in canonical order")
        for retest in self.predicted_retests:
            if retest.status is not RetestStatus.REQUIRED:
                raise ValueError("preview retests can only be required, not satisfied")
            if retest.satisfied_by_evidence_id is not None:
                raise ValueError("preview cannot bind release evidence")
        if (
            self.missing_information
            and self.recommendation is not PreviewRecommendation.INDETERMINATE
        ):
            raise ValueError("missing information requires indeterminate")
        expected = change_impact_preview_hash(
            preview_id=self.preview_id,
            project_id=self.project_id,
            scenario_id=self.scenario_id,
            scenario_hash=self.scenario_hash,
            planner_version=self.planner_version,
            generated_at=self.generated_at,
            recommendation=self.recommendation,
            predicted_affected_domains=self.predicted_affected_domains,
            predicted_required_actions=self.predicted_required_actions,
            predicted_findings=self.predicted_findings,
            predicted_retests=self.predicted_retests,
            assumptions=self.assumptions,
            missing_information=self.missing_information,
        )
        if self.preview_hash != expected:
            raise ValueError("change impact preview hash does not match its payload")
        return self


class PlanDeviation(ContractModel):
    deviation_id: str = Field(min_length=1)
    kind: PlanDeviationKind
    summary: str = Field(min_length=1)
    affected_domains: tuple[ArtifactDomain, ...] = Field(min_length=1)
    planned_ref: str | None = Field(default=None, min_length=1)
    actual_ref: str | None = Field(default=None, min_length=1)

    @field_validator("affected_domains")
    @classmethod
    def affected_domains_must_be_unique_and_canonical(
        cls, value: tuple[ArtifactDomain, ...]
    ) -> tuple[ArtifactDomain, ...]:
        return _unique_sorted_domains(value, "plan deviation affected domains")

    @model_validator(mode="after")
    def deviation_must_bind_a_side(self) -> PlanDeviation:
        if self.planned_ref is None and self.actual_ref is None:
            raise ValueError("plan deviation must reference planned or actual work")
        return self


class PlanVerification(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    verification_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    preview_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    actual_change_analysis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    verified_at: datetime
    matches_plan: bool
    observed_planned_refs: tuple[str, ...] = Field(default_factory=tuple)
    deviations: tuple[PlanDeviation, ...] = Field(default_factory=tuple)
    verification_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("verified_at")
    @classmethod
    def verified_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "verified_at")

    @field_validator("observed_planned_refs")
    @classmethod
    def observed_planned_refs_must_be_unique_and_canonical(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        return _unique_sorted_strings(value, "observed planned refs")

    @model_validator(mode="after")
    def verification_hash_must_bind_preview_and_actual(self) -> PlanVerification:
        deviation_ids = [item.deviation_id for item in self.deviations]
        if len(deviation_ids) != len(set(deviation_ids)):
            raise ValueError("plan deviation IDs must be unique")
        if self.deviations != tuple(sorted(self.deviations, key=_deviation_key)):
            raise ValueError("plan deviations must be in canonical order")
        if self.matches_plan != (not self.deviations):
            raise ValueError("matches_plan must exactly follow deviations")
        missing_refs = {
            item.planned_ref
            for item in self.deviations
            if item.kind is PlanDeviationKind.MISSING_PLANNED_CHANGE
            and item.planned_ref is not None
        }
        if missing_refs.intersection(self.observed_planned_refs):
            raise ValueError(
                "observed planned refs cannot include missing planned changes"
            )
        expected = plan_verification_hash(
            verification_id=self.verification_id,
            project_id=self.project_id,
            scenario_id=self.scenario_id,
            preview_hash=self.preview_hash,
            actual_change_analysis_hash=self.actual_change_analysis_hash,
            verified_at=self.verified_at,
            matches_plan=self.matches_plan,
            observed_planned_refs=self.observed_planned_refs,
            deviations=self.deviations,
        )
        if self.verification_hash != expected:
            raise ValueError("plan verification hash does not match its payload")
        return self


class _ComponentSpecificationPayload(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    component_id: str
    manufacturer: str
    part_number: str
    quantity: int
    source_ref: ExternalArtifactRef
    interface_contract: InterfaceContract | None
    quote: QuoteSnapshot | None
    attributes: Mapping[str, Quantity]

    @field_validator("attributes", mode="after")
    @classmethod
    def freeze_attributes(cls, value: Mapping[str, Quantity]) -> Mapping[str, Quantity]:
        return MappingProxyType(dict(sorted(value.items())))

    @field_serializer("attributes")
    def serialize_attributes(
        self, value: Mapping[str, Quantity]
    ) -> dict[str, Quantity]:
        return dict(value)


class _ChangeScenarioPayload(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    scenario_id: str
    project_id: str
    asset_input: AssetInput
    intent_kind: PlanIntentKind
    baseline_snapshot_id: str
    baseline_snapshot_hash: str
    proposed_hardware_revision_id: str
    created_at: datetime
    changes: tuple[PlannedComponentChange, ...]


class _ChangeImpactPreviewPayload(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    preview_id: str
    project_id: str
    scenario_id: str
    scenario_hash: str
    planner_version: str
    generated_at: datetime
    recommendation: PreviewRecommendation
    predicted_affected_domains: tuple[ArtifactDomain, ...]
    predicted_required_actions: tuple[str, ...]
    predicted_findings: tuple[PreviewFinding, ...]
    predicted_retests: tuple[RetestRequirement, ...]
    assumptions: tuple[str, ...]
    missing_information: tuple[str, ...]


class _PlanVerificationPayload(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    verification_id: str
    project_id: str
    scenario_id: str
    preview_hash: str
    actual_change_analysis_hash: str
    verified_at: datetime
    matches_plan: bool
    observed_planned_refs: tuple[str, ...]
    deviations: tuple[PlanDeviation, ...]


def component_specification_hash(
    *,
    component_id: str,
    manufacturer: str,
    part_number: str,
    quantity: int,
    source_ref: ExternalArtifactRef,
    interface_contract: InterfaceContract | None = None,
    quote: QuoteSnapshot | None = None,
    attributes: Mapping[str, Quantity] | None = None,
) -> str:
    return canonical_sha256(
        _ComponentSpecificationPayload(
            component_id=component_id,
            manufacturer=manufacturer,
            part_number=part_number,
            quantity=quantity,
            source_ref=source_ref,
            interface_contract=interface_contract,
            quote=quote,
            attributes=attributes or {},
        )
    )


def change_scenario_hash(
    *,
    scenario_id: str,
    project_id: str,
    asset_input: AssetInput,
    intent_kind: PlanIntentKind = PlanIntentKind.CHANGE_IMPACT,
    baseline_snapshot_id: str,
    baseline_snapshot_hash: str,
    proposed_hardware_revision_id: str,
    created_at: datetime,
    changes: tuple[PlannedComponentChange, ...],
) -> str:
    return canonical_sha256(
        _ChangeScenarioPayload(
            scenario_id=scenario_id,
            project_id=project_id,
            asset_input=asset_input,
            intent_kind=intent_kind,
            baseline_snapshot_id=baseline_snapshot_id,
            baseline_snapshot_hash=baseline_snapshot_hash,
            proposed_hardware_revision_id=proposed_hardware_revision_id,
            created_at=created_at,
            changes=changes,
        )
    )


def change_impact_preview_hash(
    *,
    preview_id: str,
    project_id: str,
    scenario_id: str,
    scenario_hash: str,
    planner_version: str,
    generated_at: datetime,
    recommendation: PreviewRecommendation,
    predicted_affected_domains: tuple[ArtifactDomain, ...],
    predicted_required_actions: tuple[str, ...],
    predicted_findings: tuple[PreviewFinding, ...] = (),
    predicted_retests: tuple[RetestRequirement, ...] = (),
    assumptions: tuple[str, ...] = (),
    missing_information: tuple[str, ...] = (),
) -> str:
    return canonical_sha256(
        _ChangeImpactPreviewPayload(
            preview_id=preview_id,
            project_id=project_id,
            scenario_id=scenario_id,
            scenario_hash=scenario_hash,
            planner_version=planner_version,
            generated_at=generated_at,
            recommendation=recommendation,
            predicted_affected_domains=predicted_affected_domains,
            predicted_required_actions=predicted_required_actions,
            predicted_findings=predicted_findings,
            predicted_retests=predicted_retests,
            assumptions=assumptions,
            missing_information=missing_information,
        )
    )


def plan_verification_hash(
    *,
    verification_id: str,
    project_id: str,
    scenario_id: str,
    preview_hash: str,
    actual_change_analysis_hash: str,
    verified_at: datetime,
    matches_plan: bool,
    observed_planned_refs: tuple[str, ...] = (),
    deviations: tuple[PlanDeviation, ...] = (),
) -> str:
    return canonical_sha256(
        _PlanVerificationPayload(
            verification_id=verification_id,
            project_id=project_id,
            scenario_id=scenario_id,
            preview_hash=preview_hash,
            actual_change_analysis_hash=actual_change_analysis_hash,
            verified_at=verified_at,
            matches_plan=matches_plan,
            observed_planned_refs=observed_planned_refs,
            deviations=deviations,
        )
    )


__all__ = [
    "AssetInput",
    "AssetInputKind",
    "ChangeImpactPreview",
    "ChangeScenario",
    "ComponentSpecification",
    "PlanDeviation",
    "PlanDeviationKind",
    "PlanIntentKind",
    "PlanVerification",
    "PlannedChangeAction",
    "PlannedComponentChange",
    "PreviewFinding",
    "PreviewFindingSeverity",
    "PreviewRecommendation",
    "change_impact_preview_hash",
    "change_scenario_hash",
    "component_specification_hash",
    "plan_verification_hash",
]
