from __future__ import annotations

import unittest
from datetime import UTC, datetime
from typing import cast

from pydantic import ValidationError

from forge_core.access_control import (
    Actor,
    AuditEvent,
    AuthorizationRequest,
    Membership,
    Organization,
    Permission,
    ProjectAccess,
    Role,
    audit_event_hash,
    authorize,
    build_audit_event,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def actor(actor_id: str = "engineer@example.com") -> Actor:
    return Actor(actor_id=actor_id, display_name="Engineer")


def membership(
    role: Role,
    *,
    org_id: str = "org-a",
    actor_id: str = "engineer@example.com",
) -> Membership:
    return Membership(
        org_id=org_id,
        actor_id=actor_id,
        role=role,
        granted_by="admin@example.com",
        granted_at=NOW,
    )


def project_access(
    *,
    org_id: str = "org-a",
    project_id: str = "robot-arm",
    actor_id: str = "engineer@example.com",
) -> ProjectAccess:
    return ProjectAccess(org_id=org_id, project_id=project_id, actor_id=actor_id)


def request(
    permission: Permission,
    *,
    org_id: str = "org-a",
    project_id: str = "robot-arm",
    actor_id: str = "engineer@example.com",
    target_actor_id: str | None = None,
    require_separation_of_duty: bool = False,
) -> AuthorizationRequest:
    return AuthorizationRequest(
        org_id=org_id,
        project_id=project_id,
        actor_id=actor_id,
        permission=permission,
        target_actor_id=target_actor_id,
        require_separation_of_duty=require_separation_of_duty,
    )


class AccessControlContractTests(unittest.TestCase):
    def test_models_reject_unknown_fields_and_non_utc_time(self) -> None:
        with self.assertRaises(ValidationError):
            Organization.model_validate(
                {
                    "org_id": "org-a",
                    "name": "Robotics",
                    "created_at": NOW.isoformat(),
                    "unexpected": True,
                }
            )

        with self.assertRaises(ValidationError):
            Organization(
                org_id="org-a",
                name="Robotics",
                created_at=datetime(2026, 9, 3, 12, 0),
            )

    def test_contract_hashes_are_stable_and_tamper_visible(self) -> None:
        first = Organization(org_id="org-a", name="Robotics", created_at=NOW)
        second = Organization(org_id="org-a", name="Robotics", created_at=NOW)
        changed = Organization(org_id="org-a", name="Other Robotics", created_at=NOW)

        self.assertEqual(first.organization_hash, second.organization_hash)
        self.assertNotEqual(first.organization_hash, changed.organization_hash)

    def test_viewer_can_read_only(self) -> None:
        read = authorize(
            actor=actor(),
            membership=membership(Role.VIEWER),
            project_access=project_access(),
            request=request(Permission.READ_PROJECT),
        )
        write = authorize(
            actor=actor(),
            membership=membership(Role.VIEWER),
            project_access=project_access(),
            request=request(Permission.CREATE_PLAN),
        )

        self.assertTrue(read.allowed)
        self.assertEqual(read.reason, "allowed")
        self.assertFalse(write.allowed)
        self.assertEqual(write.reason, "role_lacks_permission")

    def test_editor_can_plan_and_mutate_evidence_but_not_release(self) -> None:
        for permission in (
            Permission.READ_PROJECT,
            Permission.CREATE_PLAN,
            Permission.MUTATE_EVIDENCE,
        ):
            with self.subTest(permission=permission):
                result = authorize(
                    actor=actor(),
                    membership=membership(Role.EDITOR),
                    project_access=project_access(),
                    request=request(permission),
                )
                self.assertTrue(result.allowed)

        decision = authorize(
            actor=actor(),
            membership=membership(Role.EDITOR),
            project_access=project_access(),
            request=request(Permission.DECIDE_RELEASE),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "role_lacks_permission")

    def test_approver_can_accept_verify_and_decide_release(self) -> None:
        for permission in (
            Permission.ACCEPT_DESIGN,
            Permission.VERIFY_EVIDENCE,
            Permission.DECIDE_RELEASE,
        ):
            with self.subTest(permission=permission):
                result = authorize(
                    actor=actor(),
                    membership=membership(Role.APPROVER),
                    project_access=project_access(),
                    request=request(permission, target_actor_id="other@example.com"),
                )
                self.assertTrue(result.allowed)

    def test_admin_can_manage_membership_and_org(self) -> None:
        for permission in (Permission.MANAGE_MEMBERSHIP, Permission.MANAGE_ORG):
            with self.subTest(permission=permission):
                result = authorize(
                    actor=actor(),
                    membership=membership(Role.ADMIN),
                    project_access=project_access(),
                    request=request(permission),
                )
                self.assertTrue(result.allowed)

    def test_tenant_or_project_mismatch_denies_before_permission(self) -> None:
        tenant = authorize(
            actor=actor(),
            membership=membership(Role.ADMIN, org_id="org-b"),
            project_access=project_access(),
            request=request(Permission.READ_PROJECT),
        )
        project = authorize(
            actor=actor(),
            membership=membership(Role.ADMIN),
            project_access=project_access(project_id="other-project"),
            request=request(Permission.READ_PROJECT),
        )

        self.assertFalse(tenant.allowed)
        self.assertEqual(tenant.reason, "tenant_mismatch")
        self.assertFalse(project.allowed)
        self.assertEqual(project.reason, "project_mismatch")

    def test_identity_and_active_state_mismatches_fail_closed(self) -> None:
        cases: tuple[
            tuple[str, Actor, Membership, ProjectAccess, AuthorizationRequest],
            ...,
        ] = (
            (
                "actor_inactive",
                actor().model_copy(update={"active": False}),
                membership(Role.ADMIN),
                project_access(),
                request(Permission.READ_PROJECT),
            ),
            (
                "actor_request_mismatch",
                actor("other@example.com"),
                membership(Role.ADMIN, actor_id="other@example.com"),
                project_access(actor_id="other@example.com"),
                request(Permission.READ_PROJECT),
            ),
            (
                "membership_actor_mismatch",
                actor(),
                membership(Role.ADMIN, actor_id="other@example.com"),
                project_access(),
                request(Permission.READ_PROJECT),
            ),
            (
                "project_access_actor_mismatch",
                actor(),
                membership(Role.ADMIN),
                project_access(actor_id="other@example.com"),
                request(Permission.READ_PROJECT),
            ),
            (
                "membership_inactive",
                actor(),
                membership(Role.ADMIN).model_copy(update={"active": False}),
                project_access(),
                request(Permission.READ_PROJECT),
            ),
            (
                "project_access_inactive",
                actor(),
                membership(Role.ADMIN),
                project_access().model_copy(update={"active": False}),
                request(Permission.READ_PROJECT),
            ),
        )

        for expected, case_actor, case_membership, case_access, case_request in cases:
            with self.subTest(reason=expected):
                result = authorize(
                    actor=case_actor,
                    membership=case_membership,
                    project_access=case_access,
                    request=case_request,
                )
                self.assertFalse(result.allowed)
                self.assertEqual(result.reason, expected)

    def test_contract_identity_hashes_are_exposed(self) -> None:
        self.assertRegex(actor().actor_hash, r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(
            membership(Role.VIEWER).membership_hash,
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertRegex(
            project_access().project_access_hash,
            r"^sha256:[0-9a-f]{64}$",
        )

    def test_separation_of_duty_blocks_self_approval(self) -> None:
        self_approval = authorize(
            actor=actor(),
            membership=membership(Role.APPROVER),
            project_access=project_access(),
            request=request(
                Permission.DECIDE_RELEASE,
                target_actor_id="engineer@example.com",
                require_separation_of_duty=True,
            ),
        )
        other_approval = authorize(
            actor=actor(),
            membership=membership(Role.APPROVER),
            project_access=project_access(),
            request=request(
                Permission.DECIDE_RELEASE,
                target_actor_id="other@example.com",
                require_separation_of_duty=True,
            ),
        )

        self.assertFalse(self_approval.allowed)
        self.assertEqual(self_approval.reason, "separation_of_duty_violation")
        self.assertTrue(other_approval.allowed)

    def test_audit_event_binds_request_result_and_hash(self) -> None:
        auth_request = request(
            Permission.DECIDE_RELEASE, target_actor_id="other@example.com"
        )
        auth_result = authorize(
            actor=actor(),
            membership=membership(Role.APPROVER),
            project_access=project_access(),
            request=auth_request,
        )
        event = build_audit_event(
            event_id="audit-001",
            request=auth_request,
            result=auth_result,
            occurred_at=NOW,
        )

        self.assertEqual(event.org_id, auth_request.org_id)
        self.assertEqual(event.actor_id, auth_request.actor_id)
        self.assertEqual(event.project_id, auth_request.project_id)
        self.assertEqual(event.operation, Permission.DECIDE_RELEASE)
        self.assertEqual(event.request_hash, auth_request.request_hash)
        self.assertEqual(event.result_hash, auth_result.result_hash)
        self.assertEqual(event.event_hash, audit_event_hash(event))

        tampered = event.model_dump(mode="json")
        tampered["reason"] = "allowed after edit"
        with self.assertRaises(ValidationError):
            AuditEvent.model_validate(tampered)

    def test_hash_and_datetime_contracts_are_enforced(self) -> None:
        auth_request = request(Permission.READ_PROJECT)
        auth_result = authorize(
            actor=actor(),
            membership=membership(Role.VIEWER),
            project_access=project_access(),
            request=auth_request,
        )
        event = build_audit_event(
            event_id="audit-002",
            request=auth_request,
            result=auth_result,
            occurred_at=NOW,
        )

        invalid_hash = event.model_dump(mode="json")
        invalid_hash["event_hash"] = "not-a-hash"
        with self.assertRaises(ValidationError):
            AuditEvent.model_validate(invalid_hash)

        naive_time = event.model_dump(mode="json")
        naive_time["occurred_at"] = "2026-09-03T12:00:00"
        with self.assertRaises(ValidationError):
            AuditEvent.model_validate(naive_time)

    def test_contracts_are_frozen(self) -> None:
        subject = actor()
        with self.assertRaises(ValidationError):
            cast(object, subject).__setattr__("display_name", "Changed")


if __name__ == "__main__":
    unittest.main()
