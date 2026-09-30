"""Service-layer authorization guard.

Checks live in services (not only in HTTP routes) because workflows, workers and
evaluation runs call services directly. Every entry point gets the same enforcement.
"""

from __future__ import annotations

from solutionforge.core.errors import PermissionDenied
from solutionforge.db.tenancy import TenantContext
from solutionforge.security.rbac import Permission


def ensure(ctx: TenantContext, permission: Permission) -> None:
    if not ctx.can(permission):
        raise PermissionDenied(
            f"Role '{ctx.role.value}' lacks permission '{permission.value}'",
            details={"required_permission": permission.value},
        )
