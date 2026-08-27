from __future__ import annotations

from collections.abc import Iterable

from forge_core.models import RequirementPriority, Verdict, VerificationResult


def aggregate_verdict(results: Iterable[VerificationResult]) -> Verdict:
    required = tuple(
        result for result in results if result.priority is RequirementPriority.REQUIRED
    )
    if not required:
        return Verdict.INDETERMINATE
    if any(result.status is Verdict.FAIL for result in required):
        return Verdict.FAIL
    if any(result.status is Verdict.INDETERMINATE for result in required):
        return Verdict.INDETERMINATE
    return Verdict.PASS
