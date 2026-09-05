from __future__ import annotations

import unittest

from pydantic import ValidationError

from forge_core.deployment_readiness import (
    DeploymentMode,
    DeploymentReadinessStatus,
    DeploymentRequirement,
    assess_deployment_readiness,
    build_deployment_profile,
)


class DeploymentReadinessTests(unittest.TestCase):
    def test_local_loopback_profile_is_ready_without_public_controls(self) -> None:
        profile = build_deployment_profile(mode=DeploymentMode.LOCAL_LOOPBACK)

        assessment = assess_deployment_readiness(profile)

        self.assertEqual(assessment.status, DeploymentReadinessStatus.READY)
        self.assertEqual(assessment.unmet_requirements, ())
        self.assertEqual(assessment.profile_hash, profile.profile_hash)
        self.assertTrue(assessment.assessment_hash.startswith("sha256:"))

    def test_public_profile_fails_closed_until_all_deployment_controls_exist(
        self,
    ) -> None:
        profile = build_deployment_profile(
            mode=DeploymentMode.PUBLIC_MULTI_TENANT,
            authentication_enforced=True,
            rbac_enforced=True,
        )

        assessment = assess_deployment_readiness(profile)

        self.assertEqual(assessment.status, DeploymentReadinessStatus.BLOCKED)
        self.assertEqual(
            tuple(item.requirement for item in assessment.unmet_requirements),
            (
                DeploymentRequirement.ORG_TENANT_ISOLATION,
                DeploymentRequirement.AUDIT_TRAIL,
                DeploymentRequirement.SECRET_MANAGEMENT,
                DeploymentRequirement.BACKUP_RESTORE,
                DeploymentRequirement.MONITORING,
            ),
        )
        self.assertIn(
            "cross-tenant denial test",
            assessment.unmet_requirements[0].required_evidence,
        )

    def test_public_profile_is_ready_only_when_required_controls_are_true(self) -> None:
        profile = build_deployment_profile(
            mode=DeploymentMode.PUBLIC_SINGLE_TENANT,
            authentication_enforced=True,
            org_tenant_isolation_enforced=True,
            rbac_enforced=True,
            append_only_audit_trail=True,
            secret_management_configured=True,
            backup_restore_tested=True,
            monitoring_configured=True,
        )

        assessment = assess_deployment_readiness(profile)

        self.assertEqual(assessment.status, DeploymentReadinessStatus.READY)
        self.assertEqual(assessment.unmet_requirements, ())

    def test_profile_and_assessment_hashes_are_tamper_visible(self) -> None:
        profile = build_deployment_profile(mode=DeploymentMode.LOCAL_LOOPBACK)
        assessment = assess_deployment_readiness(profile)

        with self.assertRaises(ValidationError):
            type(profile).model_validate(
                profile.model_dump(mode="python") | {"authentication_enforced": True}
            )
        with self.assertRaises(ValidationError):
            type(assessment).model_validate(
                assessment.model_dump(mode="python")
                | {"status": DeploymentReadinessStatus.BLOCKED}
            )

    def test_unknown_fields_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            type(
                build_deployment_profile(mode=DeploymentMode.LOCAL_LOOPBACK)
            ).model_validate(
                {
                    "mode": DeploymentMode.LOCAL_LOOPBACK,
                    "profile_hash": "sha256:" + ("0" * 64),
                    "unexpected": True,
                }
            )


if __name__ == "__main__":
    unittest.main()
