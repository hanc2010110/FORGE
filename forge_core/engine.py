from __future__ import annotations

import hashlib
import inspect
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from forge_core.design import SystemDesignRevision, revision_hash
from forge_core.hashing import canonical_sha256
from forge_core.models import (
    AnalysisResult,
    AnalysisRunRecord,
    ApprovalRef,
    ApprovalSubjectKind,
    EngineeringSpec,
    ExecutionOutcome,
    PluginRef,
    PreflightResult,
    PreparedAnalysis,
    PreparedAnalysisBinding,
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
        """Backward-compatible one-shot wrapper for the deterministic kernel demo."""

        approval_refs: tuple[ApprovalRef, ...] = ()
        try:
            validated_spec = EngineeringSpec.model_validate(
                spec.model_dump(mode="python")
            )
        except ValidationError:
            validated_spec = None
        if (
            validated_spec is not None
            and validated_spec.status is SpecStatus.APPROVED
            and validated_spec.approved_at is not None
        ):
            approval_refs = (
                ApprovalRef(
                    approval_id=(
                        f"legacy-execute:{validated_spec.spec_id}:"
                        f"{validated_spec.spec_version}"
                    ),
                    subject_kind=ApprovalSubjectKind.ENGINEERING_SPEC,
                    project_id=validated_spec.project_id,
                    subject_id=validated_spec.spec_id,
                    subject_version=validated_spec.spec_version,
                    subject_hash=canonical_sha256(validated_spec),
                    approved_by="legacy-execute-wrapper",
                    approved_at=validated_spec.approved_at,
                ),
            )
        prepared = self.prepare(spec, plugin, approval_refs=approval_refs)
        if not prepared.binding.preflight.accepted:
            return _outcome_for_rejected_preflight(
                spec,
                prepared.binding.preflight,
                evidence_ref=f"preflight:{spec.spec_id}:{spec.spec_version}",
            )
        return self.execute_prepared(prepared, spec, plugin)

    def prepare(
        self,
        spec: EngineeringSpec,
        plugin: PhysicsPlugin,
        *,
        approval_refs: Iterable[ApprovalRef],
        revision: SystemDesignRevision | None = None,
        preparation_id: str | None = None,
    ) -> PreparedAnalysis:
        """Run preflight without creating a run or generating numeric results."""

        try:
            spec = EngineeringSpec.model_validate(spec.model_dump(mode="python"))
            spec_contract_valid = True
        except ValidationError:
            spec_contract_valid = False

        engine_version = self.engine_version
        policy_version = self.policy_version
        plugin_coordinates = _plugin_coordinates(plugin)
        registration = self.plugin_registry.get(plugin_coordinates)
        spec_hash = canonical_sha256(spec)
        preflight = (
            self._preflight(spec, plugin, registration, spec_hash)
            if spec_contract_valid
            else _rejected_preflight(spec, "invalid_spec_contract")
        )
        if (
            self.engine_version != engine_version
            or self.policy_version != policy_version
        ):
            preflight = _rejected_preflight(spec, "engine_policy_identity_changed")

        revision_id: str | None = None
        current_revision_hash: str | None = None
        revision_number: int | None = None
        if revision is not None:
            try:
                revision = SystemDesignRevision.model_validate(
                    revision.model_dump(mode="python")
                )
            except ValidationError:
                preflight = _rejected_preflight(spec, "invalid_revision_contract")
            else:
                revision_id = revision.revision_id
                current_revision_hash = revision_hash(revision)
                revision_number = revision.revision_number
                if revision.project_id != spec.project_id:
                    preflight = _rejected_preflight(spec, "revision_project_mismatch")

        try:
            approvals = tuple(
                ApprovalRef.model_validate(item.model_dump(mode="python"))
                for item in approval_refs
            )
            _require_unique_approvals(approvals)
        except AttributeError, ValidationError, ContractViolation:
            approvals = ()
            if preflight.accepted:
                preflight = _rejected_preflight(spec, "invalid_approval_contract")

        if preflight.accepted:
            approval_reason = _approval_rejection_reason(
                spec=spec,
                spec_hash=spec_hash,
                revision_id=revision_id,
                revision_number=revision_number,
                revision_hash_value=current_revision_hash,
                approvals=approvals,
            )
            if approval_reason is not None:
                preflight = _rejected_preflight(spec, approval_reason)

        if not preflight.accepted and any(
            approval.project_id != spec.project_id for approval in approvals
        ):
            approvals = ()

        binding = PreparedAnalysisBinding(
            project_id=spec.project_id,
            spec_id=spec.spec_id,
            spec_version=spec.spec_version,
            spec_hash=spec_hash,
            revision_id=revision_id,
            revision_hash=current_revision_hash,
            revision_number=revision_number,
            engine_version=engine_version,
            policy_version=policy_version,
            plugin=_plugin_ref(plugin, registration),
            input_hash=spec_hash,
            expected_requirement_ids=tuple(
                sorted({requirement.id for requirement in spec.requirements})
            ),
            expected_metrics=tuple(
                sorted({requirement.metric for requirement in spec.requirements})
            ),
            approval_refs=approvals,
            preflight=preflight,
        )
        return PreparedAnalysis(
            preparation_id=preparation_id or f"preparation-{uuid4()}",
            binding=binding,
            prepare_hash=canonical_sha256(binding),
        )

    def execute_prepared(
        self,
        prepared: PreparedAnalysis,
        spec: EngineeringSpec,
        plugin: PhysicsPlugin,
        *,
        revision: SystemDesignRevision | None = None,
        run_id: str | None = None,
    ) -> ExecutionOutcome:
        """Execute only when the persisted preparation still matches current state."""

        try:
            spec = EngineeringSpec.model_validate(spec.model_dump(mode="python"))
        except ValidationError:
            return self._rejected_outcome(spec, "invalid_spec_contract")
        try:
            prepared = PreparedAnalysis.model_validate(
                prepared.model_dump(mode="python")
            )
        except AttributeError, ValidationError:
            return self._rejected_outcome(spec, "prepared_contract_invalid")

        binding = prepared.binding
        if not binding.preflight.accepted:
            return _outcome_for_rejected_preflight(
                spec,
                binding.preflight,
                evidence_ref=f"preparation:{prepared.preparation_id}",
            )
        if (
            binding.engine_version != self.engine_version
            or binding.policy_version != self.policy_version
        ):
            return self._rejected_outcome(spec, "prepared_engine_policy_mismatch")
        if (
            binding.project_id != spec.project_id
            or binding.spec_id != spec.spec_id
            or binding.spec_version != spec.spec_version
            or binding.spec_hash != canonical_sha256(spec)
            or binding.input_hash != canonical_sha256(spec)
        ):
            return self._rejected_outcome(spec, "prepared_spec_mismatch")
        if binding.expected_requirement_ids != tuple(
            sorted({requirement.id for requirement in spec.requirements})
        ) or binding.expected_metrics != tuple(
            sorted({requirement.metric for requirement in spec.requirements})
        ):
            return self._rejected_outcome(spec, "prepared_expectations_mismatch")

        registration = self.plugin_registry.get(_plugin_coordinates(plugin))
        if registration is None or binding.plugin != _plugin_ref(plugin, registration):
            return self._rejected_outcome(spec, "prepared_plugin_mismatch")
        try:
            _require_plugin_registration(plugin, registration)
        except ContractViolation as exc:
            return self._rejected_outcome(spec, str(exc))

        if revision is None:
            revision_identity: tuple[str | None, int | None, str | None] = (
                None,
                None,
                None,
            )
        else:
            try:
                revision = SystemDesignRevision.model_validate(
                    revision.model_dump(mode="python")
                )
            except ValidationError:
                return self._rejected_outcome(spec, "invalid_revision_contract")
            revision_identity = (
                revision.revision_id,
                revision.revision_number,
                revision_hash(revision),
            )
        if revision_identity != (
            binding.revision_id,
            binding.revision_number,
            binding.revision_hash,
        ):
            return self._rejected_outcome(spec, "prepared_revision_mismatch")

        return self._execute_accepted(
            spec=spec,
            preflight=binding.preflight,
            registration=registration,
            plugin=plugin,
            spec_hash_before=binding.spec_hash,
            input_hash=binding.input_hash,
            engine_version=binding.engine_version,
            policy_version=binding.policy_version,
            run_id=run_id or f"run-{uuid4()}",
        )

    def _execute_accepted(
        self,
        *,
        spec: EngineeringSpec,
        preflight: PreflightResult,
        registration: PluginRegistration,
        plugin: PhysicsPlugin,
        spec_hash_before: str,
        input_hash: str,
        engine_version: str,
        policy_version: str,
        run_id: str,
    ) -> ExecutionOutcome:
        result: AnalysisResult | None = None

        try:
            plugin_result = plugin.run(spec)
            self._require_engine_policy_identity(engine_version, policy_version)
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
            self._require_engine_policy_identity(engine_version, policy_version)
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
            engine_version=engine_version,
            policy_version=policy_version,
            input_hash=input_hash,
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

    def _rejected_outcome(
        self, spec: EngineeringSpec, reason_code: str
    ) -> ExecutionOutcome:
        return _outcome_for_rejected_preflight(
            spec,
            _rejected_preflight(spec, reason_code),
            evidence_ref=f"preflight:{spec.spec_id}:{spec.spec_version}",
        )

    def _require_engine_policy_identity(
        self,
        engine_version: str,
        policy_version: str,
    ) -> None:
        if (
            self.engine_version != engine_version
            or self.policy_version != policy_version
        ):
            raise ContractViolation("engine_policy_identity_changed")

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


def _plugin_ref(
    plugin: PhysicsPlugin,
    registration: PluginRegistration | None,
) -> PluginRef:
    if registration is not None:
        artifact_hash = registration.artifact_hash
    else:
        source = inspect.getsourcefile(type(plugin))
        payload = (
            Path(source).read_bytes()
            if source is not None
            else ":".join(_plugin_coordinates(plugin)).encode("utf-8")
        )
        artifact_hash = "sha256:" + hashlib.sha256(payload).hexdigest()
    return PluginRef(
        plugin_id=plugin.plugin_id,
        plugin_version=plugin.plugin_version,
        schema_version=plugin.schema_version,
        artifact_hash=artifact_hash,
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


def _outcome_for_rejected_preflight(
    spec: EngineeringSpec,
    preflight: PreflightResult,
    *,
    evidence_ref: str,
) -> ExecutionOutcome:
    verifications = _indeterminate_verifications(
        spec,
        reason_code=_preflight_reason(preflight),
        evidence_ref=evidence_ref,
        requirement_reasons=preflight.requirement_reasons,
    )
    return ExecutionOutcome(
        preflight=preflight,
        run=None,
        verifications=verifications,
        overall_verdict=Verdict.INDETERMINATE,
    )


def _require_unique_approvals(approvals: tuple[ApprovalRef, ...]) -> None:
    approval_ids = [approval.approval_id for approval in approvals]
    approval_subjects = [
        (approval.subject_kind, approval.subject_id, approval.subject_version)
        for approval in approvals
    ]
    if len(approval_ids) != len(set(approval_ids)) or len(approval_subjects) != len(
        set(approval_subjects)
    ):
        raise ContractViolation("invalid_approval_contract")


def _approval_rejection_reason(
    *,
    spec: EngineeringSpec,
    spec_hash: str,
    revision_id: str | None,
    revision_number: int | None,
    revision_hash_value: str | None,
    approvals: tuple[ApprovalRef, ...],
) -> str | None:
    spec_approval = next(
        (
            approval
            for approval in approvals
            if approval.subject_kind is ApprovalSubjectKind.ENGINEERING_SPEC
        ),
        None,
    )
    if spec_approval is None:
        return "required_spec_approval_missing"
    if (
        spec_approval.project_id != spec.project_id
        or spec_approval.subject_id != spec.spec_id
        or spec_approval.subject_version != spec.spec_version
        or spec_approval.subject_hash != spec_hash
    ):
        return "required_spec_approval_stale"
    if revision_id is None:
        return None
    revision_approval = next(
        (
            approval
            for approval in approvals
            if approval.subject_kind is ApprovalSubjectKind.SYSTEM_DESIGN_REVISION
        ),
        None,
    )
    if revision_approval is None:
        return "required_revision_approval_missing"
    if (
        revision_approval.project_id != spec.project_id
        or revision_approval.subject_id != revision_id
        or revision_approval.subject_version != revision_number
        or revision_approval.subject_hash != revision_hash_value
    ):
        return "required_revision_approval_stale"
    return None


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
