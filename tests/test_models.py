from __future__ import annotations

import math
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from forge_core.hashing import canonical_sha256
from forge_core.models import (
    AnalysisResult,
    AnalysisRunRecord,
    ApprovalRef,
    ApprovalSubjectKind,
    EngineeringSpec,
    PluginRef,
    PreflightResult,
    PreparedAnalysis,
    PreparedAnalysisBinding,
    Quantity,
    Requirement,
    RequirementPriority,
    RunLifecycleStatus,
    RunStateEvent,
    RunStatus,
    SourceRef,
    SpecStatus,
    Uncertainty,
    Verdict,
    VerificationResult,
)


def user_quantity(
    value: float, *, unit: str = "1", dimension: str = "ratio"
) -> Quantity:
    return Quantity(
        value=value,
        unit=unit,
        dimension=dimension,
        source=SourceRef(kind="user", identifier="test:user"),
    )


class ContractModelTests(unittest.TestCase):
    def test_non_user_sources_require_version_and_hash(self) -> None:
        with self.assertRaises(ValidationError):
            SourceRef(kind="dataset", identifier="materials.aluminum")

    def test_source_hash_must_be_canonical_sha256(self) -> None:
        with self.assertRaises(ValidationError):
            SourceRef(
                kind="dataset",
                identifier="materials.aluminum",
                version="1.0.0",
                hash="not-a-content-hash",
            )

    def test_non_finite_quantities_are_rejected(self) -> None:
        for value in (math.inf, -math.inf, math.nan):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                user_quantity(value)

        with self.assertRaises(ValidationError):
            Uncertainty(absolute=math.inf, unit="1")

    def test_tolerance_unit_must_match_requirement_target(self) -> None:
        with self.assertRaises(ValidationError):
            Requirement(
                id="r1",
                metric="score",
                operator=">=",
                target=user_quantity(1.0, unit="mm", dimension="length"),
                priority=RequirementPriority.REQUIRED,
                tolerance=Uncertainty(absolute=0.1, unit="m"),
            )

    def test_unknown_fields_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            SourceRef.model_validate(
                {
                    "kind": "user",
                    "identifier": "test:user",
                    "unexpected": True,
                }
            )

    def test_spec_round_trip_preserves_contract(self) -> None:
        spec = EngineeringSpec(
            project_id="project-1",
            spec_id="spec-1",
            spec_version=1,
            status=SpecStatus.DRAFT,
            intent="deterministic fake analysis",
            parameters={"input_value": user_quantity(2.0)},
            requirements=(
                Requirement(
                    id="minimum_score",
                    metric="fake_score",
                    operator=">=",
                    target=user_quantity(4.0),
                    priority=RequirementPriority.REQUIRED,
                ),
            ),
        )

        restored = EngineeringSpec.model_validate_json(spec.model_dump_json())

        self.assertEqual(restored, spec)
        self.assertEqual(restored.schema_version, "1.0.0")

    def test_prd_fan_bracket_example_validates(self) -> None:
        example = Path("examples/fan_bracket_spec.json").read_text(encoding="utf-8")

        spec = EngineeringSpec.model_validate_json(example)

        self.assertEqual(spec.project_id, "local-uuid")
        self.assertEqual(spec.status, SpecStatus.APPROVED)

    def test_canonical_json_embedded_in_prd_validates(self) -> None:
        prd = Path("PRD.md").read_text(encoding="utf-8")
        canonical_json = prd.split("```json", maxsplit=1)[1].split("```", maxsplit=1)[0]

        spec = EngineeringSpec.model_validate_json(canonical_json)

        self.assertEqual(spec.spec_id, "spec-uuid")

    def test_approved_spec_requires_approval_timestamp(self) -> None:
        with self.assertRaises(ValidationError):
            EngineeringSpec(
                project_id="project-1",
                spec_id="spec-1",
                spec_version=1,
                status=SpecStatus.APPROVED,
                intent="invalid approved spec",
                parameters={},
                requirements=(),
            )

        with self.assertRaises(ValidationError):
            EngineeringSpec(
                project_id="project-1",
                spec_id="spec-1",
                spec_version=1,
                status=SpecStatus.DRAFT,
                intent="invalid draft spec",
                parameters={},
                requirements=(),
                approved_at=datetime(2026, 8, 27, tzinfo=UTC),
            )

        with self.assertRaises(ValidationError):
            EngineeringSpec(
                project_id="project-1",
                spec_id="spec-1",
                spec_version=1,
                status=SpecStatus.APPROVED,
                intent="missing plugin binding",
                parameters={},
                requirements=(),
                approved_at=datetime(2026, 8, 27, tzinfo=UTC),
            )

    def test_plugin_artifact_hash_is_canonical(self) -> None:
        with self.assertRaises(ValidationError):
            PluginRef(
                plugin_id="test",
                plugin_version="1",
                schema_version="1",
                artifact_hash="not-a-hash",
            )

    def test_approved_spec_parameters_are_deeply_immutable(self) -> None:
        example = Path("examples/fan_bracket_spec.json").read_text(encoding="utf-8")
        spec = EngineeringSpec.model_validate_json(example)

        with self.assertRaises(TypeError):
            cast(Any, spec.parameters)["length"] = user_quantity(1.0)

    def test_pass_verification_requires_observed_target_margin_and_evidence(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            VerificationResult(
                requirement_id="r1",
                priority=RequirementPriority.REQUIRED,
                status=Verdict.PASS,
                evidence_refs=(),
            )

        with self.assertRaises(ValidationError):
            VerificationResult(
                requirement_id="r1",
                priority=RequirementPriority.REQUIRED,
                status=Verdict.INDETERMINATE,
                evidence_refs=("evidence:1",),
            )

        with self.assertRaises(ValidationError):
            VerificationResult(
                requirement_id="r1",
                priority=RequirementPriority.REQUIRED,
                status=Verdict.FAIL,
                observed=user_quantity(1.0),
                target=user_quantity(2.0),
                margin=math.inf,
                evidence_refs=("evidence:1",),
            )

    def test_preflight_evidence_shape_is_enforced(self) -> None:
        with self.assertRaises(ValidationError):
            PreflightResult(
                spec_id="spec-1",
                spec_version=1,
                accepted=True,
                reason_codes=("unexpected",),
            )

        with self.assertRaises(ValidationError):
            PreflightResult(spec_id="spec-1", spec_version=1, accepted=False)

    def test_analysis_intermediates_must_be_finite(self) -> None:
        with self.assertRaises(ValidationError):
            AnalysisResult(
                outputs={},
                intermediates={"bad": math.nan},
                formula_id="test",
                formula_version="1",
            )

    def test_run_record_shape_is_enforced(self) -> None:
        with self.assertRaises(ValidationError):
            AnalysisRunRecord(run_id="run-1", status=RunStatus.SUCCEEDED)

        with self.assertRaises(ValidationError):
            AnalysisRunRecord(run_id="run-2", status=RunStatus.FAILED)

    def test_approval_reference_binds_an_immutable_subject(self) -> None:
        approval = ApprovalRef(
            approval_id="approval-spec-1",
            subject_kind=ApprovalSubjectKind.ENGINEERING_SPEC,
            project_id="project-1",
            subject_id="spec-1",
            subject_version=1,
            subject_hash="sha256:" + "1" * 64,
            approved_by="local-user",
            approved_at=datetime(2026, 8, 27, tzinfo=UTC),
        )
        restored = ApprovalRef.model_validate_json(approval.model_dump_json())

        self.assertEqual(restored, approval)
        with self.assertRaises(ValidationError):
            ApprovalRef.model_validate(
                approval.model_dump() | {"approved_at": datetime(2026, 8, 27)},
            )

    def test_prepared_analysis_hash_binds_preflight_and_revision(self) -> None:
        approval = ApprovalRef(
            approval_id="approval-spec-1",
            subject_kind=ApprovalSubjectKind.ENGINEERING_SPEC,
            project_id="project-1",
            subject_id="spec-1",
            subject_version=1,
            subject_hash="sha256:" + "1" * 64,
            approved_by="local-user",
            approved_at=datetime(2026, 8, 27, tzinfo=UTC),
        )
        revision_approval = ApprovalRef(
            approval_id="approval-revision-1",
            subject_kind=ApprovalSubjectKind.SYSTEM_DESIGN_REVISION,
            project_id="project-1",
            subject_id="revision-1",
            subject_version=1,
            subject_hash="sha256:" + "2" * 64,
            approved_by="local-user",
            approved_at=datetime(2026, 8, 27, tzinfo=UTC),
        )
        binding = PreparedAnalysisBinding(
            project_id="project-1",
            spec_id="spec-1",
            spec_version=1,
            spec_hash="sha256:" + "1" * 64,
            revision_id="revision-1",
            revision_hash="sha256:" + "2" * 64,
            revision_number=1,
            engine_version="engine-1",
            policy_version="policy-1",
            plugin=PluginRef(
                plugin_id="fake",
                plugin_version="1.0.0",
                schema_version="1.0.0",
                artifact_hash="sha256:" + "3" * 64,
            ),
            input_hash="sha256:" + "1" * 64,
            expected_requirement_ids=("minimum-score",),
            expected_metrics=("fake_score",),
            approval_refs=(approval, revision_approval),
            preflight=PreflightResult(
                spec_id="spec-1",
                spec_version=1,
                accepted=True,
            ),
        )
        prepared = PreparedAnalysis(
            preparation_id="preparation-1",
            binding=binding,
            prepare_hash=canonical_sha256(binding),
        )

        self.assertEqual(prepared.binding.revision_id, "revision-1")
        with self.assertRaises(ValidationError):
            PreparedAnalysis(
                preparation_id="preparation-1",
                binding=binding.model_copy(update={"policy_version": "policy-2"}),
                prepare_hash=prepared.prepare_hash,
            )

    def test_prepared_analysis_rejects_partial_or_inconsistent_binding(self) -> None:
        spec_approval = ApprovalRef(
            approval_id="approval-spec-1",
            subject_kind=ApprovalSubjectKind.ENGINEERING_SPEC,
            project_id="project-1",
            subject_id="spec-1",
            subject_version=1,
            subject_hash="sha256:" + "1" * 64,
            approved_by="local-user",
            approved_at=datetime(2026, 8, 27, tzinfo=UTC),
        )
        base = {
            "project_id": "project-1",
            "spec_id": "spec-1",
            "spec_version": 1,
            "spec_hash": "sha256:" + "1" * 64,
            "engine_version": "engine-1",
            "policy_version": "policy-1",
            "plugin": PluginRef(
                plugin_id="fake",
                plugin_version="1.0.0",
                schema_version="1.0.0",
                artifact_hash="sha256:" + "3" * 64,
            ),
            "input_hash": "sha256:" + "1" * 64,
            "expected_requirement_ids": ("r1",),
            "expected_metrics": ("score",),
            "approval_refs": (spec_approval,),
            "preflight": PreflightResult(
                spec_id="spec-1",
                spec_version=1,
                accepted=True,
            ),
        }

        with self.assertRaises(ValidationError):
            PreparedAnalysisBinding.model_validate(base | {"revision_id": "revision-1"})
        with self.assertRaises(ValidationError):
            PreparedAnalysisBinding.model_validate(base | {"approval_refs": ()})
        with self.assertRaises(ValidationError):
            PreparedAnalysisBinding.model_validate(
                base | {"expected_requirement_ids": ("r1", "r1")}
            )
        with self.assertRaises(ValidationError):
            PreparedAnalysisBinding.model_validate(
                base
                | {
                    "preflight": PreflightResult(
                        spec_id="other-spec",
                        spec_version=1,
                        accepted=True,
                    )
                }
            )

    def test_run_state_event_enforces_append_only_transition_shape(self) -> None:
        prepared = RunStateEvent(
            event_id="event-1",
            project_id="project-1",
            run_id="run-1",
            preparation_id="preparation-1",
            prepare_hash="sha256:" + "1" * 64,
            sequence=1,
            previous_status=None,
            status=RunLifecycleStatus.PREPARED,
            actor="local-user",
            reason_code="preflight_accepted",
            occurred_at=datetime(2026, 8, 27, tzinfo=UTC),
        )
        running = RunStateEvent(
            event_id="event-3",
            project_id="project-1",
            run_id="run-1",
            preparation_id="preparation-1",
            prepare_hash="sha256:" + "1" * 64,
            sequence=3,
            previous_status=RunLifecycleStatus.QUEUED,
            status=RunLifecycleStatus.RUNNING,
            actor="local-user",
            reason_code="execution_started",
            occurred_at=datetime(2026, 8, 27, tzinfo=UTC),
        )

        self.assertEqual(prepared.status, RunLifecycleStatus.PREPARED)
        self.assertEqual(running.previous_status, RunLifecycleStatus.QUEUED)
        for sequence, previous in (
            (3, RunLifecycleStatus.QUEUED),
            (4, RunLifecycleStatus.RUNNING),
        ):
            with self.subTest(cancel_from=previous):
                cancelled = RunStateEvent(
                    event_id=f"event-cancel-{sequence}",
                    project_id="project-1",
                    run_id="run-1",
                    preparation_id="preparation-1",
                    prepare_hash="sha256:" + "1" * 64,
                    sequence=sequence,
                    previous_status=previous,
                    status=RunLifecycleStatus.CANCELLED,
                    actor="local-user",
                    reason_code="cancelled",
                    occurred_at=datetime(2026, 8, 27, tzinfo=UTC),
                )
                self.assertEqual(cancelled.status, RunLifecycleStatus.CANCELLED)
        with self.assertRaises(ValidationError):
            RunStateEvent(
                event_id="event-invalid",
                project_id="project-1",
                run_id="run-1",
                preparation_id="preparation-1",
                prepare_hash="sha256:" + "1" * 64,
                sequence=2,
                previous_status=RunLifecycleStatus.SUCCEEDED,
                status=RunLifecycleStatus.RUNNING,
                actor="local-user",
                reason_code="invalid_transition",
                occurred_at=datetime(2026, 8, 27, tzinfo=UTC),
            )


if __name__ == "__main__":
    unittest.main()
