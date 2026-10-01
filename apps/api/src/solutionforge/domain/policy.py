"""Per-organization tool policy (validated by ``tools.policy.ToolPolicyConfig``)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from solutionforge.core.clock import utcnow
from solutionforge.db.base import Base, JSONType, TenantScopedMixin, UUIDPrimaryKeyMixin


class OrgPolicy(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "org_policies"
    __table_args__ = (sa.UniqueConstraint("organization_id"),)

    tool_policy: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)
