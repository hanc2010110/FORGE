from __future__ import annotations

import itertools
import unittest

from forge_core.models import (
    Quantity,
    RequirementPriority,
    SourceRef,
    Verdict,
    VerificationResult,
)
from forge_core.verification import aggregate_verdict


def result(status: Verdict, *, required: bool = True) -> VerificationResult:
    observed = Quantity(
        value=1.0,
        unit="1",
        dimension="ratio",
        source=SourceRef(kind="user", identifier="test:observed"),
    )
    return VerificationResult(
        requirement_id=f"requirement-{status.value}-{required}",
        priority=(
            RequirementPriority.REQUIRED if required else RequirementPriority.OPTIONAL
        ),
        status=status,
        observed=observed if status is not Verdict.INDETERMINATE else None,
        target=observed if status is not Verdict.INDETERMINATE else None,
        margin=0.0 if status is not Verdict.INDETERMINATE else None,
        evidence_refs=("evidence:1",),
        reason_code="test_indeterminate" if status is Verdict.INDETERMINATE else None,
    )


class AggregateVerdictTests(unittest.TestCase):
    def test_required_fail_dominates_every_combination(self) -> None:
        for other in Verdict:
            with self.subTest(other=other):
                self.assertEqual(
                    aggregate_verdict((result(Verdict.FAIL), result(other))),
                    Verdict.FAIL,
                )

    def test_indeterminate_dominates_when_no_required_fail_exists(self) -> None:
        combinations = itertools.product(
            (Verdict.PASS, Verdict.INDETERMINATE), repeat=3
        )
        for statuses in combinations:
            expected = (
                Verdict.INDETERMINATE
                if Verdict.INDETERMINATE in statuses
                else Verdict.PASS
            )
            with self.subTest(statuses=statuses):
                self.assertEqual(
                    aggregate_verdict(tuple(result(status) for status in statuses)),
                    expected,
                )

    def test_optional_results_do_not_change_overall_verdict(self) -> None:
        required_pass = result(Verdict.PASS)
        for optional_status in Verdict:
            with self.subTest(optional_status=optional_status):
                self.assertEqual(
                    aggregate_verdict(
                        (required_pass, result(optional_status, required=False))
                    ),
                    Verdict.PASS,
                )

    def test_no_required_requirements_is_indeterminate(self) -> None:
        self.assertEqual(
            aggregate_verdict((result(Verdict.PASS, required=False),)),
            Verdict.INDETERMINATE,
        )


if __name__ == "__main__":
    unittest.main()
