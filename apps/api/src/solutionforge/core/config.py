"""Application settings, loaded from environment variables prefixed with ``SF_``."""

from __future__ import annotations

import secrets
from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SF_", env_file=".env", extra="ignore")

    environment: Environment = Environment.DEV
    app_name: str = "SolutionForge"
    log_level: str = "INFO"
    log_json: bool = True

    database_url: str = (
        "postgresql+asyncpg://solutionforge:solutionforge@localhost:5432/solutionforge"
    )
    database_echo: bool = False

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

    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    @field_validator("jwt_secret", "anthropic_api_key", "credentials_keys", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        # `SF_JWT_SECRET=` in an env file means "not configured", not "an empty secret".
        return None if isinstance(v, str) and not v.strip() else v

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
