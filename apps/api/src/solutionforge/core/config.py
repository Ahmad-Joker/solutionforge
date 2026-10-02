"""Application settings, loaded from environment variables prefixed with ``SF_``."""

from __future__ import annotations

import secrets
from enum import StrEnum
from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


_SSL_MODES = {"require", "verify-ca", "verify-full", "prefer", "allow", "disable"}


def normalize_database_url(url: str) -> str:
    """Accept the connection strings hosting providers hand out (Neon, Supabase, Render):

    - ``postgres://`` / ``postgresql://`` → the async driver ``postgresql+asyncpg://``;
    - libpq's ``sslmode=…`` → asyncpg's ``ssl=…``;
    - ``channel_binding`` (libpq-only) is dropped — asyncpg still authenticates with SCRAM.
    """
    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+asyncpg"
    if not scheme.startswith("postgresql"):
        return url
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "channel_binding":
            continue
        if key == "sslmode" and scheme == "postgresql+asyncpg" and value in _SSL_MODES:
            key = "ssl"
        query.append((key, value))
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SF_",
        env_file=".env",
        extra="ignore",
        # Validation errors must never echo values: URLs and keys carry secrets.
        hide_input_in_errors=True,
    )

    environment: Environment = Environment.DEV
    app_name: str = "SolutionForge"
    log_level: str = "INFO"
    log_json: bool = True

    database_url: str = (
        "postgresql+asyncpg://solutionforge:solutionforge@localhost:5432/solutionforge"
    )
    # Optional: the database password on its own. When set, it is inserted into
    # database_url (which can then omit it), so the secret can be pasted into its own field
    # instead of being spliced into a long connection string by hand.
    database_password: SecretStr | None = None
    database_echo: bool = False
    # Pool per process. Free/serverless Postgres tiers have low connection limits.
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=20, ge=0, le=100)
    # Fail fast when the pool is exhausted (503 + Retry-After) instead of queueing for 30 s.
    database_pool_timeout_seconds: float = Field(default=10, gt=0, le=60)

    # JWT. Required outside dev/test; see ``_require_secrets``.
    jwt_secret: SecretStr | None = None
    jwt_issuer: str = "solutionforge"
    jwt_audience: str = "solutionforge-api"
    access_token_ttl_seconds: int = Field(default=15 * 60, ge=60, le=24 * 3600)
    refresh_token_ttl_seconds: int = Field(default=14 * 24 * 3600, ge=3600)
    invitation_ttl_seconds: int = Field(default=7 * 24 * 3600, ge=3600)

    worker_concurrency: int = Field(default=4, ge=1, le=64)
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    max_execution_input_bytes: int = Field(default=256 * 1024, ge=1024)

    # LLM. The mock provider needs no credentials; Anthropic is enabled iff a key is set.
    llm_enable_mock: bool = True
    anthropic_api_key: SecretStr | None = None
    llm_price_overrides: dict[str, dict[str, str]] = Field(default_factory=dict)
    llm_breaker_failure_threshold: int = Field(default=5, ge=1, le=100)
    llm_breaker_recovery_seconds: float = Field(default=30.0, gt=0, le=3600)

    # Connector credential encryption: comma-separated Fernet keys, first = primary.
    # Required outside dev/test (dev generates an ephemeral key: stored creds won't survive
    # a restart, which is acceptable locally and never silently used in production).
    credentials_keys: SecretStr | None = None

    # Shared rate-limit state across API replicas. Unset = in-memory (per process).
    redis_url: SecretStr | None = None
    rate_limit_enabled: bool = True

    password_hash_profile: Literal["standard", "fast-insecure-test"] = "standard"  # noqa: S105
    # Run the worker loops inside the API process (single-container demos, E2E tests).
    embedded_worker: bool = False

    # Observability. Tracing is off unless an OTLP/HTTP endpoint is set (e.g. a collector or
    # Jaeger at http://jaeger:4318). /metrics needs this bearer token; without one it is only
    # served in dev/test. The worker serves its own metrics on worker_metrics_port if set.
    otel_exporter_otlp_endpoint: str | None = None
    metrics_token: SecretStr | None = None
    worker_metrics_port: int | None = Field(default=None, ge=1024, le=65535)

    # Hard cap on request bodies, enforced while streaming (documents are ≤1M chars; JSON
    # escaping can expand that, hence the headroom).
    max_request_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)

    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalize_database_url(cls, v: object) -> object:
        if not isinstance(v, str):
            return v
        url = normalize_database_url(v.strip())  # pasted values often carry a newline
        if not url.startswith(("postgresql+asyncpg://", "sqlite")):
            # Name the scheme only: the URL may embed a password.
            scheme = url.split("://", 1)[0] if "://" in url else "(none)"
            raise ValueError(
                f"SF_DATABASE_URL must be a PostgreSQL connection string "
                f"(postgresql://user:password@host/db), not a '{scheme}' URL"
            )
        return url

    @field_validator(
        "jwt_secret",
        "anthropic_api_key",
        "credentials_keys",
        "redis_url",
        "metrics_token",
        "database_password",
        "otel_exporter_otlp_endpoint",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        # `SF_JWT_SECRET=` in an env file means "not configured", not "an empty secret".
        return None if isinstance(v, str) and not v.strip() else v

    @model_validator(mode="after")
    def _apply_database_password(self) -> Settings:
        if self.database_password is not None and self.database_url.startswith("postgresql"):
            from sqlalchemy.engine import make_url

            url = make_url(self.database_url).set(
                password=self.database_password.get_secret_value().strip()
            )
            self.database_url = url.render_as_string(hide_password=False)
        return self

    @model_validator(mode="after")
    def _require_secrets(self) -> Settings:
        if self.jwt_secret is None:
            if self.environment in (Environment.STAGING, Environment.PRODUCTION):
                raise ValueError("SF_JWT_SECRET must be set outside dev/test environments")
            # Ephemeral per-process secret: tokens do not survive restarts, which is
            # acceptable for local development and tests and never silently used in prod.
            self.jwt_secret = SecretStr(secrets.token_urlsafe(48))
        if len(self.jwt_secret.get_secret_value()) < 32:
            raise ValueError("SF_JWT_SECRET must be at least 32 characters")
        if self.password_hash_profile != "standard" and self.environment != Environment.TEST:  # noqa: S105
            raise ValueError("fast password hashing is only allowed when SF_ENVIRONMENT=test")
        if self.credentials_keys is None:
            if self.environment in (Environment.STAGING, Environment.PRODUCTION):
                raise ValueError("SF_CREDENTIALS_KEYS must be set outside dev/test environments")
            from cryptography.fernet import Fernet

            self.credentials_keys = SecretStr(Fernet.generate_key().decode())
        return self

    @property
    def credentials_key_list(self) -> list[str]:
        assert self.credentials_keys is not None
        return [k.strip() for k in self.credentials_keys.get_secret_value().split(",") if k.strip()]

    @property
    def jwt_secret_value(self) -> str:
        assert self.jwt_secret is not None  # guaranteed by validator
        return self.jwt_secret.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    return Settings()
