from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from pydantic import Field, field_serializer, field_validator, model_validator

from forge_core.change_management import (
    ArtifactDomain,
    ChangeImpact,
    ConnectorSnapshot,
    ConsistencyFinding,
    EvidenceTier,
    ExternalArtifactRef,
    FindingSeverity,
    RetestRequirement,
    RetestStatus,
)
from forge_core.constraints import InterfaceContract, InterfaceSignal
from forge_core.hashing import canonical_sha256
from forge_core.models import ContractModel, Quantity


class ArtifactChangeType(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"


class ChangeFacet(StrEnum):
    REVISION_IDENTIFIER = "revision_identifier"
    SOURCE_REVISION = "source_revision"
    ARTIFACT_ADDED = "artifact_added"
    ARTIFACT_REMOVED = "artifact_removed"
    OPAQUE_CONTENT = "opaque_content"
    PIN_ASSIGNMENT = "pin_assignment"
    VOLTAGE_RANGE = "voltage_range"
    UNIT = "unit"
    COMMAND_RANGE = "command_range"
    SAFE_VALUE = "safe_value"
    PROTOCOL_SCHEMA = "protocol_schema"


_MINIMUM_HARDWARE_RETESTS_BY_FACET: Mapping[
    ChangeFacet, frozenset[tuple[str, EvidenceTier]]
] = MappingProxyType(
    {
        ChangeFacet.REVISION_IDENTIFIER: frozenset(
            {
                ("firmware-build", EvidenceTier.STATIC),
                ("interface-contract-validation", EvidenceTier.STATIC),
                ("bom-provenance-validation", EvidenceTier.STATIC),
                ("bench-electrical", EvidenceTier.BENCH),
                ("protocol-conformance", EvidenceTier.BENCH),
                ("hil-regression", EvidenceTier.HIL),
                ("physical-device-smoke", EvidenceTier.PHYSICAL_DEVICE),
                ("documentation-review", EvidenceTier.STATIC),
            }
        ),
        ChangeFacet.SOURCE_REVISION: frozenset(
            {
                ("firmware-build", EvidenceTier.STATIC),
                ("interface-contract-validation", EvidenceTier.STATIC),
                ("bom-provenance-validation", EvidenceTier.STATIC),
                ("bench-electrical", EvidenceTier.BENCH),
                ("protocol-conformance", EvidenceTier.BENCH),
                ("hil-regression", EvidenceTier.HIL),
                ("physical-device-smoke", EvidenceTier.PHYSICAL_DEVICE),
                ("documentation-review", EvidenceTier.STATIC),
            }
        ),
        ChangeFacet.ARTIFACT_ADDED: frozenset(
            {
                ("firmware-build", EvidenceTier.STATIC),
                ("interface-contract-validation", EvidenceTier.STATIC),
                ("bom-provenance-validation", EvidenceTier.STATIC),
                ("bench-electrical", EvidenceTier.BENCH),
                ("protocol-conformance", EvidenceTier.BENCH),
                ("hil-regression", EvidenceTier.HIL),
                ("physical-device-smoke", EvidenceTier.PHYSICAL_DEVICE),
                ("documentation-review", EvidenceTier.STATIC),
            }
        ),
        ChangeFacet.ARTIFACT_REMOVED: frozenset(
            {
                ("firmware-build", EvidenceTier.STATIC),
                ("interface-contract-validation", EvidenceTier.STATIC),
                ("bom-provenance-validation", EvidenceTier.STATIC),
                ("bench-electrical", EvidenceTier.BENCH),
                ("protocol-conformance", EvidenceTier.BENCH),
                ("hil-regression", EvidenceTier.HIL),
                ("physical-device-smoke", EvidenceTier.PHYSICAL_DEVICE),
                ("documentation-review", EvidenceTier.STATIC),
            }
        ),
        ChangeFacet.OPAQUE_CONTENT: frozenset(
            {
                ("firmware-build", EvidenceTier.STATIC),
                ("interface-contract-validation", EvidenceTier.STATIC),
                ("bom-provenance-validation", EvidenceTier.STATIC),
                ("bench-electrical", EvidenceTier.BENCH),
                ("protocol-conformance", EvidenceTier.BENCH),
                ("hil-regression", EvidenceTier.HIL),
                ("physical-device-smoke", EvidenceTier.PHYSICAL_DEVICE),
                ("documentation-review", EvidenceTier.STATIC),
            }
        ),
        ChangeFacet.PIN_ASSIGNMENT: frozenset(
            {
                ("firmware-build", EvidenceTier.STATIC),
                ("interface-contract-validation", EvidenceTier.STATIC),
                ("bench-electrical", EvidenceTier.BENCH),
                ("hil-regression", EvidenceTier.HIL),
                ("physical-device-smoke", EvidenceTier.PHYSICAL_DEVICE),
                ("documentation-review", EvidenceTier.STATIC),
            }
        ),
        ChangeFacet.VOLTAGE_RANGE: frozenset(
            {
                ("interface-contract-validation", EvidenceTier.STATIC),
                ("bom-provenance-validation", EvidenceTier.STATIC),
                ("bench-electrical", EvidenceTier.BENCH),
                ("hil-regression", EvidenceTier.HIL),
                ("physical-device-smoke", EvidenceTier.PHYSICAL_DEVICE),
                ("documentation-review", EvidenceTier.STATIC),
            }
        ),
        ChangeFacet.UNIT: frozenset(
            {
                ("firmware-build", EvidenceTier.STATIC),
                ("interface-contract-validation", EvidenceTier.STATIC),
                ("bench-electrical", EvidenceTier.BENCH),
                ("protocol-conformance", EvidenceTier.BENCH),
                ("hil-regression", EvidenceTier.HIL),
                ("documentation-review", EvidenceTier.STATIC),
            }
        ),
        ChangeFacet.COMMAND_RANGE: frozenset(
            {
                ("firmware-build", EvidenceTier.STATIC),
                ("interface-contract-validation", EvidenceTier.STATIC),
                ("protocol-conformance", EvidenceTier.BENCH),
                ("hil-regression", EvidenceTier.HIL),
                ("documentation-review", EvidenceTier.STATIC),
            }
        ),
        ChangeFacet.SAFE_VALUE: frozenset(
            {
                ("firmware-build", EvidenceTier.STATIC),
                ("interface-contract-validation", EvidenceTier.STATIC),
                ("protocol-conformance", EvidenceTier.BENCH),
                ("hil-regression", EvidenceTier.HIL),
                ("documentation-review", EvidenceTier.STATIC),
            }
        ),
        ChangeFacet.PROTOCOL_SCHEMA: frozenset(
            {
                ("firmware-build", EvidenceTier.STATIC),
                ("interface-contract-validation", EvidenceTier.STATIC),
                ("protocol-conformance", EvidenceTier.BENCH),
                ("hil-regression", EvidenceTier.HIL),
                ("documentation-review", EvidenceTier.STATIC),
            }
        ),
    }
)


class ArtifactChange(ContractModel):
    change_id: str = Field(min_length=1)
    change_type: ArtifactChangeType
    domain: ArtifactDomain
    before: ExternalArtifactRef | None = None
    after: ExternalArtifactRef | None = None
    facets: tuple[ChangeFacet, ...] = Field(min_length=1)
    changed_paths: tuple[str, ...] = Field(min_length=1)

    @field_validator("facets")
    @classmethod
    def facets_must_be_unique(
        cls, value: tuple[ChangeFacet, ...]
    ) -> tuple[ChangeFacet, ...]:
        if len(value) != len(set(value)):
            raise ValueError("artifact change facets must be unique")
        return value

    @field_validator("changed_paths")
    @classmethod
    def changed_paths_must_be_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("artifact changed paths must be unique")
        return value

    @model_validator(mode="after")
    def endpoints_must_match_change_type(self) -> ArtifactChange:
        if self.change_type is ArtifactChangeType.ADDED:
            if self.before is not None or self.after is None:
                raise ValueError("added artifact requires only an after endpoint")
        elif self.change_type is ArtifactChangeType.REMOVED:
            if self.before is None or self.after is not None:
                raise ValueError("removed artifact requires only a before endpoint")
        elif self.before is None or self.after is None:
            raise ValueError("modified artifact requires before and after endpoints")
        endpoints = tuple(
            item for item in (self.before, self.after) if item is not None
        )
        if any(item.domain is not self.domain for item in endpoints):
            raise ValueError("artifact change domain must match its endpoints")
        if self.before is not None and self.after is not None:
            before_key = (self.before.source_system, self.before.artifact_id)
            after_key = (self.after.source_system, self.after.artifact_id)
            if before_key != after_key:
                raise ValueError("modified artifact endpoints must have one identity")
        return self


class _InterfaceProjectionPayload(ContractModel):
    snapshot_id: str
    source_ref: ExternalArtifactRef
    normalizer_version: str
    contract: InterfaceContract


class _ConnectorSnapshotFingerprint(ContractModel):
    schema_version: str
    snapshot_id: str
    project_id: str
    hardware_revision_id: str
    read_only: bool
    captured_at: datetime
    artifacts: tuple[ExternalArtifactRef, ...]


def connector_snapshot_hash(snapshot: ConnectorSnapshot) -> str:
    return canonical_sha256(
        _ConnectorSnapshotFingerprint(
            schema_version=snapshot.schema_version,
            snapshot_id=snapshot.snapshot_id,
            project_id=snapshot.project_id,
            hardware_revision_id=snapshot.hardware_revision_id,
            read_only=snapshot.read_only,
            captured_at=snapshot.captured_at,
            artifacts=tuple(sorted(snapshot.artifacts, key=_artifact_key)),
        )
    )


def interface_projection_hash(
    snapshot_id: str,
    source_ref: ExternalArtifactRef,
    normalizer_version: str,
    contract: InterfaceContract,
) -> str:
    return canonical_sha256(
        _InterfaceProjectionPayload(
            snapshot_id=snapshot_id,
            source_ref=source_ref,
            normalizer_version=normalizer_version,
            contract=contract,
        )
    )


class NormalizedInterfaceProjection(ContractModel):
    snapshot_id: str = Field(min_length=1)
    source_ref: ExternalArtifactRef
    normalizer_version: str = Field(min_length=1)
    contract: InterfaceContract
    projection_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def projection_must_be_source_bound(self) -> NormalizedInterfaceProjection:
        supported = {
            ArtifactDomain.HARDWARE,
            ArtifactDomain.FIRMWARE,
            ArtifactDomain.PROTOCOL,
        }
        if self.source_ref.domain not in supported:
            raise ValueError(
                "interface projection requires an interface-bearing domain"
            )
        expected = interface_projection_hash(
            self.snapshot_id,
            self.source_ref,
            self.normalizer_version,
            self.contract,
        )
        if self.projection_hash != expected:
            raise ValueError(
                "interface projection hash does not match its source payload"
            )
        return self


class RetestRule(ContractModel):
    rule_id: str = Field(min_length=1)
    test_id: str = Field(min_length=1)
    required_tier: EvidenceTier
    trigger_domains: tuple[ArtifactDomain, ...] = ()
    trigger_facets: tuple[ChangeFacet, ...] = ()
    trigger_finding_rules: tuple[str, ...] = ()
    reason_code: str = Field(min_length=1)

    @model_validator(mode="after")
    def trigger_must_be_explicit_and_unique(self) -> RetestRule:
        if (
            not self.trigger_domains
            and not self.trigger_facets
            and not self.trigger_finding_rules
        ):
            raise ValueError("retest rule requires a domain, facet, or finding trigger")
        if len(self.trigger_domains) != len(set(self.trigger_domains)):
            raise ValueError("retest trigger domains must be unique")
        if len(self.trigger_finding_rules) != len(set(self.trigger_finding_rules)):
            raise ValueError("retest finding triggers must be unique")
        if len(self.trigger_facets) != len(set(self.trigger_facets)):
            raise ValueError("retest facet triggers must be unique")
        return self


class ChangeImpactPolicy(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    policy_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    domain_impacts: Mapping[str, tuple[ArtifactDomain, ...]]
    retest_rules: tuple[RetestRule, ...]

    @field_validator("domain_impacts", mode="after")
    @classmethod
    def freeze_and_validate_domain_impacts(
        cls, value: Mapping[str, tuple[ArtifactDomain, ...]]
    ) -> Mapping[str, tuple[ArtifactDomain, ...]]:
        expected = {domain.value for domain in ArtifactDomain}
        if set(value) != expected:
            raise ValueError("impact policy must define every artifact domain")
        copied = dict(value)
        for name, domains in copied.items():
            source = ArtifactDomain(name)
            if not domains or source not in domains:
                raise ValueError("each changed domain must affect itself")
            if len(domains) != len(set(domains)):
                raise ValueError("affected domains must be unique")
        return MappingProxyType(copied)

    @field_validator("retest_rules")
    @classmethod
    def retest_rule_ids_must_be_unique(
        cls, value: tuple[RetestRule, ...]
    ) -> tuple[RetestRule, ...]:
        rule_ids = [item.rule_id for item in value]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("retest rule IDs must be unique")
        return value

    @model_validator(mode="after")
    def policy_must_fail_closed_for_every_change(self) -> ChangeImpactPolicy:
        if set(self.domain_impacts[ArtifactDomain.HARDWARE.value]) != set(
            ArtifactDomain
        ):
            raise ValueError("hardware changes must conservatively affect every domain")
        covered_domains = {
            domain for rule in self.retest_rules for domain in rule.trigger_domains
        }
        if covered_domains != set(ArtifactDomain):
            raise ValueError("retest policy must cover every artifact domain")
        covered_facets = {
            facet for rule in self.retest_rules for facet in rule.trigger_facets
        }
        if covered_facets != set(ChangeFacet):
            raise ValueError("retest policy must cover every change facet")
        for facet, required_retests in _MINIMUM_HARDWARE_RETESTS_BY_FACET.items():
            configured_retests = {
                (rule.test_id, rule.required_tier)
                for rule in self.retest_rules
                if facet in rule.trigger_facets
            }
            missing = required_retests.difference(configured_retests)
            if missing:
                formatted = ", ".join(
                    f"{test_id}@{tier.value}"
                    for test_id, tier in sorted(
                        missing, key=lambda item: (item[0], item[1].value)
                    )
                )
                raise ValueError(
                    f"hardware facet {facet.value} is missing minimum retests: "
                    f"{formatted}"
                )
        return self

    @field_serializer("domain_impacts")
    def serialize_domain_impacts(
        self, value: Mapping[str, tuple[ArtifactDomain, ...]]
    ) -> dict[str, tuple[ArtifactDomain, ...]]:
        return dict(value)


class ChangeImpactAssessment(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    policy_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    policy_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    project_id: str = Field(min_length=1)
    artifact_changes: tuple[ArtifactChange, ...]
    from_snapshot_id: str = Field(min_length=1)
    to_snapshot_id: str = Field(min_length=1)
    from_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    to_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    from_hardware_revision_id: str = Field(min_length=1)
    to_hardware_revision_id: str = Field(min_length=1)
    target_protocol_schema_hash: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    interface_projection_hashes: tuple[str, ...]
    impact: ChangeImpact | None
    findings: tuple[ConsistencyFinding, ...]
    required_retests: tuple[RetestRequirement, ...]
    analysis_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def analysis_hash_must_bind_the_complete_result(self) -> ChangeImpactAssessment:
        if (not self.artifact_changes) != (self.impact is None):
            raise ValueError("impact must exist exactly when artifact changes exist")
        if self.impact is None and (self.findings or self.required_retests):
            raise ValueError("no-change analysis cannot contain findings or retests")
        change_ids = [item.change_id for item in self.artifact_changes]
        finding_ids = [item.finding_id for item in self.findings]
        retest_ids = [item.retest_id for item in self.required_retests]
        if len(change_ids) != len(set(change_ids)):
            raise ValueError("artifact change IDs must be unique")
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("change-analysis finding IDs must be unique")
        if len(retest_ids) != len(set(retest_ids)):
            raise ValueError("change-analysis retest IDs must be unique")
        if self.impact is not None and set(self.impact.changed_domains) != {
            item.domain for item in self.artifact_changes
        }:
            raise ValueError("impact changed domains must match artifact changes")
        payload = _ChangeImpactAssessmentPayload(
            policy_id=self.policy_id,
            policy_version=self.policy_version,
            policy_hash=self.policy_hash,
            project_id=self.project_id,
            from_snapshot_id=self.from_snapshot_id,
            to_snapshot_id=self.to_snapshot_id,
            from_snapshot_hash=self.from_snapshot_hash,
            to_snapshot_hash=self.to_snapshot_hash,
            from_hardware_revision_id=self.from_hardware_revision_id,
            to_hardware_revision_id=self.to_hardware_revision_id,
            target_protocol_schema_hash=self.target_protocol_schema_hash,
            interface_projection_hashes=self.interface_projection_hashes,
            artifact_changes=self.artifact_changes,
            impact=self.impact,
            findings=self.findings,
            required_retests=self.required_retests,
        )
        if self.analysis_hash != canonical_sha256(payload):
            raise ValueError("change-impact analysis hash does not match its result")
        return self


class _ChangeImpactAssessmentPayload(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    policy_id: str
    policy_version: str
    policy_hash: str
    project_id: str
    from_snapshot_id: str
    to_snapshot_id: str
    from_snapshot_hash: str
    to_snapshot_hash: str
    from_hardware_revision_id: str
    to_hardware_revision_id: str
    target_protocol_schema_hash: str | None
    interface_projection_hashes: tuple[str, ...]
    artifact_changes: tuple[ArtifactChange, ...]
    impact: ChangeImpact | None
    findings: tuple[ConsistencyFinding, ...]
    required_retests: tuple[RetestRequirement, ...]


DEFAULT_CHANGE_IMPACT_POLICY = ChangeImpactPolicy(
    policy_id="forge-default-change-impact",
    policy_version="1.0.0",
    domain_impacts={
        "hardware": tuple(ArtifactDomain),
        "firmware": (
            ArtifactDomain.FIRMWARE,
            ArtifactDomain.TEST,
            ArtifactDomain.PROTOCOL,
            ArtifactDomain.DOCUMENTATION,
        ),
        "bom": (
            ArtifactDomain.BOM,
            ArtifactDomain.TEST,
            ArtifactDomain.DOCUMENTATION,
        ),
        "test": (ArtifactDomain.TEST, ArtifactDomain.DOCUMENTATION),
        "protocol": (
            ArtifactDomain.FIRMWARE,
            ArtifactDomain.TEST,
            ArtifactDomain.PROTOCOL,
            ArtifactDomain.DOCUMENTATION,
        ),
        "documentation": (ArtifactDomain.DOCUMENTATION,),
    },
    retest_rules=(
        RetestRule(
            rule_id="retest-firmware-build",
            test_id="firmware-build",
            required_tier=EvidenceTier.STATIC,
            trigger_domains=(ArtifactDomain.FIRMWARE, ArtifactDomain.PROTOCOL),
            trigger_facets=(
                ChangeFacet.REVISION_IDENTIFIER,
                ChangeFacet.SOURCE_REVISION,
                ChangeFacet.ARTIFACT_ADDED,
                ChangeFacet.ARTIFACT_REMOVED,
                ChangeFacet.OPAQUE_CONTENT,
                ChangeFacet.PIN_ASSIGNMENT,
                ChangeFacet.UNIT,
                ChangeFacet.COMMAND_RANGE,
                ChangeFacet.SAFE_VALUE,
                ChangeFacet.PROTOCOL_SCHEMA,
            ),
            reason_code="changed_contract_requires_firmware_build",
        ),
        RetestRule(
            rule_id="retest-interface-contract",
            test_id="interface-contract-validation",
            required_tier=EvidenceTier.STATIC,
            trigger_domains=(ArtifactDomain.FIRMWARE, ArtifactDomain.PROTOCOL),
            trigger_facets=(
                ChangeFacet.REVISION_IDENTIFIER,
                ChangeFacet.SOURCE_REVISION,
                ChangeFacet.ARTIFACT_ADDED,
                ChangeFacet.ARTIFACT_REMOVED,
                ChangeFacet.OPAQUE_CONTENT,
                ChangeFacet.PIN_ASSIGNMENT,
                ChangeFacet.VOLTAGE_RANGE,
                ChangeFacet.UNIT,
                ChangeFacet.COMMAND_RANGE,
                ChangeFacet.SAFE_VALUE,
                ChangeFacet.PROTOCOL_SCHEMA,
            ),
            reason_code="changed_interface_requires_static_validation",
        ),
        RetestRule(
            rule_id="retest-bom-provenance",
            test_id="bom-provenance-validation",
            required_tier=EvidenceTier.STATIC,
            trigger_domains=(ArtifactDomain.BOM,),
            trigger_facets=(
                ChangeFacet.REVISION_IDENTIFIER,
                ChangeFacet.SOURCE_REVISION,
                ChangeFacet.ARTIFACT_ADDED,
                ChangeFacet.ARTIFACT_REMOVED,
                ChangeFacet.OPAQUE_CONTENT,
                ChangeFacet.VOLTAGE_RANGE,
            ),
            reason_code="changed_hardware_or_bom_requires_quote_validation",
        ),
        RetestRule(
            rule_id="retest-bench-electrical",
            test_id="bench-electrical",
            required_tier=EvidenceTier.BENCH,
            trigger_domains=(ArtifactDomain.BOM,),
            trigger_facets=(
                ChangeFacet.REVISION_IDENTIFIER,
                ChangeFacet.SOURCE_REVISION,
                ChangeFacet.ARTIFACT_ADDED,
                ChangeFacet.ARTIFACT_REMOVED,
                ChangeFacet.OPAQUE_CONTENT,
                ChangeFacet.PIN_ASSIGNMENT,
                ChangeFacet.VOLTAGE_RANGE,
                ChangeFacet.UNIT,
            ),
            trigger_finding_rules=(
                "pin_mismatch",
                "voltage_unit_mismatch",
                "voltage_range_mismatch",
            ),
            reason_code="electrical_change_requires_bench_retest",
        ),
        RetestRule(
            rule_id="retest-protocol-conformance",
            test_id="protocol-conformance",
            required_tier=EvidenceTier.BENCH,
            trigger_domains=(ArtifactDomain.FIRMWARE, ArtifactDomain.PROTOCOL),
            trigger_facets=(
                ChangeFacet.REVISION_IDENTIFIER,
                ChangeFacet.SOURCE_REVISION,
                ChangeFacet.ARTIFACT_ADDED,
                ChangeFacet.ARTIFACT_REMOVED,
                ChangeFacet.OPAQUE_CONTENT,
                ChangeFacet.UNIT,
                ChangeFacet.COMMAND_RANGE,
                ChangeFacet.SAFE_VALUE,
                ChangeFacet.PROTOCOL_SCHEMA,
            ),
            trigger_finding_rules=(
                "protocol_schema_mismatch",
                "command_unit_mismatch",
                "command_range_mismatch",
            ),
            reason_code="protocol_change_requires_conformance_retest",
        ),
        RetestRule(
            rule_id="retest-hil-regression",
            test_id="hil-regression",
            required_tier=EvidenceTier.HIL,
            trigger_domains=(
                ArtifactDomain.FIRMWARE,
                ArtifactDomain.BOM,
                ArtifactDomain.PROTOCOL,
            ),
            trigger_facets=(
                ChangeFacet.REVISION_IDENTIFIER,
                ChangeFacet.SOURCE_REVISION,
                ChangeFacet.ARTIFACT_ADDED,
                ChangeFacet.ARTIFACT_REMOVED,
                ChangeFacet.OPAQUE_CONTENT,
                ChangeFacet.PIN_ASSIGNMENT,
                ChangeFacet.VOLTAGE_RANGE,
                ChangeFacet.UNIT,
                ChangeFacet.COMMAND_RANGE,
                ChangeFacet.SAFE_VALUE,
                ChangeFacet.PROTOCOL_SCHEMA,
            ),
            reason_code="runtime_change_requires_hil_regression",
        ),
        RetestRule(
            rule_id="retest-physical-smoke",
            test_id="physical-device-smoke",
            required_tier=EvidenceTier.PHYSICAL_DEVICE,
            trigger_domains=(ArtifactDomain.BOM,),
            trigger_facets=(
                ChangeFacet.REVISION_IDENTIFIER,
                ChangeFacet.SOURCE_REVISION,
                ChangeFacet.ARTIFACT_ADDED,
                ChangeFacet.ARTIFACT_REMOVED,
                ChangeFacet.OPAQUE_CONTENT,
                ChangeFacet.PIN_ASSIGNMENT,
                ChangeFacet.VOLTAGE_RANGE,
            ),
            reason_code="physical_change_requires_device_smoke_test",
        ),
        RetestRule(
            rule_id="retest-documentation",
            test_id="documentation-review",
            required_tier=EvidenceTier.STATIC,
            trigger_domains=tuple(ArtifactDomain),
            trigger_facets=tuple(ChangeFacet),
            reason_code="changed_artifact_requires_documentation_review",
        ),
    ),
)


def _artifact_key(item: ExternalArtifactRef) -> tuple[str, str, str]:
    return item.domain.value, item.source_system.value, item.artifact_id


def detect_artifact_changes(
    previous: ConnectorSnapshot, current: ConnectorSnapshot
) -> tuple[ArtifactChange, ...]:
    if previous.project_id != current.project_id:
        raise ValueError("cannot compare snapshots from different projects")
    if previous.snapshot_id == current.snapshot_id:
        raise ValueError("snapshot IDs must be different")
    if current.captured_at < previous.captured_at:
        raise ValueError("current snapshot cannot predate previous snapshot")

    before = {_artifact_key(item): item for item in previous.artifacts}
    after = {_artifact_key(item): item for item in current.artifacts}
    changes: list[ArtifactChange] = []
    for domain_name, source, artifact_id in sorted(set(before) | set(after)):
        key = (domain_name, source, artifact_id)
        old = before.get(key)
        new = after.get(key)
        change_id = f"artifact:{domain_name}:{source}:{artifact_id}"
        if old is None:
            assert new is not None
            changes.append(
                ArtifactChange(
                    change_id=change_id,
                    change_type=ArtifactChangeType.ADDED,
                    domain=new.domain,
                    after=new,
                    facets=(ChangeFacet.ARTIFACT_ADDED,),
                    changed_paths=("*",),
                )
            )
        elif new is None:
            changes.append(
                ArtifactChange(
                    change_id=change_id,
                    change_type=ArtifactChangeType.REMOVED,
                    domain=old.domain,
                    before=old,
                    facets=(ChangeFacet.ARTIFACT_REMOVED,),
                    changed_paths=("*",),
                )
            )
        elif (
            old.source_revision != new.source_revision
            or old.content_hash != new.content_hash
        ):
            facets: list[ChangeFacet] = []
            paths: list[str] = []
            if old.source_revision != new.source_revision:
                facets.append(ChangeFacet.SOURCE_REVISION)
                paths.append("source_revision")
            if old.content_hash != new.content_hash:
                facets.append(ChangeFacet.OPAQUE_CONTENT)
                paths.append("content")
            changes.append(
                ArtifactChange(
                    change_id=change_id,
                    change_type=ArtifactChangeType.MODIFIED,
                    domain=new.domain,
                    before=old,
                    after=new,
                    facets=tuple(facets),
                    changed_paths=tuple(paths),
                )
            )
    if previous.hardware_revision_id != current.hardware_revision_id:
        hardware_indexes = [
            index
            for index, change in enumerate(changes)
            if change.domain is ArtifactDomain.HARDWARE
        ]
        if hardware_indexes:
            index = hardware_indexes[0]
            change = changes[index]
            changes[index] = ArtifactChange.model_validate(
                change.model_dump(mode="python")
                | {
                    "facets": tuple(
                        sorted(
                            set(change.facets) | {ChangeFacet.REVISION_IDENTIFIER},
                            key=str,
                        )
                    ),
                    "changed_paths": tuple(
                        sorted(set(change.changed_paths) | {"hardware_revision_id"})
                    ),
                }
            )
        else:
            current_hardware = sorted(
                (
                    item
                    for item in current.artifacts
                    if item.domain is ArtifactDomain.HARDWARE
                ),
                key=_artifact_key,
            )
            if not current_hardware:
                raise ValueError(
                    "hardware revision change requires a hardware artifact"
                )
            after_ref = current_hardware[0]
            before_ref = before.get(_artifact_key(after_ref), after_ref)
            changes.append(
                ArtifactChange(
                    change_id=(
                        f"revision:{previous.hardware_revision_id}:"
                        f"{current.hardware_revision_id}"
                    ),
                    change_type=ArtifactChangeType.MODIFIED,
                    domain=ArtifactDomain.HARDWARE,
                    before=before_ref,
                    after=after_ref,
                    facets=(ChangeFacet.REVISION_IDENTIFIER,),
                    changed_paths=("hardware_revision_id",),
                )
            )
    return tuple(sorted(changes, key=lambda item: item.change_id))


def _finding(
    rule_id: str,
    suffix: str,
    summary: str,
    domains: tuple[ArtifactDomain, ...],
    refs: tuple[str, ...],
) -> ConsistencyFinding:
    return ConsistencyFinding(
        finding_id=f"finding:{rule_id}:{suffix}",
        rule_id=rule_id,
        severity=FindingSeverity.BLOCKER,
        summary=summary,
        affected_domains=domains,
        evidence_refs=refs,
    )


def _quantity_metadata(value: Quantity) -> tuple[str, str]:
    return value.unit, value.dimension


def _quantity_value(value: Quantity) -> float:
    return value.value


def _optional_quantity_signature(
    value: Quantity | None,
) -> tuple[str, str, float] | None:
    if value is None:
        return None
    return value.unit, value.dimension, value.value


def _signal_findings(
    name: str,
    hardware: InterfaceSignal,
    candidate: InterfaceSignal,
    refs: tuple[str, str],
    candidate_domain: ArtifactDomain,
) -> tuple[ConsistencyFinding, ...]:
    findings: list[ConsistencyFinding] = []
    domains = (ArtifactDomain.HARDWARE, candidate_domain)
    if hardware.pin != candidate.pin:
        findings.append(
            _finding(
                "pin_mismatch",
                name,
                (
                    f"signal {name} uses hardware pin {hardware.pin} "
                    f"but {candidate_domain.value} pin {candidate.pin}"
                ),
                domains,
                refs,
            )
        )
    if hardware.direction != candidate.direction:
        findings.append(
            _finding(
                "signal_direction_mismatch",
                name,
                (
                    f"signal {name} direction differs between hardware "
                    f"and {candidate_domain.value} contracts"
                ),
                domains,
                refs,
            )
        )
    hardware_voltage = (
        _quantity_metadata(hardware.voltage_min),
        _quantity_metadata(hardware.voltage_max),
    )
    candidate_voltage = (
        _quantity_metadata(candidate.voltage_min),
        _quantity_metadata(candidate.voltage_max),
    )
    if hardware_voltage != candidate_voltage:
        findings.append(
            _finding(
                "voltage_unit_mismatch",
                name,
                f"signal {name} voltage unit or dimension differs",
                domains,
                refs,
            )
        )
    elif _quantity_value(hardware.voltage_min) != _quantity_value(
        candidate.voltage_min
    ) or _quantity_value(hardware.voltage_max) != _quantity_value(
        candidate.voltage_max
    ):
        findings.append(
            _finding(
                "voltage_range_mismatch",
                name,
                f"signal {name} voltage range differs",
                domains,
                refs,
            )
        )

    if (hardware.command_min is None) != (candidate.command_min is None):
        findings.append(
            _finding(
                "command_range_presence_mismatch",
                name,
                f"signal {name} command range exists on only one contract",
                domains,
                refs,
            )
        )
    elif hardware.command_min is not None and candidate.command_min is not None:
        assert hardware.command_max is not None
        assert candidate.command_max is not None
        if (
            _quantity_metadata(hardware.command_min),
            _quantity_metadata(hardware.command_max),
        ) != (
            _quantity_metadata(candidate.command_min),
            _quantity_metadata(candidate.command_max),
        ):
            findings.append(
                _finding(
                    "command_unit_mismatch",
                    name,
                    f"signal {name} command unit or dimension differs",
                    domains,
                    refs,
                )
            )
        elif _quantity_value(hardware.command_min) != _quantity_value(
            candidate.command_min
        ) or _quantity_value(hardware.command_max) != _quantity_value(
            candidate.command_max
        ):
            findings.append(
                _finding(
                    "command_range_mismatch",
                    name,
                    f"signal {name} command range differs",
                    domains,
                    refs,
                )
            )
    if _optional_quantity_signature(
        hardware.safe_value
    ) != _optional_quantity_signature(candidate.safe_value):
        findings.append(
            _finding(
                "safe_value_mismatch",
                name,
                f"signal {name} safe value differs",
                domains,
                refs,
            )
        )
    return tuple(findings)


def validate_interface_compatibility(
    hardware: InterfaceContract,
    candidate: InterfaceContract,
    *,
    evidence_refs: tuple[str, str] | None = None,
    candidate_domain: ArtifactDomain = ArtifactDomain.FIRMWARE,
) -> tuple[ConsistencyFinding, ...]:
    refs = evidence_refs or (
        f"interface:{hardware.contract_id}",
        f"interface:{candidate.contract_id}",
    )
    findings: list[ConsistencyFinding] = []
    if hardware.protocol_schema_hash != candidate.protocol_schema_hash:
        findings.append(
            _finding(
                "protocol_schema_mismatch",
                candidate.contract_id,
                f"hardware and {candidate_domain.value} protocol schema hashes differ",
                tuple(
                    sorted(
                        {
                            ArtifactDomain.HARDWARE,
                            candidate_domain,
                            ArtifactDomain.PROTOCOL,
                        },
                        key=str,
                    )
                ),
                refs,
            )
        )
    hardware_signals = {signal.name: signal for signal in hardware.signals}
    candidate_signals = {signal.name: signal for signal in candidate.signals}
    for name in sorted(set(hardware_signals) | set(candidate_signals)):
        hardware_signal = hardware_signals.get(name)
        candidate_signal = candidate_signals.get(name)
        if hardware_signal is None:
            findings.append(
                _finding(
                    "signal_missing_in_hardware",
                    f"{candidate.contract_id}:{name}",
                    (
                        f"signal {name} exists in {candidate_domain.value} "
                        "but not hardware contract"
                    ),
                    (ArtifactDomain.HARDWARE, candidate_domain),
                    refs,
                )
            )
        elif candidate_signal is None:
            findings.append(
                _finding(
                    f"signal_missing_in_{candidate_domain.value}",
                    f"{candidate.contract_id}:{name}",
                    (
                        f"signal {name} exists in hardware but not "
                        f"{candidate_domain.value} contract"
                    ),
                    (ArtifactDomain.HARDWARE, candidate_domain),
                    refs,
                )
            )
        else:
            signal_findings = _signal_findings(
                name,
                hardware_signal,
                candidate_signal,
                refs,
                candidate_domain,
            )
            findings.extend(
                item.model_copy(
                    update={
                        "finding_id": (
                            f"finding:{item.rule_id}:{candidate.contract_id}:{name}"
                        )
                    }
                )
                for item in signal_findings
            )
    return tuple(findings)


def _projection_ref(projection: NormalizedInterfaceProjection) -> str:
    source = projection.source_ref
    return (
        f"artifact:{source.domain.value}:{source.source_system.value}:"
        f"{source.artifact_id}:{source.source_revision}:{source.content_hash}"
    )


def _interface_change_details(
    previous: InterfaceContract,
    current: InterfaceContract,
) -> tuple[tuple[ChangeFacet, ...], tuple[str, ...]]:
    facets: set[ChangeFacet] = set()
    paths: set[str] = set()
    if previous.protocol_schema_hash != current.protocol_schema_hash:
        facets.add(ChangeFacet.PROTOCOL_SCHEMA)
        paths.add("protocol_schema_hash")
    before = {signal.name: signal for signal in previous.signals}
    after = {signal.name: signal for signal in current.signals}
    for name in sorted(set(before) | set(after)):
        old = before.get(name)
        new = after.get(name)
        prefix = f"signals.{name}"
        if old is None or new is None:
            facets.add(ChangeFacet.PIN_ASSIGNMENT)
            paths.add(prefix)
            continue
        if old.pin != new.pin or old.direction != new.direction:
            facets.add(ChangeFacet.PIN_ASSIGNMENT)
            paths.add(f"{prefix}.pin_or_direction")
        old_voltage_metadata = (
            _quantity_metadata(old.voltage_min),
            _quantity_metadata(old.voltage_max),
        )
        new_voltage_metadata = (
            _quantity_metadata(new.voltage_min),
            _quantity_metadata(new.voltage_max),
        )
        if old_voltage_metadata != new_voltage_metadata:
            facets.add(ChangeFacet.UNIT)
            paths.add(f"{prefix}.voltage_unit")
        elif (
            old.voltage_min.value != new.voltage_min.value
            or old.voltage_max.value != new.voltage_max.value
        ):
            facets.add(ChangeFacet.VOLTAGE_RANGE)
            paths.add(f"{prefix}.voltage_range")
        if (old.command_min is None) != (new.command_min is None):
            facets.add(ChangeFacet.COMMAND_RANGE)
            paths.add(f"{prefix}.command_range")
        elif old.command_min is not None and new.command_min is not None:
            assert old.command_max is not None
            assert new.command_max is not None
            old_metadata = (
                _quantity_metadata(old.command_min),
                _quantity_metadata(old.command_max),
            )
            new_metadata = (
                _quantity_metadata(new.command_min),
                _quantity_metadata(new.command_max),
            )
            if old_metadata != new_metadata:
                facets.add(ChangeFacet.UNIT)
                paths.add(f"{prefix}.command_unit")
            elif (
                old.command_min.value != new.command_min.value
                or old.command_max.value != new.command_max.value
            ):
                facets.add(ChangeFacet.COMMAND_RANGE)
                paths.add(f"{prefix}.command_range")
        if _optional_quantity_signature(old.safe_value) != _optional_quantity_signature(
            new.safe_value
        ):
            facets.add(ChangeFacet.SAFE_VALUE)
            paths.add(f"{prefix}.safe_value")
    return tuple(sorted(facets, key=str)), tuple(sorted(paths))


def _bind_interface_projections(
    previous: ConnectorSnapshot,
    current: ConnectorSnapshot,
    projections: tuple[NormalizedInterfaceProjection, ...],
) -> Mapping[tuple[str, ArtifactDomain], NormalizedInterfaceProjection]:
    snapshots = {
        previous.snapshot_id: previous,
        current.snapshot_id: current,
    }
    bound: dict[tuple[str, ArtifactDomain], NormalizedInterfaceProjection] = {}
    for projection in projections:
        snapshot = snapshots.get(projection.snapshot_id)
        if snapshot is None:
            raise ValueError("interface projection references an unrelated snapshot")
        if projection.source_ref not in snapshot.artifacts:
            raise ValueError("interface projection source is not in its snapshot")
        key = (projection.snapshot_id, projection.source_ref.domain)
        if key in bound:
            raise ValueError("only one interface projection is allowed per domain")
        bound[key] = projection
    return MappingProxyType(bound)


def _enrich_changes_with_projection_facets(
    changes: tuple[ArtifactChange, ...],
    previous: ConnectorSnapshot,
    current: ConnectorSnapshot,
    projections: Mapping[tuple[str, ArtifactDomain], NormalizedInterfaceProjection],
) -> tuple[ArtifactChange, ...]:
    by_identity: dict[tuple[str, str, str], tuple[ChangeFacet, ...]] = {}
    paths_by_identity: dict[tuple[str, str, str], tuple[str, ...]] = {}
    for domain in (
        ArtifactDomain.HARDWARE,
        ArtifactDomain.FIRMWARE,
        ArtifactDomain.PROTOCOL,
    ):
        before = projections.get((previous.snapshot_id, domain))
        after = projections.get((current.snapshot_id, domain))
        if before is None or after is None:
            continue
        facets, paths = _interface_change_details(before.contract, after.contract)
        if not facets:
            continue
        identity = _artifact_key(after.source_ref)
        if identity != _artifact_key(before.source_ref):
            continue
        by_identity[identity] = facets
        paths_by_identity[identity] = paths

    enriched: list[ArtifactChange] = []
    for change in changes:
        endpoint = change.after or change.before
        assert endpoint is not None
        details = by_identity.get(_artifact_key(endpoint))
        if details is None:
            enriched.append(change)
            continue
        facet_set = set(change.facets)
        facet_set.update(details)
        path_set = set(change.changed_paths)
        path_set.update(paths_by_identity[_artifact_key(endpoint)])
        enriched.append(
            ArtifactChange.model_validate(
                change.model_dump(mode="python")
                | {
                    "facets": tuple(sorted(facet_set, key=str)),
                    "changed_paths": tuple(sorted(path_set)),
                }
            )
        )
    return tuple(enriched)


def _build_impact(
    previous: ConnectorSnapshot,
    current: ConnectorSnapshot,
    changes: tuple[ArtifactChange, ...],
    policy: ChangeImpactPolicy,
) -> ChangeImpact | None:
    changed = {item.domain for item in changes}
    if not changed:
        return None
    affected: set[ArtifactDomain] = set()
    reasons: dict[str, list[str]] = {}
    for domain in sorted(changed, key=str):
        for target in policy.domain_impacts[domain.value]:
            affected.add(target)
            domain_changes = tuple(item for item in changes if item.domain is domain)
            reasons.setdefault(target.value, []).extend(
                f"{change.change_id}:{facet.value}"
                for change in domain_changes
                for facet in change.facets
            )
    return ChangeImpact(
        from_revision_id=previous.hardware_revision_id,
        to_revision_id=current.hardware_revision_id,
        changed_domains=tuple(sorted(changed, key=str)),
        affected_domains=tuple(sorted(affected, key=str)),
        reasons={
            name: tuple(sorted(set(values))) for name, values in sorted(reasons.items())
        },
    )


def assign_required_retests(
    changes: tuple[ArtifactChange, ...],
    impact: ChangeImpact,
    findings: tuple[ConsistencyFinding, ...],
    policy: ChangeImpactPolicy,
) -> tuple[RetestRequirement, ...]:
    finding_by_rule: dict[str, list[ConsistencyFinding]] = {}
    for finding in findings:
        finding_by_rule.setdefault(finding.rule_id, []).append(finding)
    changed = set(impact.changed_domains)
    merged: dict[tuple[str, EvidenceTier], tuple[set[str], set[str]]] = {}
    for rule in policy.retest_rules:
        matching_domains = changed.intersection(rule.trigger_domains)
        facet_changes = tuple(
            change
            for change in changes
            if change.domain is ArtifactDomain.HARDWARE
            and set(change.facets).intersection(rule.trigger_facets)
        )
        matching_findings = tuple(
            finding
            for rule_id in rule.trigger_finding_rules
            for finding in finding_by_rule.get(rule_id, ())
        )
        if not matching_domains and not facet_changes and not matching_findings:
            continue
        triggered_by = {
            change.change_id for change in changes if change.domain in matching_domains
        }
        triggered_by.update(change.change_id for change in facet_changes)
        triggered_by.update(finding.finding_id for finding in matching_findings)
        reason_codes = {f"rule:{rule.rule_id}", rule.reason_code}
        reason_codes.update(f"changed:{domain.value}" for domain in matching_domains)
        reason_codes.update(
            f"facet:{facet.value}"
            for change in facet_changes
            for facet in set(change.facets).intersection(rule.trigger_facets)
        )
        reason_codes.update(f"finding:{item.rule_id}" for item in matching_findings)
        merge_key = (rule.test_id, rule.required_tier)
        existing = merged.get(merge_key)
        if existing is None:
            merged[merge_key] = (triggered_by, reason_codes)
            continue
        existing_triggers, existing_reasons = existing
        existing_triggers.update(triggered_by)
        existing_reasons.update(reason_codes)
        merged[merge_key] = (existing_triggers, existing_reasons)

    return tuple(
        RetestRequirement(
            retest_id=(
                f"retest:{policy.policy_id}:{policy.policy_version}:"
                f"{test_id}:{tier.value}:{impact.to_revision_id}"
            ),
            test_id=test_id,
            required_tier=tier,
            status=RetestStatus.REQUIRED,
            triggered_by=tuple(sorted(triggers)),
            reason_codes=tuple(sorted(reasons)),
        )
        for (test_id, tier), (triggers, reasons) in sorted(
            merged.items(), key=lambda item: (item[0][0], item[0][1].value)
        )
    )


def assess_change_impact(
    previous: ConnectorSnapshot,
    current: ConnectorSnapshot,
    *,
    interface_projections: tuple[NormalizedInterfaceProjection, ...] = (),
    policy: ChangeImpactPolicy = DEFAULT_CHANGE_IMPACT_POLICY,
) -> ChangeImpactAssessment:
    changes = detect_artifact_changes(previous, current)
    projections = _bind_interface_projections(previous, current, interface_projections)
    changes = _enrich_changes_with_projection_facets(
        changes, previous, current, projections
    )
    impact = _build_impact(previous, current, changes, policy)
    findings: list[ConsistencyFinding] = []
    interface_domains = {
        ArtifactDomain.HARDWARE,
        ArtifactDomain.FIRMWARE,
        ArtifactDomain.PROTOCOL,
    }
    if impact is not None and interface_domains.intersection(impact.affected_domains):
        current_interfaces = {
            domain: projections.get((current.snapshot_id, domain))
            for domain in interface_domains
        }
        hardware = current_interfaces[ArtifactDomain.HARDWARE]
        firmware = current_interfaces[ArtifactDomain.FIRMWARE]
        for domain, projection in sorted(
            current_interfaces.items(), key=lambda item: str(item[0])
        ):
            if projection is None:
                findings.append(
                    _finding(
                        "interface_contract_missing",
                        domain.value,
                        (
                            f"{domain.value} interface contract is required "
                            "for this change"
                        ),
                        tuple(sorted(interface_domains, key=str)),
                        (f"snapshot:{current.snapshot_id}",),
                    )
                )
        if hardware is not None and firmware is not None:
            findings.extend(
                validate_interface_compatibility(
                    hardware.contract,
                    firmware.contract,
                    evidence_refs=(
                        _projection_ref(hardware),
                        _projection_ref(firmware),
                    ),
                )
            )
        protocol = current_interfaces[ArtifactDomain.PROTOCOL]
        if hardware is not None and protocol is not None:
            findings.extend(
                validate_interface_compatibility(
                    hardware.contract,
                    protocol.contract,
                    evidence_refs=(
                        _projection_ref(hardware),
                        _projection_ref(protocol),
                    ),
                    candidate_domain=ArtifactDomain.PROTOCOL,
                )
            )
    if (
        impact is not None
        and previous.hardware_revision_id == current.hardware_revision_id
        and any(change.domain is ArtifactDomain.HARDWARE for change in changes)
    ):
        findings.append(
            _finding(
                "hardware_revision_reused",
                current.hardware_revision_id,
                "artifact content changed without a new hardware revision identifier",
                (ArtifactDomain.HARDWARE,),
                (f"snapshot:{previous.snapshot_id}", f"snapshot:{current.snapshot_id}"),
            )
        )
    finding_tuple = tuple(sorted(findings, key=lambda item: item.finding_id))
    retests = (
        assign_required_retests(changes, impact, finding_tuple, policy)
        if impact is not None
        else ()
    )
    target_protocol = projections.get((current.snapshot_id, ArtifactDomain.PROTOCOL))
    payload = _ChangeImpactAssessmentPayload(
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_hash=canonical_sha256(policy),
        project_id=previous.project_id,
        from_snapshot_id=previous.snapshot_id,
        to_snapshot_id=current.snapshot_id,
        from_snapshot_hash=connector_snapshot_hash(previous),
        to_snapshot_hash=connector_snapshot_hash(current),
        from_hardware_revision_id=previous.hardware_revision_id,
        to_hardware_revision_id=current.hardware_revision_id,
        target_protocol_schema_hash=(
            target_protocol.contract.protocol_schema_hash
            if target_protocol is not None
            else None
        ),
        interface_projection_hashes=tuple(
            sorted(item.projection_hash for item in interface_projections)
        ),
        artifact_changes=changes,
        impact=impact,
        findings=finding_tuple,
        required_retests=retests,
    )
    return ChangeImpactAssessment(
        **payload.model_dump(mode="python"),
        analysis_hash=canonical_sha256(payload),
    )


analyze_change = assess_change_impact


__all__ = [
    "ArtifactChange",
    "ArtifactChangeType",
    "ChangeFacet",
    "ChangeImpactAssessment",
    "ChangeImpactPolicy",
    "DEFAULT_CHANGE_IMPACT_POLICY",
    "NormalizedInterfaceProjection",
    "RetestRule",
    "analyze_change",
    "assess_change_impact",
    "assign_required_retests",
    "connector_snapshot_hash",
    "detect_artifact_changes",
    "interface_projection_hash",
    "validate_interface_compatibility",
]
