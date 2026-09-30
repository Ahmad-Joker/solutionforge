"""Application settings, loaded from environment variables prefixed with ``SF_``."""

from __future__ import annotations

import secrets
from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
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

    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

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
        return self

    @property
    def jwt_secret_value(self) -> str:
        assert self.jwt_secret is not None  # guaranteed by validator
        return self.jwt_secret.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    return Settings()
