from __future__ import annotations

from datetime import datetime

import pytest

from solutionforge.db.base import UTCDateTime
from solutionforge.services.audit_service import REDACTED, sanitize_metadata
from solutionforge.services.org_service import SLUG_RE, slugify


def test_sanitize_redacts_nested_secret_keys() -> None:
    out = sanitize_metadata(
        {
            "email": "a@b.co",
            "password": "hunter2",
            "nested": {"API_KEY": "sk-123", "ok": 1, "list": [{"refresh_token": "x"}]},
            "Authorization": "Bearer abc",
        }
    )
    assert out == {
        "email": "a@b.co",
        "password": REDACTED,
        "nested": {"API_KEY": REDACTED, "ok": 1, "list": [{"refresh_token": REDACTED}]},
        "Authorization": REDACTED,
    }


def test_sanitize_bounds_depth_and_stringifies_unknown_types() -> None:
    deep: dict[str, object] = {}
    cur = deep
    for _ in range(20):
        cur["x"] = {}
        cur = cur["x"]  # type: ignore[assignment]
    assert "[TRUNCATED]" in repr(sanitize_metadata(deep))
    assert sanitize_metadata({"obj": object()})["obj"].startswith("<object")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Acme Corp", "acme-corp"),
        ("  --Hello__World!! ", "hello-world"),
        ("A", "org-a"),
        ("", "org"),
    ],
)
def test_slugify(name: str, expected: str) -> None:
    assert slugify(name) == expected
    assert SLUG_RE.match(slugify(name))


def test_utc_datetime_rejects_naive() -> None:
    with pytest.raises(ValueError, match="naive"):
        UTCDateTime().process_bind_param(datetime(2026, 1, 1), dialect=None)  # type: ignore[arg-type]


def test_owner_lock_query_is_valid_for_postgres() -> None:
    """Regression: PostgreSQL rejects FOR UPDATE on the nullable side of an outer join,
    which a default joined eager load of Membership.user produced. SQLite hides this."""
    import uuid

    from sqlalchemy.dialects import postgresql

    from solutionforge.db.tenancy import TenantContext
    from solutionforge.security.rbac import Role
    from solutionforge.services.org_service import owners_for_update

    ctx = TenantContext(organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role=Role.OWNER)
    sql = str(owners_for_update(ctx).compile(dialect=postgresql.dialect()))
    assert "LEFT OUTER JOIN" not in sql
    assert "FOR UPDATE OF memberships" in sql


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (  # Neon's dashboard string
            "postgresql://u:p@ep-x-123.eu-central-1.aws.neon.tech/neondb"
            "?sslmode=require&channel_binding=require",
            "postgresql+asyncpg://u:p@ep-x-123.eu-central-1.aws.neon.tech/neondb?ssl=require",
        ),
        ("postgres://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),  # Heroku/Render style
        ("postgresql+asyncpg://u:p@h/db?ssl=require", "postgresql+asyncpg://u:p@h/db?ssl=require"),
        (  # percent-encoded credentials and unrelated params survive
            "postgresql://u:p%40ss%20w@h/db?sslmode=disable&application_name=sf",
            "postgresql+asyncpg://u:p%40ss%20w@h/db?ssl=disable&application_name=sf",
        ),
        ("sqlite+aiosqlite:///tmp/x.db", "sqlite+aiosqlite:///tmp/x.db"),
    ],
)
def test_database_urls_from_hosting_providers_are_normalized(given: str, expected: str) -> None:
    from solutionforge.core.config import Settings, normalize_database_url

    assert normalize_database_url(given) == expected
    assert Settings(database_url=given).database_url == expected


@pytest.mark.parametrize(
    "bad",
    [
        "https://user:S3CRETPW@ep-x.us-east-1.aws.neon.tech/neondb",  # Neon HTTP endpoint
        "https://solutionforge-api.onrender.com",  # an app URL pasted by mistake
        "mysql://user:S3CRETPW@host/db",
        "user:S3CRETPW@host/db",
    ],
)
def test_wrong_database_url_fails_clearly_without_echoing_secrets(bad: str) -> None:
    from pydantic import ValidationError

    from solutionforge.core.config import Settings

    with pytest.raises(ValidationError) as err:
        Settings(database_url=bad)
    message = str(err.value)
    assert "must be a PostgreSQL connection string" in message
    assert "S3CRETPW" not in message and "neon.tech" not in message


def test_database_password_can_be_supplied_separately() -> None:
    from sqlalchemy.engine import make_url

    from solutionforge.core.config import Settings

    s = Settings(
        database_url="  postgresql://owner@ep-x.us-east-1.aws.neon.tech/neondb?sslmode=require\n",
        database_password=" p@ss/w:rd#1\n",
    )
    url = make_url(s.database_url)
    assert (url.drivername, url.username, url.host, url.database) == (
        "postgresql+asyncpg",
        "owner",
        "ep-x.us-east-1.aws.neon.tech",
        "neondb",
    )
    assert url.password == "p@ss/w:rd#1"
    assert dict(url.query) == {"ssl": "require"}
    # Unset (or blank) leaves a password that is already in the URL alone.
    unchanged = Settings(database_url="postgresql://u:old@h/db", database_password="")
    assert unchanged.database_url == "postgresql+asyncpg://u:old@h/db"
