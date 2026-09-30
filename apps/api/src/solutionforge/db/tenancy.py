"""Tenant-scoping helpers.

The rule: data belonging to an organization is only ever read through a
:class:`TenantContext`, which is produced by the API layer *after* verifying the caller's
membership. Services never accept a bare ``organization_id`` from request bodies.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from solutionforge.db.base import TenantScopedMixin
from solutionforge.security.rbac import Permission, Role, has_permission


@dataclass(frozen=True, slots=True)
class TenantContext:
    organization_id: uuid.UUID
    user_id: uuid.UUID
    role: Role

    def can(self, permission: Permission) -> bool:
        return has_permission(self.role, permission)


def scoped_select[M: TenantScopedMixin](
    model: type[M], ctx: TenantContext, *criteria: Any
) -> sa.Select[M]:
    """``SELECT model WHERE organization_id = ctx.organization_id AND <criteria>``."""
    return sa.select(model).where(model.organization_id == ctx.organization_id, *criteria)
