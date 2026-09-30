"""Deterministic role-based access control.

Authorization decisions are pure functions of (role, permission). Nothing here consults an
LLM, a prompt, or request-supplied data — that is a core design invariant (see
docs/adr/0005-deterministic-authorization.md).
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


# Higher rank = more privilege. Used for "may not grant above your own level" rules.
ROLE_RANK: dict[Role, int] = {Role.VIEWER: 10, Role.OPERATOR: 20, Role.ADMIN: 30, Role.OWNER: 40}


class Permission(StrEnum):
    ORG_READ = "org:read"
    ORG_MANAGE = "org:manage"
    ORG_DELETE = "org:delete"

    MEMBER_READ = "member:read"
    MEMBER_MANAGE = "member:manage"

    WORKFLOW_READ = "workflow:read"
    WORKFLOW_WRITE = "workflow:write"
    WORKFLOW_EXECUTE = "workflow:execute"
    WORKFLOW_DEPLOY = "workflow:deploy"

    TOOL_READ = "tool:read"
    TOOL_MANAGE = "tool:manage"

    KNOWLEDGE_READ = "knowledge:read"
    KNOWLEDGE_WRITE = "knowledge:write"

    APPROVAL_READ = "approval:read"
    APPROVAL_DECIDE = "approval:decide"
    APPROVAL_DECIDE_HIGH_RISK = "approval:decide_high_risk"

    EVALUATION_READ = "evaluation:read"
    EVALUATION_RUN = "evaluation:run"

    AUDIT_READ = "audit:read"
    USAGE_READ = "usage:read"


_VIEWER = frozenset(
    {
        Permission.ORG_READ,
        Permission.MEMBER_READ,
        Permission.WORKFLOW_READ,
        Permission.TOOL_READ,
        Permission.KNOWLEDGE_READ,
        Permission.APPROVAL_READ,
        Permission.EVALUATION_READ,
    }
)
_OPERATOR = _VIEWER | {
    Permission.WORKFLOW_EXECUTE,
    Permission.APPROVAL_DECIDE,
    Permission.EVALUATION_RUN,
    Permission.USAGE_READ,
}
_ADMIN = _OPERATOR | {
    Permission.ORG_MANAGE,
    Permission.MEMBER_MANAGE,
    Permission.WORKFLOW_WRITE,
    Permission.WORKFLOW_DEPLOY,
    Permission.TOOL_MANAGE,
    Permission.KNOWLEDGE_WRITE,
    Permission.APPROVAL_DECIDE_HIGH_RISK,
    Permission.AUDIT_READ,
}
_OWNER = _ADMIN | {Permission.ORG_DELETE}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: _VIEWER,
    Role.OPERATOR: frozenset(_OPERATOR),
    Role.ADMIN: frozenset(_ADMIN),
    Role.OWNER: frozenset(_OWNER),
}


def has_permission(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]


def can_assign_role(actor: Role, target_current: Role | None, target_new: Role) -> bool:
    """Whether ``actor`` may move a member from ``target_current`` to ``target_new``.

    - Requires MEMBER_MANAGE.
    - Nobody can grant a role above their own.
    - Only owners can modify an owner (ADMIN cannot demote an OWNER).
    Last-owner protection needs database state and lives in the membership service.
    """
    if not has_permission(actor, Permission.MEMBER_MANAGE):
        return False
    if ROLE_RANK[target_new] > ROLE_RANK[actor]:
        return False
    return not (target_current is not None and ROLE_RANK[target_current] > ROLE_RANK[actor])
