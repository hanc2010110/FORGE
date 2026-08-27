from __future__ import annotations

from collections.abc import Callable

from forge_core.models import (
    AnalysisResult,
    EngineeringSpec,
    PreflightResult,
    Quantity,
    SourceRef,
    SpecStatus,
    Verdict,
    VerificationResult,
)


class FakePhysicsPlugin:
    """Deterministic contract plugin. It is intentionally not a physics model."""

    plugin_id = "fake.double"
    plugin_version = "1.0.0"
    schema_version = "1.0.0"
    formula_hash = (
        "sha256:7995956c3038d217d6fd42edb4e090c39db88fd62c7087c7e1f3d32bf5f630d7"
    )
    required_parameter = "input_value"
    supported_metric = "fake_score"

    def preflight(self, spec: EngineeringSpec) -> PreflightResult:
        reasons: list[str] = []
        missing_inputs: tuple[str, ...] = ()
        requirement_reasons: dict[str, tuple[str, ...]] = {}
        if spec.status is not SpecStatus.APPROVED:
            reasons.append("spec_not_approved")
        if self.required_parameter not in spec.parameters:
            missing_inputs = (self.required_parameter,)
        if spec.unknowns:
            reasons.append("unresolved_unknowns")
        if not spec.requirements:
            reasons.append("missing_requirements")
        for requirement in spec.requirements:
            if requirement.metric != self.supported_metric:
                requirement_reasons[requirement.id] = ("unsupported_metric",)
        return PreflightResult(
            spec_id=spec.spec_id,
            spec_version=spec.spec_version,
            accepted=not reasons and not missing_inputs and not requirement_reasons,
            reason_codes=tuple(reasons),
            missing_inputs=missing_inputs,
            requirement_reasons=requirement_reasons,
        )

    def run(self, spec: EngineeringSpec) -> AnalysisResult:
        input_value = spec.parameters[self.required_parameter]
        score = Quantity(
            value=input_value.value * 2.0,
            unit=input_value.unit,
            dimension=input_value.dimension,
            source=SourceRef(
                kind="formula",
                identifier="fake.double",
                version=self.plugin_version,
                hash=self.formula_hash,
            ),
        )
        return AnalysisResult(
            outputs={self.supported_metric: score},
            intermediates={"multiplier": 2.0},
            formula_id="fake.double",
            formula_version=self.plugin_version,
        )

    def verify(
        self, spec: EngineeringSpec, result: AnalysisResult
    ) -> tuple[VerificationResult, ...]:
        verifications: list[VerificationResult] = []
        for requirement in spec.requirements:
            observed = result.outputs.get(requirement.metric)
            if observed is None:
                verifications.append(
                    VerificationResult(
                        requirement_id=requirement.id,
                        priority=requirement.priority,
                        status=Verdict.INDETERMINATE,
                        target=requirement.target,
                        applied_tolerance=requirement.tolerance,
                        evidence_refs=("analysis:missing_output",),
                        reason_code="missing_output",
                    )
                )
                continue
            if (
                observed.dimension != requirement.target.dimension
                or observed.unit != requirement.target.unit
            ):
                verifications.append(
                    VerificationResult(
                        requirement_id=requirement.id,
                        priority=requirement.priority,
                        status=Verdict.INDETERMINATE,
                        observed=observed,
                        target=requirement.target,
                        applied_tolerance=requirement.tolerance,
                        evidence_refs=(f"analysis:{requirement.metric}",),
                        reason_code="unit_or_dimension_mismatch",
                    )
                )
                continue

            tolerance = (
                requirement.tolerance.absolute
                if requirement.tolerance is not None
                else 0.0
            )
            comparator, margin = _comparison(requirement.operator, tolerance)
            target = requirement.target.value
            passed = comparator(observed.value, target)
            verifications.append(
                VerificationResult(
                    requirement_id=requirement.id,
                    priority=requirement.priority,
                    status=Verdict.PASS if passed else Verdict.FAIL,
                    observed=observed,
                    target=requirement.target,
                    applied_tolerance=requirement.tolerance,
                    margin=margin(observed.value, target),
                    evidence_refs=(
                        f"analysis:{requirement.metric}",
                        f"formula:{result.formula_id}:{result.formula_version}",
                    ),
                )
            )
        return tuple(verifications)


def _comparison(
    operator: str, tolerance: float
) -> tuple[Callable[[float, float], bool], Callable[[float, float], float]]:
    comparisons: dict[
        str, tuple[Callable[[float, float], bool], Callable[[float, float], float]]
    ] = {
        "<": (
            lambda observed, target: observed + tolerance < target,
            lambda observed, target: target - observed - tolerance,
        ),
        "<=": (
            lambda observed, target: observed + tolerance <= target,
            lambda observed, target: target - observed - tolerance,
        ),
        "==": (
            lambda observed, target: abs(observed - target) <= tolerance,
            lambda observed, target: tolerance - abs(observed - target),
        ),
        ">=": (
            lambda observed, target: observed - tolerance >= target,
            lambda observed, target: observed - target - tolerance,
        ),
        ">": (
            lambda observed, target: observed - tolerance > target,
            lambda observed, target: observed - target - tolerance,
        ),
    }
    return comparisons[operator]
