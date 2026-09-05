from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from forge_core.hashing import canonical_sha256
from forge_core.models import ContractModel

LOCAL_ORG_ID = "local-org"
LOCAL_ACTOR_ID = "local-operator"


class AuthorizationDeniedError(PermissionError):
    """Raised when persisted access-control state denies an operation."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AccessContract(ContractModel):
    """Immutable versioned access-control contract."""


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


class Role(StrEnum):
    VIEWER = "viewer"
    EDITOR = "editor"
    APPROVER = "approver"
    ADMIN = "admin"


class Permission(StrEnum):
    READ_PROJECT = "read_project"
    CREATE_PLAN = "create_plan"
    MUTATE_EVIDENCE = "mutate_evidence"
    ACCEPT_DESIGN = "accept_design"
    VERIFY_EVIDENCE = "verify_evidence"
    DECIDE_RELEASE = "decide_release"
    MANAGE_MEMBERSHIP = "manage_membership"
    MANAGE_ORG = "manage_org"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset({Permission.READ_PROJECT}),
    Role.EDITOR: frozenset(
        {
            Permission.READ_PROJECT,
            Permission.CREATE_PLAN,
            Permission.MUTATE_EVIDENCE,
        }
    ),
    Role.APPROVER: frozenset(
        {
            Permission.READ_PROJECT,
            Permission.CREATE_PLAN,
            Permission.MUTATE_EVIDENCE,
            Permission.ACCEPT_DESIGN,
            Permission.VERIFY_EVIDENCE,
            Permission.DECIDE_RELEASE,
        }
    ),
    Role.ADMIN: frozenset(Permission),
}


class Organization(AccessContract):
    schema_version: Literal["1.0.0"] = "1.0.0"
    org_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    name: str = Field(min_length=1, max_length=160)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "created_at")

    @property
    def organization_hash(self) -> str:
        return canonical_sha256(self)


class Actor(AccessContract):
    schema_version: Literal["1.0.0"] = "1.0.0"
    actor_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
    display_name: str = Field(min_length=1, max_length=160)
    active: bool = True

    @property
    def actor_hash(self) -> str:
        return canonical_sha256(self)


class Membership(AccessContract):
    schema_version: Literal["1.0.0"] = "1.0.0"
    org_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    role: Role
    granted_by: str = Field(min_length=1)
    granted_at: datetime
    active: bool = True

    @field_validator("granted_at")
    @classmethod
    def granted_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "granted_at")

    @property
    def membership_hash(self) -> str:
        return canonical_sha256(self)


class ProjectAccess(AccessContract):
    schema_version: Literal["1.0.0"] = "1.0.0"
    org_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    active: bool = True

    @property
    def project_access_hash(self) -> str:
        return canonical_sha256(self)


class AuthorizationRequest(AccessContract):
    schema_version: Literal["1.0.0"] = "1.0.0"
    org_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    permission: Permission
    target_actor_id: str | None = Field(default=None, min_length=1)
    require_separation_of_duty: bool = False

    @property
    def request_hash(self) -> str:
        return canonical_sha256(self)


class AuthorizationResult(AccessContract):
    schema_version: Literal["1.0.0"] = "1.0.0"
    allowed: bool
    reason: str = Field(min_length=1)
    role: Role | None = None
    permission: Permission
    request_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @property
    def result_hash(self) -> str:
        return canonical_sha256(self)


class AuditEvent(AccessContract):
    schema_version: Literal["1.0.0"] = "1.0.0"
    event_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    org_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    operation: Permission
    request_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    result_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    allowed: bool
    reason: str = Field(min_length=1)
    occurred_at: datetime
    event_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "occurred_at")

    @model_validator(mode="after")
    def event_hash_must_match_payload(self) -> AuditEvent:
        expected = audit_event_hash(self)
        if self.event_hash != expected:
            raise ValueError("event_hash does not match audit event payload")
        return self


def audit_event_hash(event: AuditEvent) -> str:
    payload = event.model_copy(update={"event_hash": "sha256:" + ("0" * 64)})
    return canonical_sha256(payload)


def build_audit_event(
    *,
    event_id: str,
    request: AuthorizationRequest,
    result: AuthorizationResult,
    occurred_at: datetime,
) -> AuditEvent:
    draft = AuditEvent.model_construct(
        schema_version="1.0.0",
        event_id=event_id,
        org_id=request.org_id,
        actor_id=request.actor_id,
        project_id=request.project_id,
        operation=request.permission,
        request_hash=request.request_hash,
        result_hash=result.result_hash,
        allowed=result.allowed,
        reason=result.reason,
        occurred_at=occurred_at,
        event_hash="sha256:" + ("0" * 64),
    )
    return AuditEvent.model_validate(
        draft.model_copy(update={"event_hash": audit_event_hash(draft)}).model_dump()
    )


def authorize(
    *,
    actor: Actor,
    membership: Membership,
    project_access: ProjectAccess,
    request: AuthorizationRequest,
) -> AuthorizationResult:
    allowed = False
    reason = "permission_denied"

    if not actor.active:
        reason = "actor_inactive"
    elif actor.actor_id != request.actor_id:
        reason = "actor_request_mismatch"
    elif membership.actor_id != actor.actor_id:
        reason = "membership_actor_mismatch"
    elif project_access.actor_id != actor.actor_id:
        reason = "project_access_actor_mismatch"
    elif membership.org_id != request.org_id or project_access.org_id != request.org_id:
        reason = "tenant_mismatch"
    elif project_access.project_id != request.project_id:
        reason = "project_mismatch"
    elif not membership.active:
        reason = "membership_inactive"
    elif not project_access.active:
        reason = "project_access_inactive"
    elif request.permission not in ROLE_PERMISSIONS[membership.role]:
        reason = "role_lacks_permission"
    elif (
        request.require_separation_of_duty
        and request.permission
        in {
            Permission.ACCEPT_DESIGN,
            Permission.VERIFY_EVIDENCE,
            Permission.DECIDE_RELEASE,
        }
        and request.target_actor_id == actor.actor_id
    ):
        reason = "separation_of_duty_violation"
    else:
        allowed = True
        reason = "allowed"

    return AuthorizationResult(
        allowed=allowed,
        reason=reason,
        role=membership.role,
        permission=request.permission,
        request_hash=request.request_hash,
    )
