from __future__ import annotations

import hashlib
import inspect
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from forge_core.hashing import canonical_sha256
from forge_core.models import (
    AnalysisResult,
    AnalysisRunRecord,
    EngineeringSpec,
    ExecutionOutcome,
    PreflightResult,
    Quantity,
    Requirement,
    RunManifest,
    RunStatus,
    SpecStatus,
    Verdict,
    VerificationBundle,
    VerificationResult,
)
from forge_core.plugin import PhysicsPlugin
from forge_core.verification import aggregate_verdict

PluginCoordinates = tuple[str, str, str]
PluginIdentity = tuple[str, str, str, str]


@dataclass(frozen=True)
class PluginRegistration:
    plugin_id: str
    plugin_version: str
    schema_version: str
    artifact_hash: str
    artifact_path: Path

    @property
    def coordinates(self) -> PluginCoordinates:
        return (self.plugin_id, self.plugin_version, self.schema_version)

    @property
    def identity(self) -> PluginIdentity:
        return (*self.coordinates, self.artifact_hash)


class AnalysisEngine:
    def __init__(
        self,
        *,
        engine_version: str,
        policy_version: str,
        allowed_plugins: Iterable[PluginRegistration],
    ) -> None:
        self.engine_version = engine_version
        self.policy_version = policy_version
        registrations = tuple(allowed_plugins)
        self.plugin_registry = {
            registration.coordinates: registration for registration in registrations
        }
        if len(self.plugin_registry) != len(registrations):
            raise ValueError("plugin registry coordinates must be unique")

    def execute(self, spec: EngineeringSpec, plugin: PhysicsPlugin) -> ExecutionOutcome:
        try:
            spec = EngineeringSpec.model_validate(spec.model_dump(mode="python"))
        except ValidationError:
            preflight = _rejected_preflight(spec, "invalid_spec_contract")
            return ExecutionOutcome(
                preflight=preflight,
                run=None,
                verifications=_indeterminate_verifications(
                    spec,
                    reason_code="invalid_spec_contract",
                    evidence_ref=f"preflight:{spec.spec_id}:{spec.spec_version}",
                ),
                overall_verdict=Verdict.INDETERMINATE,
            )

        plugin_coordinates = _plugin_coordinates(plugin)
        registration = self.plugin_registry.get(plugin_coordinates)
        spec_hash_before = canonical_sha256(spec)
        preflight = self._preflight(spec, plugin, registration, spec_hash_before)
        if not preflight.accepted:
            verifications = _indeterminate_verifications(
                spec,
                reason_code=_preflight_reason(preflight),
                evidence_ref=f"preflight:{spec.spec_id}:{spec.spec_version}",
                requirement_reasons=preflight.requirement_reasons,
            )
            return ExecutionOutcome(
                preflight=preflight,
                run=None,
                verifications=verifications,
                overall_verdict=Verdict.INDETERMINATE,
            )

        run_id = f"run-{uuid4()}"
        result: AnalysisResult | None = None
        assert registration is not None
        try:
            plugin_result = plugin.run(spec)
            _require_plugin_registration(plugin, registration)
            try:
                result = AnalysisResult.model_validate(
                    plugin_result.model_dump(mode="python")
                )
            except ValidationError as exc:
                raise ContractViolation("invalid_analysis_contract") from exc
            if canonical_sha256(spec) != spec_hash_before:
                raise ContractViolation("approved_spec_mutated")
            plugin_verifications = plugin.verify(spec, result)
            _require_plugin_registration(plugin, registration)
            try:
                verifications = tuple(
                    VerificationResult.model_validate(item.model_dump(mode="python"))
                    for item in plugin_verifications
                )
            except ValidationError as exc:
                raise ContractViolation("invalid_verification_contract") from exc
            _validate_verification_coverage(spec, result, verifications)
            if canonical_sha256(spec) != spec_hash_before:
                raise ContractViolation("approved_spec_mutated")
        except ContractViolation as exc:
            return self._failed_outcome(
                spec=spec,
                preflight=preflight,
                run_id=run_id,
                reason_code=str(exc),
                result=result,
            )
        except Exception:
            return self._failed_outcome(
                spec=spec,
                preflight=preflight,
                run_id=run_id,
                reason_code="plugin_failed",
                result=result,
            )

        overall_verdict = aggregate_verdict(verifications)
        verification_bundle = VerificationBundle(
            results=verifications, overall_verdict=overall_verdict
        )
        manifest = RunManifest(
            spec_hash=spec_hash_before,
            plugin_id=registration.plugin_id,
            plugin_version=registration.plugin_version,
            plugin_schema_version=registration.schema_version,
            plugin_artifact_hash=registration.artifact_hash,
            engine_version=self.engine_version,
            policy_version=self.policy_version,
            input_hash=spec_hash_before,
            output_hash=canonical_sha256(result),
            verification_hash=canonical_sha256(verification_bundle),
        )
        run = AnalysisRunRecord(
            run_id=run_id,
            status=RunStatus.SUCCEEDED,
            analysis_result=result,
            manifest=manifest,
        )
        return ExecutionOutcome(
            preflight=preflight,
            run=run,
            verifications=verifications,
            overall_verdict=overall_verdict,
        )

    def _preflight(
        self,
        spec: EngineeringSpec,
        plugin: PhysicsPlugin,
        registration: PluginRegistration | None,
        spec_hash_before: str,
    ) -> PreflightResult:
        if spec.status is not SpecStatus.APPROVED:
            return _rejected_preflight(spec, "spec_not_approved")
        if registration is None:
            return _rejected_preflight(spec, "plugin_not_allowed")
        assert spec.model is not None
        if _spec_plugin_identity(spec) != registration.identity:
            return _rejected_preflight(spec, "plugin_not_approved_for_spec")
        try:
            _require_plugin_registration(plugin, registration)
            plugin_result = plugin.preflight(spec)
            _require_plugin_registration(plugin, registration)
            result = PreflightResult.model_validate(
                plugin_result.model_dump(mode="python")
            )
        except ContractViolation as exc:
            return _rejected_preflight(spec, str(exc))
        except Exception:
            return _rejected_preflight(spec, "plugin_preflight_failed")
        if canonical_sha256(spec) != spec_hash_before:
            return _rejected_preflight(spec, "approved_spec_mutated")
        if result.spec_id != spec.spec_id or result.spec_version != spec.spec_version:
            return _rejected_preflight(spec, "plugin_preflight_identity_mismatch")
        return result

    def _failed_outcome(
        self,
        *,
        spec: EngineeringSpec,
        preflight: PreflightResult,
        run_id: str,
        reason_code: str,
        result: AnalysisResult | None,
    ) -> ExecutionOutcome:
        verifications = _indeterminate_verifications(
            spec,
            reason_code=reason_code,
            evidence_ref=f"run:{run_id}:failed",
        )
        return ExecutionOutcome(
            preflight=preflight,
            run=AnalysisRunRecord(
                run_id=run_id,
                status=RunStatus.FAILED,
                analysis_result=result,
                error_code=reason_code,
            ),
            verifications=verifications,
            overall_verdict=Verdict.INDETERMINATE,
        )


class ContractViolation(RuntimeError):
    pass


def _plugin_coordinates(plugin: PhysicsPlugin) -> PluginCoordinates:
    return (
        plugin.plugin_id,
        plugin.plugin_version,
        plugin.schema_version,
    )


def _spec_plugin_identity(spec: EngineeringSpec) -> PluginIdentity:
    assert spec.model is not None
    return (
        spec.model.plugin_id,
        spec.model.plugin_version,
        spec.model.schema_version,
        spec.model.artifact_hash,
    )


def _require_plugin_registration(
    plugin: PhysicsPlugin, registration: PluginRegistration
) -> None:
    if _plugin_coordinates(plugin) != registration.coordinates:
        raise ContractViolation("plugin_identity_changed")
    source = inspect.getsourcefile(type(plugin))
    if source is None or Path(source).resolve() != registration.artifact_path.resolve():
        raise ContractViolation("plugin_artifact_mismatch")
    digest = "sha256:" + hashlib.sha256(Path(source).read_bytes()).hexdigest()
    if digest != registration.artifact_hash:
        raise ContractViolation("plugin_artifact_mismatch")


def _rejected_preflight(spec: EngineeringSpec, reason_code: str) -> PreflightResult:
    return PreflightResult(
        spec_id=spec.spec_id,
        spec_version=spec.spec_version,
        accepted=False,
        reason_codes=(reason_code,),
    )


def _preflight_reason(preflight: PreflightResult) -> str:
    reasons = (
        preflight.reason_codes
        + preflight.policy_violations
        + tuple(f"missing_input:{item}" for item in preflight.missing_inputs)
    )
    return reasons[0] if reasons else "preflight_rejected"


def _indeterminate_verifications(
    spec: EngineeringSpec,
    *,
    reason_code: str,
    evidence_ref: str,
    requirement_reasons: Mapping[str, tuple[str, ...]] | None = None,
) -> tuple[VerificationResult, ...]:
    reasons = dict(requirement_reasons or {})
    return tuple(
        VerificationResult(
            requirement_id=requirement.id,
            priority=requirement.priority,
            status=Verdict.INDETERMINATE,
            target=requirement.target,
            applied_tolerance=requirement.tolerance,
            evidence_refs=(evidence_ref,),
            reason_code=(reasons.get(requirement.id) or (reason_code,))[0],
        )
        for requirement in spec.requirements
    )


def _validate_verification_coverage(
    spec: EngineeringSpec,
    analysis_result: AnalysisResult,
    results: tuple[VerificationResult, ...],
) -> None:
    expected = {requirement.id: requirement for requirement in spec.requirements}
    actual_ids = [result.requirement_id for result in results]
    if (
        len(results) != len(spec.requirements)
        or len(actual_ids) != len(set(actual_ids))
        or set(actual_ids) != set(expected)
    ):
        raise ContractViolation("invalid_verification_coverage")
    for result in results:
        requirement = expected[result.requirement_id]
        if (
            result.priority is not requirement.priority
            or result.target != requirement.target
            or result.applied_tolerance != requirement.tolerance
            or result.observed != analysis_result.outputs.get(requirement.metric)
        ):
            raise ContractViolation("invalid_verification_evidence")
        if result.status in (Verdict.PASS, Verdict.FAIL):
            expected_status, expected_margin = _derive_verdict(
                requirement, result.observed
            )
            if result.status is not expected_status or result.margin != expected_margin:
                raise ContractViolation("invalid_verification_evidence")


def _derive_verdict(
    requirement: Requirement, observed: Quantity | None
) -> tuple[Verdict, float]:
    if observed is None:
        raise ContractViolation("invalid_verification_evidence")
    if (
        observed.dimension != requirement.target.dimension
        or observed.unit != requirement.target.unit
    ):
        raise ContractViolation("incompatible_verification_units")
    tolerance = (
        requirement.tolerance.absolute if requirement.tolerance is not None else 0.0
    )
    target = requirement.target.value
    value = observed.value
    comparisons = {
        "<": (value + tolerance < target, target - value - tolerance),
        "<=": (value + tolerance <= target, target - value - tolerance),
        "==": (
            abs(value - target) <= tolerance,
            tolerance - abs(value - target),
        ),
        ">=": (value - tolerance >= target, value - target - tolerance),
        ">": (value - tolerance > target, value - target - tolerance),
    }
    passed, margin = comparisons[requirement.operator]
    return (Verdict.PASS if passed else Verdict.FAIL, margin)
