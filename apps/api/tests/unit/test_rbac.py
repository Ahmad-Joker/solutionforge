from __future__ import annotations

from itertools import pairwise

import pytest

from solutionforge.security.rbac import (
    ROLE_PERMISSIONS,
    ROLE_RANK,
    Permission,
    Role,
    can_assign_role,
    has_permission,
)

ROLES_ASC = sorted(Role, key=lambda r: ROLE_RANK[r])


def test_every_role_has_a_permission_set_and_rank() -> None:
    assert set(ROLE_PERMISSIONS) == set(Role)
    assert set(ROLE_RANK) == set(Role)


@pytest.mark.parametrize(("lower", "higher"), list(pairwise(ROLES_ASC)))
def test_permissions_are_monotonic_in_rank(lower: Role, higher: Role) -> None:
    """A higher role never loses a permission a lower role has."""
    assert ROLE_PERMISSIONS[lower] < ROLE_PERMISSIONS[higher]


@pytest.mark.parametrize(
    ("role", "permission", "expected"),
    [
        (Role.VIEWER, Permission.WORKFLOW_READ, True),
        (Role.VIEWER, Permission.WORKFLOW_EXECUTE, False),
        (Role.VIEWER, Permission.APPROVAL_DECIDE, False),
        (Role.VIEWER, Permission.AUDIT_READ, False),
        (Role.OPERATOR, Permission.WORKFLOW_EXECUTE, True),
        (Role.OPERATOR, Permission.APPROVAL_DECIDE, True),
        (Role.OPERATOR, Permission.APPROVAL_DECIDE_HIGH_RISK, False),
        (Role.OPERATOR, Permission.WORKFLOW_WRITE, False),
        (Role.OPERATOR, Permission.MEMBER_MANAGE, False),
        (Role.ADMIN, Permission.APPROVAL_DECIDE_HIGH_RISK, True),
        (Role.ADMIN, Permission.WORKFLOW_DEPLOY, True),
        (Role.ADMIN, Permission.ORG_DELETE, False),
        (Role.OWNER, Permission.ORG_DELETE, True),
    ],
)
def test_permission_matrix(role: Role, permission: Permission, expected: bool) -> None:
    assert has_permission(role, permission) is expected


def test_owner_has_every_permission() -> None:
    assert ROLE_PERMISSIONS[Role.OWNER] == frozenset(Permission)


@pytest.mark.parametrize(
    ("actor", "current", "new", "expected"),
    [
        (Role.OWNER, Role.VIEWER, Role.OWNER, True),
        (Role.OWNER, Role.OWNER, Role.ADMIN, True),
        (Role.ADMIN, Role.VIEWER, Role.OPERATOR, True),
        (Role.ADMIN, Role.VIEWER, Role.ADMIN, True),
        (Role.ADMIN, Role.VIEWER, Role.OWNER, False),  # cannot grant above own rank
        (Role.ADMIN, Role.OWNER, Role.VIEWER, False),  # cannot demote a higher rank
        (Role.OPERATOR, Role.VIEWER, Role.VIEWER, False),  # lacks member:manage
        (Role.VIEWER, None, Role.VIEWER, False),
        (Role.ADMIN, None, Role.ADMIN, True),  # invitations
        (Role.ADMIN, None, Role.OWNER, False),
    ],
)
def test_can_assign_role(actor: Role, current: Role | None, new: Role, expected: bool) -> None:
    assert can_assign_role(actor, current, new) is expected
