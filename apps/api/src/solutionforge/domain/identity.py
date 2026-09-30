"""Identity and tenancy entities: users, organizations, memberships, tokens, invitations."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from solutionforge.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from solutionforge.security.rbac import Role

RoleColumn = sa.Enum(
    Role,
    name="member_role",
    values_callable=lambda e: [m.value for m in e],
    native_enum=False,
    length=16,
    validate_strings=True,
)


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(sa.String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(nullable=True)

    memberships: Mapped[list[Membership]] = relationship(back_populates="user")


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    slug: Mapped[str] = mapped_column(sa.String(63), unique=True, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="organization", cascade="all, delete-orphan", passive_deletes=True
    )


class Membership(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (sa.UniqueConstraint("organization_id", "user_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[Role] = mapped_column(RoleColumn, nullable=False)

    # innerjoin: user_id is NOT NULL, and PostgreSQL rejects FOR UPDATE on the nullable side
    # of the outer join a default joined eager load would produce.
    user: Mapped[User] = relationship(back_populates="memberships", lazy="joined", innerjoin=True)
    organization: Mapped[Organization] = relationship(back_populates="memberships")


class RefreshToken(UUIDPrimaryKeyMixin, Base):
    """Opaque refresh tokens, stored only as SHA-256 hashes.

    Tokens rotate on every use. All tokens from one login share a ``family_id``; presenting
    an already-rotated token revokes the entire family (token theft detection).
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    family_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)


class Invitation(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "invitations"

    email: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    role: Mapped[Role] = mapped_column(RoleColumn, nullable=False)
    token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    accepted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
