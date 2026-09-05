from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType

from forge_core.change_management import (
    ArtifactDomain,
    ChangeImpact,
    ConsistencyFinding,
    ExternalArtifactRef,
    FindingSeverity,
    RetestRequirement,
)
from forge_core.change_planning import (
    ChangeImpactPreview,
    ChangeScenario,
    ComponentSpecification,
    PlanDeviation,
    PlanDeviationKind,
    PlannedChangeAction,
    PlannedComponentChange,
    PlanVerification,
    PreviewFinding,
    PreviewFindingSeverity,
    PreviewRecommendation,
    change_impact_preview_hash,
    plan_verification_hash,
)
from forge_core.constraints import InterfaceContract, InterfaceSignal
from forge_core.impact_engine import (
    DEFAULT_CHANGE_IMPACT_POLICY,
    ArtifactChange,
    ArtifactChangeType,
    ChangeFacet,
    ChangeImpactAssessment,
    ChangeImpactPolicy,
    assign_required_retests,
)
from forge_core.models import Quantity

PLANNER_VERSION = "1.0.0"

_ALL_DOMAINS = tuple(sorted(ArtifactDomain, key=lambda item: item.value))
_INTERFACE_DOMAINS = tuple(
    sorted(
        {
            ArtifactDomain.FIRMWARE,
            ArtifactDomain.HARDWARE,
            ArtifactDomain.PROTOCOL,
            ArtifactDomain.TEST,
            ArtifactDomain.DOCUMENTATION,
        },
        key=lambda item: item.value,
    )
)
_BOM_DOMAINS = tuple(
    sorted(
        {
            ArtifactDomain.BOM,
            ArtifactDomain.TEST,
            ArtifactDomain.DOCUMENTATION,
        },
        key=lambda item: item.value,
    )
)

_ACTION_BY_FACET: Mapping[ChangeFacet, tuple[str, ...]] = MappingProxyType(
    {
        ChangeFacet.REVISION_IDENTIFIER: ("assign-hardware-revision",),
        ChangeFacet.SOURCE_REVISION: ("refresh-source-baseline",),
        ChangeFacet.ARTIFACT_ADDED: ("add-component-to-bom",),
        ChangeFacet.ARTIFACT_REMOVED: ("remove-component-from-bom",),
        ChangeFacet.OPAQUE_CONTENT: ("review-component-datasheet",),
        ChangeFacet.PIN_ASSIGNMENT: ("firmware-pinmap-update",),
        ChangeFacet.VOLTAGE_RANGE: ("bench-electrical", "voltage-rail-review"),
        ChangeFacet.UNIT: ("firmware-unit-conversion-review",),
        ChangeFacet.COMMAND_RANGE: ("firmware-command-range-update",),
        ChangeFacet.SAFE_VALUE: ("firmware-safe-state-update",),
        ChangeFacet.PROTOCOL_SCHEMA: ("protocol-schema-review",),
    }
)
_BOM_ACTIONS = ("bom-quote-refresh", "bom-line-update")


def preview_change_scenario(
    scenario: ChangeScenario,
    *,
    generated_at: datetime,
    policy: ChangeImpactPolicy = DEFAULT_CHANGE_IMPACT_POLICY,
) -> ChangeImpactPreview:
    """Forecast the validation impact of a planned hardware/component change.

    The result is planning data only. It never binds release evidence or marks a
    required retest as satisfied.
    """

    findings: list[PreviewFinding] = []
    missing: set[str] = set()
    assumptions: set[str] = {
        "preview-uses-candidate-source-snapshots",
        "physics-simulation-results-must-arrive-as-external-evidence",
    }
    changed_domains: set[ArtifactDomain] = set()
    affected_domains: set[ArtifactDomain] = set()
    facets_by_change: dict[str, set[ChangeFacet]] = {}
    required_actions: set[str] = set()

    for change in scenario.changes:
        before, after = change.before, change.after
        endpoints = tuple(item for item in (before, after) if item is not None)
        missing.update(_missing_information(change))
        changed_domains.update(_changed_domains(change))
        affected_domains.update(_domains_for_action(change))
        facets = _facets_for_change(change)
        facets_by_change[change.change_id] = set(facets)
        required_actions.update(change.required_actions)
        derived_actions = set(change.required_actions)
        for facet in facets:
            derived_actions.update(_ACTION_BY_FACET[facet])
        if _change_has_bom_impact(change):
            derived_actions.update(_BOM_ACTIONS)
        for endpoint in endpoints:
            derived_actions.add(
                f"verify-source:{endpoint.source_ref.source_system.value}"
            )
        required_actions.update(derived_actions)
        findings.extend(_findings_for_change(change, scenario.scenario_hash))
        findings.extend(
            _plan_scope_findings(
                change,
                derived_domains=_domains_for_action(change),
                derived_actions=derived_actions,
            )
        )

    for domain in tuple(changed_domains):
        affected_domains.update(policy.domain_impacts[domain.value])
    if ArtifactDomain.HARDWARE in changed_domains:
        affected_domains.update(_ALL_DOMAINS)

    if missing:
        recommendation = PreviewRecommendation.INDETERMINATE
    elif any(
        finding.severity is PreviewFindingSeverity.INCOMPATIBILITY
        for finding in findings
    ):
        recommendation = PreviewRecommendation.INCOMPATIBLE
    else:
        recommendation = PreviewRecommendation.CHANGES_REQUIRED

    finding_tuple = tuple(sorted(findings, key=lambda item: item.finding_id))
    change_tuple = _artifact_changes_for_plan(scenario, facets_by_change)
    impact = _impact_for_plan(scenario, change_tuple, affected_domains, policy)
    consistency_findings = tuple(
        ConsistencyFinding(
            finding_id=item.finding_id,
            rule_id=item.rule_id,
            severity=FindingSeverity.BLOCKER,
            summary=item.summary,
            affected_domains=item.affected_domains,
            evidence_refs=item.evidence_refs or (scenario.scenario_hash,),
        )
        for item in finding_tuple
    )
    retests = (
        assign_required_retests(change_tuple, impact, consistency_findings, policy)
        if impact is not None
        else ()
    )
    preview_id = _stable_id(
        "preview",
        scenario.scenario_hash,
        generated_at.isoformat(),
    )
    predicted_affected_domains = tuple(
        sorted(
            affected_domains or {ArtifactDomain.HARDWARE},
            key=lambda item: item.value,
        )
    )
    predicted_required_actions = tuple(sorted(required_actions or {"no-op-review"}))
    predicted_retests = tuple(sorted(retests, key=_retest_key))
    assumption_tuple = tuple(sorted(assumptions))
    missing_tuple = tuple(sorted(missing))
    preview_hash = change_impact_preview_hash(
        preview_id=preview_id,
        project_id=scenario.project_id,
        scenario_id=scenario.scenario_id,
        scenario_hash=scenario.scenario_hash,
        planner_version=PLANNER_VERSION,
        generated_at=generated_at,
        recommendation=recommendation,
        predicted_affected_domains=predicted_affected_domains,
        predicted_required_actions=predicted_required_actions,
        predicted_findings=finding_tuple,
        predicted_retests=predicted_retests,
        assumptions=assumption_tuple,
        missing_information=missing_tuple,
    )
    return ChangeImpactPreview(
        preview_id=preview_id,
        project_id=scenario.project_id,
        scenario_id=scenario.scenario_id,
        scenario_hash=scenario.scenario_hash,
        planner_version=PLANNER_VERSION,
        generated_at=generated_at,
        recommendation=recommendation,
        predicted_affected_domains=predicted_affected_domains,
        predicted_required_actions=predicted_required_actions,
        predicted_findings=finding_tuple,
        predicted_retests=predicted_retests,
        assumptions=assumption_tuple,
        missing_information=missing_tuple,
        preview_hash=preview_hash,
    )


def verify_plan_against_actual(
    preview: ChangeImpactPreview,
    scenario: ChangeScenario,
    actual: ChangeImpactAssessment,
    *,
    verified_at: datetime,
) -> PlanVerification:
    if preview.project_id != scenario.project_id:
        raise ValueError("preview belongs to a different project")
    if preview.scenario_id != scenario.scenario_id:
        raise ValueError("preview does not belong to scenario")
    if preview.scenario_hash != scenario.scenario_hash:
        raise ValueError("preview does not belong to scenario")
    if actual.project_id != scenario.project_id:
        raise ValueError("actual change assessment belongs to a different project")

    expected = _expected_plan_refs(preview, scenario)
    observed = _actual_refs(actual).intersection(expected)
    deviations: list[PlanDeviation] = []
    deviations.extend(_scenario_target_deviations(scenario, actual))
    for ref in sorted(expected - observed):
        deviations.append(_deviation_for_missing(ref))
    for ref in sorted(_actual_refs(actual) - expected):
        deviations.append(_deviation_for_unplanned(ref))

    deviation_tuple = tuple(sorted(deviations, key=lambda item: item.deviation_id))
    verification_id = _stable_id(
        "plan-verification",
        preview.preview_hash,
        actual.analysis_hash,
        verified_at.isoformat(),
    )
    matches_plan = not deviation_tuple
    observed_planned_refs = tuple(sorted(observed))
    verification_hash = plan_verification_hash(
        verification_id=verification_id,
        project_id=preview.project_id,
        scenario_id=preview.scenario_id,
        preview_hash=preview.preview_hash,
        actual_change_analysis_hash=actual.analysis_hash,
        verified_at=verified_at,
        matches_plan=matches_plan,
        observed_planned_refs=observed_planned_refs,
        deviations=deviation_tuple,
    )
    return PlanVerification(
        verification_id=verification_id,
        project_id=preview.project_id,
        scenario_id=preview.scenario_id,
        preview_hash=preview.preview_hash,
        actual_change_analysis_hash=actual.analysis_hash,
        verified_at=verified_at,
        matches_plan=matches_plan,
        observed_planned_refs=observed_planned_refs,
        deviations=deviation_tuple,
        verification_hash=verification_hash,
    )


def _stable_id(prefix: str, *parts: str) -> str:
    payload = json.dumps(
        {"parts": tuple(parts), "prefix": prefix},
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(payload).hexdigest()[:16]}"


def _source_ref_id(source: ExternalArtifactRef) -> str:
    return (
        f"source:{source.domain.value}:{source.source_system.value}:"
        f"{source.artifact_id}:{source.source_revision}:{source.content_hash}"
    )


def _quantity_signature(value: Quantity | None) -> tuple[str, str, float] | None:
    if value is None:
        return None
    return value.unit, value.dimension, value.value


def _money_signature(value: Decimal) -> str:
    return str(value.normalize())


def _retest_key(item: RetestRequirement) -> tuple[str, str, str]:
    return item.retest_id, item.test_id, item.required_tier.value


def _changed_domains(change: PlannedComponentChange) -> set[ArtifactDomain]:
    if change.action in {PlannedChangeAction.ADD, PlannedChangeAction.REMOVE}:
        return {ArtifactDomain.HARDWARE, ArtifactDomain.BOM}
    domains = {ArtifactDomain.HARDWARE}
    if _change_has_bom_impact(change):
        domains.add(ArtifactDomain.BOM)
    return domains


def _domains_for_action(change: PlannedComponentChange) -> set[ArtifactDomain]:
    domains = set(change.affected_domains)
    if _interface_changed(change):
        domains.update(_INTERFACE_DOMAINS)
    if _change_has_bom_impact(change):
        domains.update(_BOM_DOMAINS)
    if change.action in {PlannedChangeAction.ADD, PlannedChangeAction.REMOVE}:
        domains.update(_ALL_DOMAINS)
    return domains


def _missing_information(change: PlannedComponentChange) -> set[str]:
    missing: set[str] = set()
    for label, component in (("before", change.before), ("after", change.after)):
        if component is None:
            continue
        if component.interface_contract is None:
            missing.add(f"{change.change_id}:{label}:interface_contract")
        if component.quote is None:
            missing.add(f"{change.change_id}:{label}:quote")
        if (
            component.source_ref.source_revision == ""
            or component.source_ref.content_hash == ""
        ):
            missing.add(f"{change.change_id}:{label}:source_ref")
    return missing


def _change_has_bom_impact(change: PlannedComponentChange) -> bool:
    before, after = change.before, change.after
    if before is None or after is None:
        return True
    return (
        before.part_number != after.part_number
        or before.quantity != after.quantity
        or _quote_signature(before) != _quote_signature(after)
    )


def _interface_changed(change: PlannedComponentChange) -> bool:
    before, after = change.before, change.after
    if before is None or after is None:
        return True
    return before.interface_contract != after.interface_contract


def _quote_signature(component: ComponentSpecification) -> tuple[str, ...] | None:
    quote = component.quote
    if quote is None:
        return None
    return (
        quote.quote_id,
        quote.part_number,
        quote.supplier,
        quote.region,
        quote.currency,
        _money_signature(quote.unit_price),
        str(quote.minimum_quantity),
        quote.observed_at.isoformat(),
        quote.expires_at.isoformat(),
        str(quote.shipping_included),
        str(quote.shipping_cost),
        str(quote.tax_included),
        str(quote.tax_cost),
        quote.source_url,
        quote.source_hash,
    )


def _facets_for_change(change: PlannedComponentChange) -> tuple[ChangeFacet, ...]:
    if change.action is PlannedChangeAction.ADD:
        return (ChangeFacet.ARTIFACT_ADDED,)
    if change.action is PlannedChangeAction.REMOVE:
        return (ChangeFacet.ARTIFACT_REMOVED,)
    assert change.before is not None
    assert change.after is not None
    facets: set[ChangeFacet] = set()
    if (
        change.before.source_ref.source_revision
        != change.after.source_ref.source_revision
    ):
        facets.add(ChangeFacet.SOURCE_REVISION)
    if change.before.source_ref.content_hash != change.after.source_ref.content_hash:
        facets.add(ChangeFacet.OPAQUE_CONTENT)
    if change.before.part_number != change.after.part_number:
        facets.add(ChangeFacet.OPAQUE_CONTENT)
    facets.update(
        _interface_facets(
            change.before.interface_contract,
            change.after.interface_contract,
        )
    )
    return tuple(
        sorted(
            facets or {ChangeFacet.OPAQUE_CONTENT},
            key=lambda item: item.value,
        )
    )


def _interface_facets(
    before: InterfaceContract | None, after: InterfaceContract | None
) -> set[ChangeFacet]:
    if before is None or after is None:
        return {ChangeFacet.OPAQUE_CONTENT}
    facets: set[ChangeFacet] = set()
    if before.protocol_schema_hash != after.protocol_schema_hash:
        facets.add(ChangeFacet.PROTOCOL_SCHEMA)
    before_signals = {signal.name: signal for signal in before.signals}
    after_signals = {signal.name: signal for signal in after.signals}
    for name in sorted(set(before_signals) | set(after_signals)):
        old = before_signals.get(name)
        new = after_signals.get(name)
        if old is None or new is None:
            facets.add(ChangeFacet.PIN_ASSIGNMENT)
            continue
        facets.update(_signal_facets(old, new))
    return facets


def _signal_facets(before: InterfaceSignal, after: InterfaceSignal) -> set[ChangeFacet]:
    facets: set[ChangeFacet] = set()
    if before.pin != after.pin or before.direction != after.direction:
        facets.add(ChangeFacet.PIN_ASSIGNMENT)
    if (
        before.voltage_min.unit,
        before.voltage_min.dimension,
        before.voltage_max.unit,
        before.voltage_max.dimension,
    ) != (
        after.voltage_min.unit,
        after.voltage_min.dimension,
        after.voltage_max.unit,
        after.voltage_max.dimension,
    ):
        facets.add(ChangeFacet.UNIT)
    elif (
        before.voltage_min.value != after.voltage_min.value
        or before.voltage_max.value != after.voltage_max.value
    ):
        facets.add(ChangeFacet.VOLTAGE_RANGE)
    if (
        _quantity_signature(before.command_min),
        _quantity_signature(before.command_max),
    ) != (
        _quantity_signature(after.command_min),
        _quantity_signature(after.command_max),
    ):
        old_unit = (
            None
            if before.command_min is None
            else (before.command_min.unit, before.command_min.dimension)
        )
        new_unit = (
            None
            if after.command_min is None
            else (after.command_min.unit, after.command_min.dimension)
        )
        facets.add(
            ChangeFacet.UNIT if old_unit != new_unit else ChangeFacet.COMMAND_RANGE
        )
    if _quantity_signature(before.safe_value) != _quantity_signature(after.safe_value):
        facets.add(ChangeFacet.SAFE_VALUE)
    return facets


def _findings_for_change(
    change: PlannedComponentChange, scenario_hash: str
) -> tuple[PreviewFinding, ...]:
    findings: list[PreviewFinding] = []
    before, after = change.before, change.after
    refs = tuple(
        sorted(
            _source_ref_id(component.source_ref)
            for component in (before, after)
            if component is not None
        )
        or (scenario_hash,)
    )
    if before is None or after is None:
        action = "adds" if before is None else "removes"
        return (
            _preview_finding(
                change.change_id,
                "component_presence_change",
                PreviewFindingSeverity.RISK,
                f"planned change {action} component {change.change_id}",
                _ALL_DOMAINS,
                refs,
            ),
        )
    if before.part_number != after.part_number:
        findings.append(
            _preview_finding(
                change.change_id,
                "part_number_change",
                PreviewFindingSeverity.RISK,
                (
                    f"component {change.change_id} changes part "
                    f"{before.part_number} to {after.part_number}"
                ),
                _BOM_DOMAINS,
                refs,
            )
        )
    if before.quantity != after.quantity:
        findings.append(
            _preview_finding(
                change.change_id,
                "quantity_change",
                PreviewFindingSeverity.RISK,
                f"component {change.change_id} quantity changes",
                _BOM_DOMAINS,
                refs,
            )
        )
    if _quote_signature(before) != _quote_signature(after):
        findings.append(
            _preview_finding(
                change.change_id,
                "quote_provenance_or_price_change",
                PreviewFindingSeverity.RISK,
                f"component {change.change_id} quote price or provenance changes",
                _BOM_DOMAINS,
                refs,
            )
        )
    findings.extend(_interface_findings(change.change_id, before, after, refs))
    return tuple(sorted(findings, key=lambda item: item.finding_id))


def _plan_scope_findings(
    change: PlannedComponentChange,
    *,
    derived_domains: set[ArtifactDomain],
    derived_actions: set[str],
) -> tuple[PreviewFinding, ...]:
    findings: list[PreviewFinding] = []
    refs = tuple(
        sorted(
            _source_ref_id(component.source_ref)
            for component in (change.before, change.after)
            if component is not None
        )
    )
    declared_domains = set(change.affected_domains)
    declared_actions = set(change.required_actions)
    missing_domains = derived_domains - declared_domains
    missing_actions = derived_actions - declared_actions
    if missing_domains:
        findings.append(
            _preview_finding(
                change.change_id,
                "plan_scope_understates_domains",
                PreviewFindingSeverity.RISK,
                "planned affected domains omit derived impact domains",
                tuple(sorted(missing_domains, key=lambda item: item.value)),
                refs,
            )
        )
    if missing_actions:
        findings.append(
            _preview_finding(
                change.change_id,
                "plan_scope_understates_actions",
                PreviewFindingSeverity.RISK,
                "planned required actions omit derived validation work",
                tuple(sorted(derived_domains, key=lambda item: item.value)),
                refs,
            )
        )
    return tuple(findings)


def _interface_findings(
    change_id: str,
    before: ComponentSpecification,
    after: ComponentSpecification,
    refs: tuple[str, ...],
) -> tuple[PreviewFinding, ...]:
    before_contract = before.interface_contract
    after_contract = after.interface_contract
    if before_contract is None or after_contract is None:
        return (
            _preview_finding(
                change_id,
                "interface_contract_missing",
                PreviewFindingSeverity.MISSING_INFORMATION,
                f"component {change_id} is missing source-bound interface data",
                _INTERFACE_DOMAINS,
                refs,
            ),
        )
    findings: list[PreviewFinding] = []
    if before_contract.protocol_schema_hash != after_contract.protocol_schema_hash:
        findings.append(
            _preview_finding(
                change_id,
                "protocol_schema_change",
                PreviewFindingSeverity.RISK,
                f"component {change_id} changes protocol schema",
                _INTERFACE_DOMAINS,
                refs,
            )
        )
    before_signals = {signal.name: signal for signal in before_contract.signals}
    after_signals = {signal.name: signal for signal in after_contract.signals}
    for name in sorted(set(before_signals) | set(after_signals)):
        old = before_signals.get(name)
        new = after_signals.get(name)
        if old is None or new is None:
            findings.append(
                _preview_finding(
                    change_id,
                    f"signal_presence_change:{name}",
                    PreviewFindingSeverity.INCOMPATIBILITY,
                    f"component {change_id} changes signal presence for {name}",
                    _INTERFACE_DOMAINS,
                    refs,
                )
            )
            continue
        findings.extend(_signal_findings(change_id, name, old, new, refs))
    return tuple(findings)


def _signal_findings(
    change_id: str,
    signal_name: str,
    before: InterfaceSignal,
    after: InterfaceSignal,
    refs: tuple[str, ...],
) -> tuple[PreviewFinding, ...]:
    findings: list[PreviewFinding] = []
    if before.pin != after.pin or before.direction != after.direction:
        findings.append(
            _preview_finding(
                change_id,
                "pin_assignment_change",
                PreviewFindingSeverity.RISK,
                f"signal {signal_name} pin assignment or direction changes",
                _INTERFACE_DOMAINS,
                refs,
            )
        )
    if (
        before.voltage_min.unit,
        before.voltage_min.dimension,
        before.voltage_max.unit,
        before.voltage_max.dimension,
    ) != (
        after.voltage_min.unit,
        after.voltage_min.dimension,
        after.voltage_max.unit,
        after.voltage_max.dimension,
    ):
        severity = (
            PreviewFindingSeverity.INCOMPATIBILITY
            if (
                before.voltage_min.dimension,
                before.voltage_max.dimension,
            )
            != (
                after.voltage_min.dimension,
                after.voltage_max.dimension,
            )
            else PreviewFindingSeverity.RISK
        )
        findings.append(
            _preview_finding(
                change_id,
                "voltage_unit_change",
                severity,
                f"signal {signal_name} voltage unit or dimension changes",
                _INTERFACE_DOMAINS,
                refs,
            )
        )
    elif (
        before.voltage_min.value != after.voltage_min.value
        or before.voltage_max.value != after.voltage_max.value
    ):
        incompatible = (
            before.voltage_max.value < after.voltage_min.value
            or after.voltage_max.value < before.voltage_min.value
        )
        findings.append(
            _preview_finding(
                change_id,
                (
                    "voltage_range_incompatible"
                    if incompatible
                    else "voltage_range_change"
                ),
                (
                    PreviewFindingSeverity.INCOMPATIBILITY
                    if incompatible
                    else PreviewFindingSeverity.RISK
                ),
                f"signal {signal_name} voltage range changes",
                _INTERFACE_DOMAINS,
                refs,
            )
        )
    if (
        _quantity_signature(before.command_min),
        _quantity_signature(before.command_max),
    ) != (
        _quantity_signature(after.command_min),
        _quantity_signature(after.command_max),
    ):
        old_min, old_max = before.command_min, before.command_max
        new_min, new_max = after.command_min, after.command_max
        incompatible = (
            old_min is None
            or old_max is None
            or new_min is None
            or new_max is None
            or old_min.dimension != new_min.dimension
            or old_max.dimension != new_max.dimension
            or old_max.value < new_min.value
            or new_max.value < old_min.value
        )
        findings.append(
            _preview_finding(
                change_id,
                (
                    "command_range_incompatible"
                    if incompatible
                    else "command_range_change"
                ),
                (
                    PreviewFindingSeverity.INCOMPATIBILITY
                    if incompatible
                    else PreviewFindingSeverity.RISK
                ),
                f"signal {signal_name} command range or unit changes",
                _INTERFACE_DOMAINS,
                refs,
            )
        )
    if _quantity_signature(before.safe_value) != _quantity_signature(after.safe_value):
        findings.append(
            _preview_finding(
                change_id,
                "safe_value_change",
                PreviewFindingSeverity.RISK,
                f"signal {signal_name} safe value changes",
                _INTERFACE_DOMAINS,
                refs,
            )
        )
    return tuple(findings)


def _preview_finding(
    change_id: str,
    rule_id: str,
    severity: PreviewFindingSeverity,
    summary: str,
    domains: Iterable[ArtifactDomain],
    refs: tuple[str, ...],
) -> PreviewFinding:
    return PreviewFinding(
        finding_id=f"preview:{change_id}:{rule_id}",
        rule_id=rule_id,
        severity=severity,
        summary=summary,
        affected_domains=tuple(sorted(set(domains), key=lambda item: item.value)),
        evidence_refs=tuple(sorted(set(refs))),
    )


def _artifact_changes_for_plan(
    scenario: ChangeScenario, facets_by_change: Mapping[str, set[ChangeFacet]]
) -> tuple[ArtifactChange, ...]:
    changes: list[ArtifactChange] = []
    for change in scenario.changes:
        endpoint = change.after or change.before
        if endpoint is None:
            continue
        change_type = (
            ArtifactChangeType.ADDED
            if change.action is PlannedChangeAction.ADD
            else ArtifactChangeType.REMOVED
            if change.action is PlannedChangeAction.REMOVE
            else ArtifactChangeType.MODIFIED
        )
        facets = tuple(
            sorted(
                facets_by_change[change.change_id],
                key=lambda item: item.value,
            )
        )
        paths = tuple(f"planned.{facet.value}" for facet in facets)
        changes.append(
            ArtifactChange(
                change_id=f"planned:{change.change_id}",
                change_type=change_type,
                domain=endpoint.source_ref.domain,
                before=change.before.source_ref if change.before is not None else None,
                after=change.after.source_ref if change.after is not None else None,
                facets=facets,
                changed_paths=paths or ("planned.component",),
            )
        )
    return tuple(sorted(changes, key=lambda item: item.change_id))


def _impact_for_plan(
    scenario: ChangeScenario,
    changes: tuple[ArtifactChange, ...],
    affected_domains: set[ArtifactDomain],
    policy: ChangeImpactPolicy,
) -> ChangeImpact | None:
    if not changes:
        return None
    changed = {change.domain for change in changes}
    affected = set(affected_domains)
    for domain in changed:
        affected.update(policy.domain_impacts[domain.value])
    reasons: dict[str, list[str]] = {domain.value: [] for domain in affected}
    for domain in sorted(affected, key=lambda item: item.value):
        for change in changes:
            if change.domain in changed:
                reasons[domain.value].extend(
                    f"{change.change_id}:{facet.value}" for facet in change.facets
                )
    return ChangeImpact(
        from_revision_id=scenario.baseline_snapshot_id,
        to_revision_id=scenario.proposed_hardware_revision_id,
        changed_domains=tuple(sorted(changed, key=lambda item: item.value)),
        affected_domains=tuple(sorted(affected, key=lambda item: item.value)),
        reasons={
            key: tuple(sorted(set(values)))
            for key, values in sorted(reasons.items())
            if values
        },
    )


def _expected_plan_refs(
    preview: ChangeImpactPreview, scenario: ChangeScenario
) -> set[str]:
    refs = {f"domain:{domain.value}" for domain in preview.predicted_affected_domains}
    refs.update(f"finding:{finding.rule_id}" for finding in preview.predicted_findings)
    refs.update(
        f"retest:{retest.test_id}:{retest.required_tier.value}"
        for retest in preview.predicted_retests
    )
    for change in scenario.changes:
        refs.update(f"facet:{facet.value}" for facet in _facets_for_change(change))
    return refs


def _actual_refs(actual: ChangeImpactAssessment) -> set[str]:
    refs: set[str] = set()
    if actual.impact is not None:
        refs.update(
            f"domain:{domain.value}" for domain in actual.impact.affected_domains
        )
    refs.update(f"finding:{finding.rule_id}" for finding in actual.findings)
    refs.update(
        f"retest:{retest.test_id}:{retest.required_tier.value}"
        for retest in actual.required_retests
    )
    for change in actual.artifact_changes:
        refs.update(f"facet:{facet.value}" for facet in change.facets)
    return refs


def _scenario_target_deviations(
    scenario: ChangeScenario, actual: ChangeImpactAssessment
) -> tuple[PlanDeviation, ...]:
    deviations: list[PlanDeviation] = []
    if (
        scenario.baseline_snapshot_id != actual.from_snapshot_id
        or scenario.baseline_snapshot_hash != actual.from_snapshot_hash
    ):
        deviations.append(
            PlanDeviation(
                deviation_id="deviation:target:baseline_snapshot",
                kind=PlanDeviationKind.DOMAIN_MISMATCH,
                summary="actual analysis does not use the scenario baseline snapshot",
                affected_domains=(ArtifactDomain.HARDWARE,),
                planned_ref=(
                    f"baseline:{scenario.baseline_snapshot_id}:"
                    f"{scenario.baseline_snapshot_hash}"
                ),
                actual_ref=(
                    f"baseline:{actual.from_snapshot_id}:{actual.from_snapshot_hash}"
                ),
            )
        )
    if scenario.proposed_hardware_revision_id != actual.to_hardware_revision_id:
        deviations.append(
            PlanDeviation(
                deviation_id="deviation:target:hardware_revision",
                kind=PlanDeviationKind.DOMAIN_MISMATCH,
                summary=(
                    "actual analysis does not target the proposed hardware revision"
                ),
                affected_domains=(ArtifactDomain.HARDWARE,),
                planned_ref=f"revision:{scenario.proposed_hardware_revision_id}",
                actual_ref=f"revision:{actual.to_hardware_revision_id}",
            )
        )
    return tuple(deviations)


def _deviation_for_missing(ref: str) -> PlanDeviation:
    return PlanDeviation(
        deviation_id=f"deviation:missing:{ref}",
        kind=_missing_kind(ref),
        summary=f"planned validation reference was not observed: {ref}",
        affected_domains=_domains_for_ref(ref),
        planned_ref=ref,
    )


def _deviation_for_unplanned(ref: str) -> PlanDeviation:
    return PlanDeviation(
        deviation_id=f"deviation:unplanned:{ref}",
        kind=PlanDeviationKind.UNPLANNED_ACTUAL_CHANGE,
        summary=f"actual change introduced an unplanned validation reference: {ref}",
        affected_domains=_domains_for_ref(ref),
        actual_ref=ref,
    )


def _missing_kind(ref: str) -> PlanDeviationKind:
    if ref.startswith("domain:"):
        return PlanDeviationKind.DOMAIN_MISMATCH
    if ref.startswith("retest:"):
        return PlanDeviationKind.RETEST_GAP
    if ref.startswith("finding:"):
        return PlanDeviationKind.FINDING_GAP
    return PlanDeviationKind.MISSING_PLANNED_CHANGE


def _domains_for_ref(ref: str) -> tuple[ArtifactDomain, ...]:
    if ref.startswith("domain:"):
        domain_name = ref.removeprefix("domain:")
        return (ArtifactDomain(domain_name),)
    if ref.startswith("retest:"):
        return (ArtifactDomain.TEST,)
    if ref.startswith("finding:"):
        return _ALL_DOMAINS
    if ref.startswith("facet:"):
        return (ArtifactDomain.HARDWARE,)
    return _ALL_DOMAINS


__all__ = [
    "PLANNER_VERSION",
    "preview_change_scenario",
    "verify_plan_against_actual",
]
