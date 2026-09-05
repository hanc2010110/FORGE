from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from forge_core.hashing import canonical_sha256
from forge_core.models import ContractModel


class DeploymentMode(StrEnum):
    LOCAL_LOOPBACK = "local_loopback"
    PUBLIC_SINGLE_TENANT = "public_single_tenant"
    PUBLIC_MULTI_TENANT = "public_multi_tenant"


class DeploymentRequirement(StrEnum):
    AUTHENTICATION = "authentication"
    ORG_TENANT_ISOLATION = "org_tenant_isolation"
    RBAC = "rbac"
    AUDIT_TRAIL = "audit_trail"
    SECRET_MANAGEMENT = "secret_management"
    BACKUP_RESTORE = "backup_restore"
    MONITORING = "monitoring"


class DeploymentReadinessStatus(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"


class DeploymentCapabilityProfile(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    mode: DeploymentMode
    authentication_enforced: bool = False
    org_tenant_isolation_enforced: bool = False
    rbac_enforced: bool = False
    append_only_audit_trail: bool = False
    secret_management_configured: bool = False
    backup_restore_tested: bool = False
    monitoring_configured: bool = False
    profile_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def profile_hash_must_match_payload(self) -> DeploymentCapabilityProfile:
        expected = deployment_profile_hash(
            self.mode,
            self.authentication_enforced,
            self.org_tenant_isolation_enforced,
            self.rbac_enforced,
            self.append_only_audit_trail,
            self.secret_management_configured,
            self.backup_restore_tested,
            self.monitoring_configured,
        )
        if self.profile_hash != expected:
            raise ValueError("deployment profile hash does not match payload")
        return self


class UnmetDeploymentRequirement(ContractModel):
    requirement: DeploymentRequirement
    reason: str = Field(min_length=1)
    required_evidence: str = Field(min_length=1)


class DeploymentReadinessAssessment(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    mode: DeploymentMode
    status: DeploymentReadinessStatus
    profile_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    unmet_requirements: tuple[UnmetDeploymentRequirement, ...] = ()
    assessment_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def assessment_must_fail_closed_and_match_hash(
        self,
    ) -> DeploymentReadinessAssessment:
        if self.status is DeploymentReadinessStatus.READY and self.unmet_requirements:
            raise ValueError("ready assessment cannot include unmet requirements")
        if (
            self.status is DeploymentReadinessStatus.BLOCKED
            and not self.unmet_requirements
        ):
            raise ValueError("blocked assessment requires unmet requirements")
        expected = deployment_assessment_hash(
            self.mode,
            self.status,
            self.profile_hash,
            self.unmet_requirements,
        )
        if self.assessment_hash != expected:
            raise ValueError("deployment assessment hash does not match payload")
        return self


class _DeploymentProfilePayload(ContractModel):
    mode: DeploymentMode
    authentication_enforced: bool
    org_tenant_isolation_enforced: bool
    rbac_enforced: bool
    append_only_audit_trail: bool
    secret_management_configured: bool
    backup_restore_tested: bool
    monitoring_configured: bool


class _DeploymentAssessmentPayload(ContractModel):
    mode: DeploymentMode
    status: DeploymentReadinessStatus
    profile_hash: str
    unmet_requirements: tuple[UnmetDeploymentRequirement, ...]


_PUBLIC_REQUIREMENTS: tuple[
    tuple[DeploymentRequirement, str, str, str],
    ...,
] = (
    (
        DeploymentRequirement.AUTHENTICATION,
        "authentication_enforced",
        "public deployment requires authenticated actors",
        "identity provider session validation and rejected anonymous request log",
    ),
    (
        DeploymentRequirement.ORG_TENANT_ISOLATION,
        "org_tenant_isolation_enforced",
        "public deployment requires organization/project tenant isolation",
        "cross-tenant denial test and storage ownership policy evidence",
    ),
    (
        DeploymentRequirement.RBAC,
        "rbac_enforced",
        "public deployment requires permission checks on mutations and release actions",
        "authorization matrix test covering viewer, editor, approver and admin roles",
    ),
    (
        DeploymentRequirement.AUDIT_TRAIL,
        "append_only_audit_trail",
        "public deployment requires append-only audit events for authorization results",
        "tamper-visible audit event export with request/result hashes",
    ),
    (
        DeploymentRequirement.SECRET_MANAGEMENT,
        "secret_management_configured",
        "public deployment requires managed secrets outside source and browser state",
        "secret inventory, rotation procedure and leak scan result",
    ),
    (
        DeploymentRequirement.BACKUP_RESTORE,
        "backup_restore_tested",
        "public deployment requires tested backup and restore for release evidence",
        "restore drill result bound to database and artifact snapshot hashes",
    ),
    (
        DeploymentRequirement.MONITORING,
        "monitoring_configured",
        "public deployment requires health, error and audit monitoring",
        "monitoring dashboard or alert test for API, storage and connector failures",
    ),
)


def deployment_profile_hash(
    mode: DeploymentMode,
    authentication_enforced: bool,
    org_tenant_isolation_enforced: bool,
    rbac_enforced: bool,
    append_only_audit_trail: bool,
    secret_management_configured: bool,
    backup_restore_tested: bool,
    monitoring_configured: bool,
) -> str:
    return canonical_sha256(
        _DeploymentProfilePayload(
            mode=mode,
            authentication_enforced=authentication_enforced,
            org_tenant_isolation_enforced=org_tenant_isolation_enforced,
            rbac_enforced=rbac_enforced,
            append_only_audit_trail=append_only_audit_trail,
            secret_management_configured=secret_management_configured,
            backup_restore_tested=backup_restore_tested,
            monitoring_configured=monitoring_configured,
        )
    )


def build_deployment_profile(
    *,
    mode: DeploymentMode,
    authentication_enforced: bool = False,
    org_tenant_isolation_enforced: bool = False,
    rbac_enforced: bool = False,
    append_only_audit_trail: bool = False,
    secret_management_configured: bool = False,
    backup_restore_tested: bool = False,
    monitoring_configured: bool = False,
) -> DeploymentCapabilityProfile:
    return DeploymentCapabilityProfile(
        mode=mode,
        authentication_enforced=authentication_enforced,
        org_tenant_isolation_enforced=org_tenant_isolation_enforced,
        rbac_enforced=rbac_enforced,
        append_only_audit_trail=append_only_audit_trail,
        secret_management_configured=secret_management_configured,
        backup_restore_tested=backup_restore_tested,
        monitoring_configured=monitoring_configured,
        profile_hash=deployment_profile_hash(
            mode,
            authentication_enforced,
            org_tenant_isolation_enforced,
            rbac_enforced,
            append_only_audit_trail,
            secret_management_configured,
            backup_restore_tested,
            monitoring_configured,
        ),
    )


def assess_deployment_readiness(
    profile: DeploymentCapabilityProfile,
) -> DeploymentReadinessAssessment:
    normalized = DeploymentCapabilityProfile.model_validate(
        profile.model_dump(mode="python")
    )
    unmet = tuple(
        UnmetDeploymentRequirement(
            requirement=requirement,
            reason=reason,
            required_evidence=required_evidence,
        )
        for requirement, field_name, reason, required_evidence in _PUBLIC_REQUIREMENTS
        if normalized.mode is not DeploymentMode.LOCAL_LOOPBACK
        and not bool(getattr(normalized, field_name))
    )
    status = (
        DeploymentReadinessStatus.BLOCKED if unmet else DeploymentReadinessStatus.READY
    )
    assessment_hash = deployment_assessment_hash(
        normalized.mode,
        status,
        normalized.profile_hash,
        unmet,
    )
    return DeploymentReadinessAssessment(
        mode=normalized.mode,
        status=status,
        profile_hash=normalized.profile_hash,
        unmet_requirements=unmet,
        assessment_hash=assessment_hash,
    )


def deployment_assessment_hash(
    mode: DeploymentMode,
    status: DeploymentReadinessStatus,
    profile_hash: str,
    unmet_requirements: tuple[UnmetDeploymentRequirement, ...],
) -> str:
    return canonical_sha256(
        _DeploymentAssessmentPayload(
            mode=mode,
            status=status,
            profile_hash=profile_hash,
            unmet_requirements=unmet_requirements,
        )
    )
