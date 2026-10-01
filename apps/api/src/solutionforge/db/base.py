"""Declarative base, portable column types and shared mixins."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

from solutionforge.core.clock import utcnow

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UTCDateTime(sa.TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend.

    PostgreSQL stores ``timestamptz``; SQLite (used for fast local tests) drops tzinfo,
    so we re-attach UTC on the way out and reject naive datetimes on the way in.
    """

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are not allowed; use solutionforge.core.clock")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: UTCDateTime(), uuid.UUID: sa.Uuid()}


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)


class TenantScopedMixin:
    """Marks a table as owned by exactly one organization.

    Every query against a tenant-scoped model must filter on ``organization_id``; use
    :func:`solutionforge.db.tenancy.scoped_select` rather than hand-writing the filter.
    """

    @declared_attr
    def organization_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(
            sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
        )


class Embedding(sa.TypeDecorator[list[float]]):
    """``vector(N)`` on PostgreSQL (pgvector, HNSW-indexable); a JSON array elsewhere.

    The SQLite fallback exists only so the fast local test loop can exercise the portable
    retrieval path; production retrieval runs in SQL on pgvector.
    """

    impl = sa.JSON
    cache_ok = True

    def __init__(self, dim: int) -> None:
        super().__init__()
        self.dim = dim

    def load_dialect_impl(self, dialect: Dialect) -> sa.types.TypeEngine[Any]:
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import Vector

            return dialect.type_descriptor(Vector(self.dim))
        return dialect.type_descriptor(sa.JSON())

    def process_bind_param(self, value: list[float] | None, dialect: Dialect) -> Any:
        if value is not None and len(value) != self.dim:
            raise ValueError(f"embedding has {len(value)} dims, column expects {self.dim}")
        return value

    def process_result_value(self, value: Any, dialect: Dialect) -> list[float] | None:
        if value is None:
            return None
        return [float(x) for x in value]


# Objects created by migrations for PostgreSQL only (HNSW/GIN indexes) are named with this
# prefix so the model-vs-migration drift check can ignore them.
PG_ONLY_PREFIX = "pgx_"


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    return not (type_ == "index" and name is not None and name.startswith(PG_ONLY_PREFIX))


def compare_type(
    context: Any,
    inspected_column: Any,
    metadata_column: Any,
    inspected_type: Any,
    metadata_type: Any,
) -> bool | None:
    """Alembic hook: an Embedding column matches pgvector's VECTOR / SQLite's JSON."""
    if isinstance(metadata_type, Embedding):
        return False
    return None  # default comparison
