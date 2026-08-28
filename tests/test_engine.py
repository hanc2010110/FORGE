from __future__ import annotations

import hashlib
import inspect
import unittest
from datetime import UTC, datetime
from pathlib import Path

from forge_core.design import (
    ArtifactKind,
    RevisionArtifact,
    SystemDesignRevision,
    revision_hash,
)
from forge_core.engine import AnalysisEngine, PluginRegistration
from forge_core.hashing import canonical_sha256
from forge_core.models import (
    AnalysisResult,
    ApprovalRef,
    ApprovalSubjectKind,
    EngineeringSpec,
    PluginRef,
    PreflightResult,
    PreparedAnalysis,
    Quantity,
    Requirement,
    RequirementPriority,
    RunStatus,
    SourceRef,
    SpecStatus,
    Uncertainty,
    Verdict,
    VerificationResult,
)
from plugins.fake import FakePhysicsPlugin

FAKE_PLUGIN_ARTIFACT_HASH = (
    "sha256:6276d94a0dba2fd8c46f43cd809fd47be80eef8eb62a2318b086b69b3b269cda"
)


def fake_registration() -> PluginRegistration:
    return PluginRegistration(
        plugin_id=FakePhysicsPlugin.plugin_id,
        plugin_version=FakePhysicsPlugin.plugin_version,
        schema_version=FakePhysicsPlugin.schema_version,
        artifact_hash=FAKE_PLUGIN_ARTIFACT_HASH,
        artifact_path=Path("plugins/fake.py"),
    )


def registration_for(plugin: FakePhysicsPlugin) -> PluginRegistration:
    source = inspect.getsourcefile(type(plugin))
    assert source is not None
    artifact_path = Path(source)
    return PluginRegistration(
        plugin_id=plugin.plugin_id,
        plugin_version=plugin.plugin_version,
        schema_version=plugin.schema_version,
        artifact_hash="sha256:"
        + hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        artifact_path=artifact_path,
    )


def engine_for(plugin: FakePhysicsPlugin) -> tuple[AnalysisEngine, PluginRegistration]:
    registration = registration_for(plugin)
    return (
        AnalysisEngine(
            engine_version="0.1.0",
            policy_version="1.0.0",
            allowed_plugins={registration},
        ),
        registration,
    )


def quantity(value: float) -> Quantity:
    return Quantity(
        value=value,
        unit="1",
        dimension="ratio",
        source=SourceRef(kind="user", identifier="test:user"),
    )


def make_spec(
    *,
    status: SpecStatus = SpecStatus.APPROVED,
    include_input: bool = True,
    target: float = 4.0,
    tolerance: float | None = None,
    unknowns: tuple[str, ...] = (),
    registration: PluginRegistration | None = None,
) -> EngineeringSpec:
    selected = registration or fake_registration()
    return EngineeringSpec(
        project_id="project-1",
        spec_id="spec-1",
        spec_version=1,
        status=status,
        intent="deterministic fake analysis",
        model=PluginRef(
            plugin_id=selected.plugin_id,
            plugin_version=selected.plugin_version,
            schema_version=selected.schema_version,
            artifact_hash=selected.artifact_hash,
        ),
        parameters={"input_value": quantity(2.0)} if include_input else {},
        requirements=(
            Requirement(
                id="minimum_score",
                metric="fake_score",
                operator=">=",
                target=quantity(target),
                priority=RequirementPriority.REQUIRED,
                tolerance=Uncertainty(absolute=tolerance, unit="1")
                if tolerance is not None
                else None,
            ),
        ),
        unknowns=unknowns,
        approved_at=datetime(2026, 8, 27, tzinfo=UTC)
        if status is SpecStatus.APPROVED
        else None,
    )


def approval_for(
    spec: EngineeringSpec,
    *,
    revision: SystemDesignRevision | None = None,
) -> tuple[ApprovalRef, ...]:
    assert spec.approved_at is not None
    approvals = [
        ApprovalRef(
            approval_id=f"approval:{spec.spec_id}:{spec.spec_version}",
            subject_kind=ApprovalSubjectKind.ENGINEERING_SPEC,
            project_id=spec.project_id,
            subject_id=spec.spec_id,
            subject_version=spec.spec_version,
            subject_hash=canonical_sha256(spec),
            approved_by="local-user",
            approved_at=spec.approved_at,
        )
    ]
    if revision is not None:
        approvals.append(
            ApprovalRef(
                approval_id=f"approval:{revision.revision_id}",
                subject_kind=ApprovalSubjectKind.SYSTEM_DESIGN_REVISION,
                project_id=revision.project_id,
                subject_id=revision.revision_id,
                subject_version=revision.revision_number,
                subject_hash=revision_hash(revision),
                approved_by="local-user",
                approved_at=revision.approved_at,
            )
        )
    return tuple(approvals)


def make_revision(number: int = 1) -> SystemDesignRevision:
    required = (
        ArtifactKind.SYSTEM_SPEC,
        ArtifactKind.INTERFACE,
        ArtifactKind.BOM,
        ArtifactKind.BUDGET_POLICY,
    )
    return SystemDesignRevision(
        project_id="project-1",
        revision_id=f"revision-{number}",
        revision_number=number,
        parent_revision_id="revision-1" if number > 1 else None,
        approved_at=datetime(2026, 8, 27, tzinfo=UTC),
        artifacts=tuple(
            RevisionArtifact(
                artifact_id=f"artifact:{item.value}",
                kind=item,
                version=str(number),
                content_hash=f"sha256:{number:064x}",
            )
            for item in required
        ),
    )


class AnalysisEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = FakePhysicsPlugin()
        self.engine = AnalysisEngine(
            engine_version="0.1.0",
            policy_version="1.0.0",
            allowed_plugins={fake_registration()},
        )

    def test_draft_spec_is_rejected_without_creating_a_run(self) -> None:
        outcome = self.engine.execute(make_spec(status=SpecStatus.DRAFT), self.plugin)

        self.assertFalse(outcome.preflight.accepted)
        self.assertIsNone(outcome.run)
        self.assertEqual(outcome.overall_verdict, Verdict.INDETERMINATE)
        self.assertIn("spec_not_approved", outcome.preflight.reason_codes)

    def test_engine_blocks_draft_even_if_plugin_preflight_is_permissive(self) -> None:
        class PermissivePlugin(FakePhysicsPlugin):
            def preflight(self, spec: EngineeringSpec) -> PreflightResult:
                return PreflightResult(
                    spec_id=spec.spec_id,
                    spec_version=spec.spec_version,
                    accepted=True,
                )

        outcome = self.engine.execute(
            make_spec(status=SpecStatus.DRAFT), PermissivePlugin()
        )

        self.assertFalse(outcome.preflight.accepted)
        self.assertIsNone(outcome.run)
        self.assertIn("spec_not_approved", outcome.preflight.reason_codes)

    def test_missing_input_is_rejected_without_creating_a_run(self) -> None:
        outcome = self.engine.execute(make_spec(include_input=False), self.plugin)

        self.assertFalse(outcome.preflight.accepted)
        self.assertIsNone(outcome.run)
        self.assertEqual(outcome.preflight.missing_inputs, ("input_value",))
        self.assertEqual(outcome.verifications[0].status, Verdict.INDETERMINATE)

    def test_unknowns_block_execution(self) -> None:
        outcome = self.engine.execute(
            make_spec(unknowns=("load direction is unknown",)), self.plugin
        )

        self.assertFalse(outcome.preflight.accepted)
        self.assertIsNone(outcome.run)
        self.assertIn("unresolved_unknowns", outcome.preflight.reason_codes)

    def test_fake_plugin_passes_requirement_deterministically(self) -> None:
        first = self.engine.execute(make_spec(), self.plugin)
        second = self.engine.execute(make_spec(), self.plugin)

        self.assertTrue(first.preflight.accepted)
        self.assertEqual(first.overall_verdict, Verdict.PASS)
        assert first.run is not None
        assert second.run is not None
        assert first.run.manifest is not None
        assert second.run.manifest is not None
        self.assertEqual(first.run.analysis_result, second.run.analysis_result)
        self.assertEqual(first.run.manifest, second.run.manifest)
        self.assertNotEqual(first.run.run_id, second.run.run_id)
        self.assertTrue(first.run.manifest.verification_hash.startswith("sha256:"))

    def test_valid_calculation_can_produce_engineering_fail(self) -> None:
        outcome = self.engine.execute(make_spec(target=4.1), self.plugin)

        self.assertTrue(outcome.preflight.accepted)
        self.assertIsNotNone(outcome.run)
        self.assertEqual(outcome.overall_verdict, Verdict.FAIL)
        self.assertEqual(outcome.verifications[0].status, Verdict.FAIL)

    def test_tolerance_is_applied_conservatively(self) -> None:
        boundary = self.engine.execute(
            make_spec(target=4.0, tolerance=0.1), self.plugin
        )
        sufficient_margin = self.engine.execute(
            make_spec(target=3.9, tolerance=0.1), self.plugin
        )

        self.assertEqual(boundary.overall_verdict, Verdict.FAIL)
        self.assertEqual(sufficient_margin.overall_verdict, Verdict.PASS)

    def test_preflight_identity_mismatch_is_rejected_without_run(self) -> None:
        class WrongIdentityPlugin(FakePhysicsPlugin):
            def preflight(self, spec: EngineeringSpec) -> PreflightResult:
                return PreflightResult(
                    spec_id="different-spec",
                    spec_version=spec.spec_version,
                    accepted=True,
                )

        plugin = WrongIdentityPlugin()
        engine, registration = engine_for(plugin)
        outcome = engine.execute(make_spec(registration=registration), plugin)

        self.assertFalse(outcome.preflight.accepted)
        self.assertIsNone(outcome.run)
        self.assertIn(
            "plugin_preflight_identity_mismatch", outcome.preflight.reason_codes
        )

    def test_unallowlisted_plugin_is_rejected(self) -> None:
        plugin = FakePhysicsPlugin()
        plugin.plugin_version = "1.0.1"
        spec = make_spec()
        assert spec.model is not None
        matching_model = spec.model.model_copy(update={"plugin_version": "1.0.1"})

        outcome = self.engine.execute(
            spec.model_copy(update={"model": matching_model}), plugin
        )

        self.assertFalse(outcome.preflight.accepted)
        self.assertIn("plugin_not_allowed", outcome.preflight.reason_codes)

    def test_registered_plugin_artifact_hash_is_verified(self) -> None:
        registration = fake_registration()
        wrong_registration = PluginRegistration(
            plugin_id=registration.plugin_id,
            plugin_version=registration.plugin_version,
            schema_version=registration.schema_version,
            artifact_hash="sha256:" + "0" * 64,
            artifact_path=registration.artifact_path,
        )
        engine = AnalysisEngine(
            engine_version="0.1.0",
            policy_version="1.0.0",
            allowed_plugins={wrong_registration},
        )

        outcome = engine.execute(
            make_spec(registration=wrong_registration), self.plugin
        )

        self.assertFalse(outcome.preflight.accepted)
        self.assertIn("plugin_artifact_mismatch", outcome.preflight.reason_codes)

    def test_approved_spec_requires_bound_plugin(self) -> None:
        invalid = make_spec().model_copy(update={"model": None})

        outcome = self.engine.execute(invalid, self.plugin)

        self.assertFalse(outcome.preflight.accepted)
        self.assertIn("invalid_spec_contract", outcome.preflight.reason_codes)

    def test_wrong_approved_plugin_identity_is_rejected(self) -> None:
        for update in (
            {"plugin_id": "different.plugin"},
            {"plugin_version": "9.9.9"},
            {"schema_version": "9.9.9"},
            {"artifact_hash": "sha256:" + "0" * 64},
        ):
            with self.subTest(update=update):
                spec = make_spec()
                assert spec.model is not None
                wrong_model = spec.model.model_copy(update=update)
                outcome = self.engine.execute(
                    spec.model_copy(update={"model": wrong_model}), self.plugin
                )
                self.assertFalse(outcome.preflight.accepted)
                self.assertIn(
                    "plugin_not_approved_for_spec", outcome.preflight.reason_codes
                )

    def test_preflight_exception_is_rejected(self) -> None:
        class ExplodingPreflightPlugin(FakePhysicsPlugin):
            def preflight(self, spec: EngineeringSpec) -> PreflightResult:
                raise RuntimeError("boom")

        plugin = ExplodingPreflightPlugin()
        engine, registration = engine_for(plugin)
        outcome = engine.execute(make_spec(registration=registration), plugin)

        self.assertFalse(outcome.preflight.accepted)
        self.assertIn("plugin_preflight_failed", outcome.preflight.reason_codes)

    def test_plugin_identity_mutation_is_blocked_at_every_callback(self) -> None:
        class PreflightMutator(FakePhysicsPlugin):
            def preflight(self, spec: EngineeringSpec) -> PreflightResult:
                result = super().preflight(spec)
                self.plugin_version = "mutated"
                return result

        class RunMutator(FakePhysicsPlugin):
            def run(self, spec: EngineeringSpec) -> AnalysisResult:
                result = super().run(spec)
                self.plugin_version = "mutated"
                return result

        class VerifyMutator(FakePhysicsPlugin):
            def verify(
                self, spec: EngineeringSpec, result: AnalysisResult
            ) -> tuple[VerificationResult, ...]:
                verifications = super().verify(spec, result)
                self.plugin_version = "mutated"
                return verifications

        for plugin_type in (PreflightMutator, RunMutator, VerifyMutator):
            with self.subTest(plugin_type=plugin_type):
                plugin = plugin_type()
                engine, registration = engine_for(plugin)
                outcome = engine.execute(make_spec(registration=registration), plugin)
                self.assertEqual(outcome.overall_verdict, Verdict.INDETERMINATE)
                if plugin_type is PreflightMutator:
                    self.assertIsNone(outcome.run)
                    self.assertIn(
                        "plugin_identity_changed", outcome.preflight.reason_codes
                    )
                else:
                    assert outcome.run is not None
                    self.assertEqual(outcome.run.status, RunStatus.FAILED)
                    self.assertEqual(outcome.run.error_code, "plugin_identity_changed")

    def test_plugin_failure_creates_failed_run_and_indeterminate_evidence(self) -> None:
        class ExplodingPlugin(FakePhysicsPlugin):
            def run(self, spec: EngineeringSpec) -> AnalysisResult:
                raise RuntimeError("boom")

        plugin = ExplodingPlugin()
        engine, registration = engine_for(plugin)
        outcome = engine.execute(make_spec(registration=registration), plugin)

        assert outcome.run is not None
        self.assertEqual(outcome.run.status, RunStatus.FAILED)
        self.assertEqual(outcome.run.error_code, "plugin_failed")
        self.assertEqual(outcome.overall_verdict, Verdict.INDETERMINATE)

    def test_plugin_cannot_bypass_result_contract_with_model_copy(self) -> None:
        class InvalidResultPlugin(FakePhysicsPlugin):
            def run(self, spec: EngineeringSpec) -> AnalysisResult:
                valid = super().run(spec)
                return valid.model_copy(update={"intermediates": {"bad": float("nan")}})

        plugin = InvalidResultPlugin()
        engine, registration = engine_for(plugin)
        outcome = engine.execute(make_spec(registration=registration), plugin)

        assert outcome.run is not None
        self.assertEqual(outcome.run.error_code, "invalid_analysis_contract")

    def test_plugin_cannot_bypass_verification_contract_with_model_copy(self) -> None:
        class InvalidVerificationPlugin(FakePhysicsPlugin):
            def verify(
                self, spec: EngineeringSpec, result: AnalysisResult
            ) -> tuple[VerificationResult, ...]:
                valid = super().verify(spec, result)[0]
                return (valid.model_copy(update={"evidence_refs": ()}),)

        plugin = InvalidVerificationPlugin()
        engine, registration = engine_for(plugin)
        outcome = engine.execute(make_spec(registration=registration), plugin)

        assert outcome.run is not None
        self.assertEqual(outcome.run.error_code, "invalid_verification_contract")

    def test_unknown_verification_id_fails_the_run(self) -> None:
        class WrongEvidencePlugin(FakePhysicsPlugin):
            def verify(
                self, spec: EngineeringSpec, result: AnalysisResult
            ) -> tuple[VerificationResult, ...]:
                valid = super().verify(spec, result)[0]
                return (valid.model_copy(update={"requirement_id": "unknown"}),)

        plugin = WrongEvidencePlugin()
        engine, registration = engine_for(plugin)
        outcome = engine.execute(make_spec(registration=registration), plugin)

        assert outcome.run is not None
        self.assertEqual(outcome.run.status, RunStatus.FAILED)
        self.assertEqual(outcome.run.error_code, "invalid_verification_coverage")
        self.assertEqual(outcome.overall_verdict, Verdict.INDETERMINATE)

    def test_mismatched_verification_target_fails_the_run(self) -> None:
        class WrongEvidencePlugin(FakePhysicsPlugin):
            def verify(
                self, spec: EngineeringSpec, result: AnalysisResult
            ) -> tuple[VerificationResult, ...]:
                valid = super().verify(spec, result)[0]
                return (valid.model_copy(update={"target": quantity(99.0)}),)

        plugin = WrongEvidencePlugin()
        engine, registration = engine_for(plugin)
        outcome = engine.execute(make_spec(registration=registration), plugin)

        assert outcome.run is not None
        self.assertEqual(outcome.run.status, RunStatus.FAILED)
        self.assertEqual(outcome.run.error_code, "invalid_verification_evidence")

    def test_verification_observed_must_match_analysis_output(self) -> None:
        class WrongObservedPlugin(FakePhysicsPlugin):
            def verify(
                self, spec: EngineeringSpec, result: AnalysisResult
            ) -> tuple[VerificationResult, ...]:
                valid = super().verify(spec, result)[0]
                return (valid.model_copy(update={"observed": quantity(99.0)}),)

        plugin = WrongObservedPlugin()
        engine, registration = engine_for(plugin)
        outcome = engine.execute(make_spec(registration=registration), plugin)

        assert outcome.run is not None
        self.assertEqual(outcome.run.error_code, "invalid_verification_evidence")

    def test_engine_rejects_numeric_pass_with_wrong_unit_or_dimension(self) -> None:
        class IncompatibleEvidencePlugin(FakePhysicsPlugin):
            wrong_unit = "mm"
            wrong_dimension = "length"

            def run(self, spec: EngineeringSpec) -> AnalysisResult:
                return AnalysisResult(
                    outputs={
                        "fake_score": Quantity(
                            value=4.0,
                            unit=self.wrong_unit,
                            dimension=self.wrong_dimension,
                            source=SourceRef(
                                kind="user", identifier="test:incompatible"
                            ),
                        )
                    },
                    formula_id="test.incompatible",
                    formula_version="1.0.0",
                )

            def verify(
                self, spec: EngineeringSpec, result: AnalysisResult
            ) -> tuple[VerificationResult, ...]:
                requirement = spec.requirements[0]
                return (
                    VerificationResult(
                        requirement_id=requirement.id,
                        priority=requirement.priority,
                        status=Verdict.PASS,
                        observed=result.outputs[requirement.metric],
                        target=requirement.target,
                        applied_tolerance=requirement.tolerance,
                        margin=0.0,
                        evidence_refs=("analysis:forged-pass",),
                    ),
                )

        for unit, dimension in (("mm", "ratio"), ("1", "length")):
            with self.subTest(unit=unit, dimension=dimension):
                plugin = IncompatibleEvidencePlugin()
                plugin.wrong_unit = unit
                plugin.wrong_dimension = dimension
                engine, registration = engine_for(plugin)
                outcome = engine.execute(make_spec(registration=registration), plugin)

                assert outcome.run is not None
                self.assertEqual(outcome.run.status, RunStatus.FAILED)
                self.assertEqual(
                    outcome.run.error_code, "incompatible_verification_units"
                )
                self.assertEqual(outcome.overall_verdict, Verdict.INDETERMINATE)

    def test_engine_rejects_forged_tolerance_status_and_margin(self) -> None:
        class ForgedEvidencePlugin(FakePhysicsPlugin):
            forged_update: dict[str, object] = {}

            def verify(
                self, spec: EngineeringSpec, result: AnalysisResult
            ) -> tuple[VerificationResult, ...]:
                valid = super().verify(spec, result)[0]
                return (valid.model_copy(update=self.forged_update),)

        cases: tuple[dict[str, object], ...] = (
            {"applied_tolerance": Uncertainty(absolute=1.0, unit="1")},
            {"status": Verdict.FAIL},
            {"margin": 999.0},
        )
        for update in cases:
            with self.subTest(update=update):
                plugin = ForgedEvidencePlugin()
                plugin.forged_update = update
                engine, registration = engine_for(plugin)
                outcome = engine.execute(make_spec(registration=registration), plugin)

                assert outcome.run is not None
                self.assertEqual(
                    outcome.run.error_code, "invalid_verification_evidence"
                )

    def test_duplicate_requirement_ids_are_rejected_at_boundary(self) -> None:
        spec = make_spec()
        duplicate = spec.model_copy(
            update={"requirements": (spec.requirements[0], spec.requirements[0])}
        )

        outcome = self.engine.execute(duplicate, self.plugin)

        self.assertFalse(outcome.preflight.accepted)
        self.assertIn("invalid_spec_contract", outcome.preflight.reason_codes)

    def test_fake_plugin_reports_missing_output_and_unit_mismatch(self) -> None:
        spec = make_spec()
        missing = AnalysisResult(outputs={}, formula_id="test", formula_version="1.0.0")
        wrong_unit = AnalysisResult(
            outputs={
                "fake_score": Quantity(
                    value=4.0,
                    unit="mm",
                    dimension="length",
                    source=SourceRef(kind="user", identifier="test:result"),
                )
            },
            formula_id="test",
            formula_version="1.0.0",
        )

        self.assertEqual(
            self.plugin.verify(spec, missing)[0].reason_code, "missing_output"
        )
        self.assertEqual(
            self.plugin.verify(spec, wrong_unit)[0].reason_code,
            "unit_or_dimension_mismatch",
        )

    def test_all_supported_comparison_operators_execute(self) -> None:
        base = make_spec()
        for operator in ("<", "<=", "==", ">=", ">"):
            with self.subTest(operator=operator):
                requirement = base.requirements[0].model_copy(
                    update={"operator": operator, "target": quantity(4.0)}
                )
                spec = base.model_copy(update={"requirements": (requirement,)})
                outcome = self.engine.execute(spec, self.plugin)
                self.assertIn(outcome.overall_verdict, (Verdict.PASS, Verdict.FAIL))

    def test_manifest_changes_when_approved_input_changes(self) -> None:
        first_spec = make_spec()
        second_payload = first_spec.model_dump()
        second_payload["parameters"] = {"input_value": quantity(3.0)}
        second_spec = EngineeringSpec.model_validate(second_payload)

        first = self.engine.execute(first_spec, self.plugin)
        second = self.engine.execute(second_spec, self.plugin)

        assert first.run is not None
        assert second.run is not None
        assert first.run.manifest is not None
        assert second.run.manifest is not None
        self.assertNotEqual(
            first.run.manifest.input_hash, second.run.manifest.input_hash
        )
        self.assertNotEqual(
            first.run.manifest.output_hash, second.run.manifest.output_hash
        )

    def test_manifest_verification_hash_binds_evidence(self) -> None:
        class ExtraEvidencePlugin(FakePhysicsPlugin):
            def verify(
                self, spec: EngineeringSpec, result: AnalysisResult
            ) -> tuple[VerificationResult, ...]:
                valid = super().verify(spec, result)[0]
                return (
                    valid.model_copy(
                        update={
                            "evidence_refs": valid.evidence_refs + ("review:extra",)
                        }
                    ),
                )

        first = self.engine.execute(make_spec(), self.plugin)
        plugin = ExtraEvidencePlugin()
        engine, registration = engine_for(plugin)
        second = engine.execute(make_spec(registration=registration), plugin)
        assert first.run is not None and first.run.manifest is not None
        assert second.run is not None and second.run.manifest is not None
        self.assertNotEqual(
            first.run.manifest.verification_hash,
            second.run.manifest.verification_hash,
        )

    def test_prepare_requires_exact_approval_before_acceptance(self) -> None:
        spec = make_spec()

        prepared = self.engine.prepare(spec, self.plugin, approval_refs=())

        self.assertFalse(prepared.binding.preflight.accepted)
        self.assertIn(
            "required_spec_approval_missing",
            prepared.binding.preflight.reason_codes,
        )
        outcome = self.engine.execute_prepared(prepared, spec, self.plugin)
        self.assertIsNone(outcome.run)

    def test_execute_wrapper_preserves_deterministic_rejection_evidence_ref(
        self,
    ) -> None:
        outcome = self.engine.execute(make_spec(include_input=False), self.plugin)

        self.assertIsNone(outcome.run)
        self.assertEqual(
            outcome.verifications[0].evidence_refs,
            ("preflight:spec-1:1",),
        )

    def test_prepare_then_execute_does_not_repeat_preflight(self) -> None:
        class CountingPlugin(FakePhysicsPlugin):
            preflight_calls = 0
            run_calls = 0

            def preflight(self, spec: EngineeringSpec) -> PreflightResult:
                self.preflight_calls += 1
                return super().preflight(spec)

            def run(self, spec: EngineeringSpec) -> AnalysisResult:
                self.run_calls += 1
                return super().run(spec)

        plugin = CountingPlugin()
        engine, registration = engine_for(plugin)
        spec = make_spec(registration=registration)

        prepared = engine.prepare(
            spec,
            plugin,
            approval_refs=approval_for(spec),
            preparation_id="preparation-1",
        )
        outcome = engine.execute_prepared(
            prepared,
            spec,
            plugin,
            run_id="run-fixed",
        )

        self.assertTrue(prepared.binding.preflight.accepted)
        self.assertEqual(plugin.preflight_calls, 1)
        self.assertEqual(plugin.run_calls, 1)
        assert outcome.run is not None
        self.assertEqual(outcome.run.run_id, "run-fixed")

    def test_execute_prepared_blocks_stale_or_tampered_inputs_before_run(self) -> None:
        class CountingPlugin(FakePhysicsPlugin):
            run_calls = 0

            def run(self, spec: EngineeringSpec) -> AnalysisResult:
                self.run_calls += 1
                return super().run(spec)

        cases = ("stale_spec", "tampered_token", "engine_changed")
        for case in cases:
            with self.subTest(case=case):
                plugin = CountingPlugin()
                engine, registration = engine_for(plugin)
                spec = make_spec(registration=registration)
                prepared = engine.prepare(
                    spec,
                    plugin,
                    approval_refs=approval_for(spec),
                )
                executing_engine = engine
                executing_spec = spec
                executing_prepared = prepared
                if case == "stale_spec":
                    payload = spec.model_dump(mode="python")
                    payload["parameters"] = {"input_value": quantity(3.0)}
                    executing_spec = EngineeringSpec.model_validate(payload)
                elif case == "tampered_token":
                    executing_prepared = prepared.model_copy(
                        update={
                            "binding": prepared.binding.model_copy(
                                update={"policy_version": "tampered"}
                            )
                        }
                    )
                else:
                    executing_engine = AnalysisEngine(
                        engine_version="0.2.0",
                        policy_version="1.0.0",
                        allowed_plugins={registration},
                    )

                outcome = executing_engine.execute_prepared(
                    executing_prepared,
                    executing_spec,
                    plugin,
                )

                self.assertIsNone(outcome.run)
                self.assertFalse(outcome.preflight.accepted)
                self.assertEqual(plugin.run_calls, 0)

    def test_execute_prepared_rechecks_bound_design_revision(self) -> None:
        plugin = FakePhysicsPlugin()
        engine, registration = engine_for(plugin)
        spec = make_spec(registration=registration)
        first_revision = make_revision()
        prepared = engine.prepare(
            spec,
            plugin,
            approval_refs=approval_for(spec, revision=first_revision),
            revision=first_revision,
        )

        outcome = engine.execute_prepared(
            prepared,
            spec,
            plugin,
            revision=make_revision(2),
        )

        self.assertIsNone(outcome.run)
        self.assertIn("prepared_revision_mismatch", outcome.preflight.reason_codes)

    def test_engine_policy_mutation_during_plugin_run_fails_closed(self) -> None:
        class EngineMutatingPlugin(FakePhysicsPlugin):
            engine: AnalysisEngine | None = None

            def run(self, spec: EngineeringSpec) -> AnalysisResult:
                assert self.engine is not None
                self.engine.policy_version = "mutated-during-run"
                return super().run(spec)

        plugin = EngineMutatingPlugin()
        engine, registration = engine_for(plugin)
        plugin.engine = engine
        spec = make_spec(registration=registration)
        prepared = engine.prepare(
            spec,
            plugin,
            approval_refs=approval_for(spec),
        )

        outcome = engine.execute_prepared(prepared, spec, plugin)

        assert outcome.run is not None
        self.assertEqual(outcome.run.status, RunStatus.FAILED)
        self.assertEqual(outcome.run.error_code, "engine_policy_identity_changed")
        self.assertIsNone(outcome.run.manifest)

    def test_engine_policy_mutation_during_preflight_rejects_preparation(
        self,
    ) -> None:
        class PreflightMutatingPlugin(FakePhysicsPlugin):
            engine: AnalysisEngine | None = None

            def preflight(self, spec: EngineeringSpec) -> PreflightResult:
                assert self.engine is not None
                self.engine.policy_version = "mutated-during-preflight"
                return super().preflight(spec)

        plugin = PreflightMutatingPlugin()
        engine, registration = engine_for(plugin)
        plugin.engine = engine
        spec = make_spec(registration=registration)

        prepared = engine.prepare(
            spec,
            plugin,
            approval_refs=approval_for(spec),
        )

        self.assertFalse(prepared.binding.preflight.accepted)
        self.assertIn(
            "engine_policy_identity_changed",
            prepared.binding.preflight.reason_codes,
        )
        self.assertEqual(prepared.binding.policy_version, "1.0.0")

    def test_execute_wrapper_matches_two_stage_success_contract(self) -> None:
        spec = make_spec()

        wrapped = self.engine.execute(spec, self.plugin)
        prepared = self.engine.prepare(
            spec,
            self.plugin,
            approval_refs=approval_for(spec),
        )
        staged = self.engine.execute_prepared(prepared, spec, self.plugin)

        assert wrapped.run is not None and staged.run is not None
        self.assertEqual(wrapped.run.analysis_result, staged.run.analysis_result)
        self.assertEqual(wrapped.run.manifest, staged.run.manifest)
        self.assertEqual(wrapped.verifications, staged.verifications)
        self.assertNotEqual(wrapped.run.run_id, staged.run.run_id)

    def test_prepare_rejects_invalid_stale_and_revision_approvals(self) -> None:
        plugin = FakePhysicsPlugin()
        engine, registration = engine_for(plugin)
        spec = make_spec(registration=registration)
        spec_approval = approval_for(spec)[0]
        revision = make_revision()
        revision_approval = approval_for(spec, revision=revision)[1]
        cases = (
            (
                "duplicate",
                (spec_approval, spec_approval),
                None,
                "invalid_approval_contract",
            ),
            (
                "stale_spec",
                (
                    spec_approval.model_copy(
                        update={"subject_hash": "sha256:" + "0" * 64}
                    ),
                ),
                None,
                "required_spec_approval_stale",
            ),
            (
                "missing_revision",
                (spec_approval,),
                revision,
                "required_revision_approval_missing",
            ),
            (
                "stale_revision",
                (
                    spec_approval,
                    revision_approval.model_copy(
                        update={"subject_hash": "sha256:" + "0" * 64}
                    ),
                ),
                revision,
                "required_revision_approval_stale",
            ),
        )
        for name, approvals, selected_revision, reason in cases:
            with self.subTest(name=name):
                prepared = engine.prepare(
                    spec,
                    plugin,
                    approval_refs=approvals,
                    revision=selected_revision,
                )
                self.assertFalse(prepared.binding.preflight.accepted)
                self.assertIn(reason, prepared.binding.preflight.reason_codes)

    def test_prepare_rejects_invalid_or_cross_project_revision(self) -> None:
        plugin = FakePhysicsPlugin()
        engine, registration = engine_for(plugin)
        spec = make_spec(registration=registration)
        valid = make_revision()
        invalid = valid.model_copy(update={"artifacts": ()})
        cross_project = valid.model_copy(update={"project_id": "other-project"})

        invalid_prepared = engine.prepare(
            spec,
            plugin,
            approval_refs=approval_for(spec),
            revision=invalid,
        )
        cross_project_prepared = engine.prepare(
            spec,
            plugin,
            approval_refs=approval_for(spec),
            revision=cross_project,
        )

        self.assertIn(
            "invalid_revision_contract",
            invalid_prepared.binding.preflight.reason_codes,
        )
        self.assertIn(
            "revision_project_mismatch",
            cross_project_prepared.binding.preflight.reason_codes,
        )

    def test_execute_prepared_rejects_resealed_expectations_and_bad_revision(
        self,
    ) -> None:
        plugin = FakePhysicsPlugin()
        engine, registration = engine_for(plugin)
        spec = make_spec(registration=registration)
        prepared = engine.prepare(
            spec,
            plugin,
            approval_refs=approval_for(spec),
        )
        changed_binding = prepared.binding.model_copy(
            update={"expected_metrics": ("different_metric",)}
        )
        resealed = PreparedAnalysis(
            preparation_id=prepared.preparation_id,
            binding=changed_binding,
            prepare_hash=canonical_sha256(changed_binding),
        )

        expectation_outcome = engine.execute_prepared(resealed, spec, plugin)

        self.assertIn(
            "prepared_expectations_mismatch",
            expectation_outcome.preflight.reason_codes,
        )

        revision = make_revision()
        revision_prepared = engine.prepare(
            spec,
            plugin,
            approval_refs=approval_for(spec, revision=revision),
            revision=revision,
        )
        invalid_revision = revision.model_copy(update={"artifacts": ()})
        revision_outcome = engine.execute_prepared(
            revision_prepared,
            spec,
            plugin,
            revision=invalid_revision,
        )
        self.assertIn(
            "invalid_revision_contract",
            revision_outcome.preflight.reason_codes,
        )

        plugin.plugin_version = "mutated"
        plugin_outcome = engine.execute_prepared(prepared, spec, plugin)
        self.assertIn(
            "prepared_plugin_mismatch",
            plugin_outcome.preflight.reason_codes,
        )


if __name__ == "__main__":
    unittest.main()
