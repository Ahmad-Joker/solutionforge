"""FastAPI dependencies: settings, DB session, current user, tenant context."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Path, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.core.config import Settings
from solutionforge.core.errors import AuthenticationFailed, TooManyRequests
from solutionforge.core.logging import get_logger
from solutionforge.db.tenancy import TenantContext
from solutionforge.domain.identity import User
from solutionforge.security.ratelimit import Limit, RateLimiter
from solutionforge.security.tokens import decode_access_token
from solutionforge.services import auth_service, org_service
from solutionforge.services.audit_service import RequestMeta
from solutionforge.workflows.registry import StepRegistry

_bearer = HTTPBearer(auto_error=False)
log = get_logger(__name__)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_registry(request: Request) -> StepRegistry:
    return request.app.state.step_registry  # type: ignore[no-any-return]


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


def get_request_meta(request: Request) -> RequestMeta:
    return RequestMeta(
        ip_address=request.client.host if request.client else None,
        request_id=getattr(request.state, "request_id", None),
    )


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
RequestMetaDep = Annotated[RequestMeta, Depends(get_request_meta)]
RegistryDep = Annotated[StepRegistry, Depends(get_registry)]


async def get_current_user(
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationFailed("Missing bearer token")
    user_id = decode_access_token(credentials.credentials, settings)
    return await auth_service.get_active_user(session, user_id)


CurrentUserDep = Annotated[User, Depends(get_current_user)]


async def get_tenant(
    session: SessionDep,
    user: CurrentUserDep,
    org_id: Annotated[uuid.UUID, Path(description="Organization ID")],
) -> TenantContext:
    return await org_service.resolve_tenant(session, organization_id=org_id, user_id=user.id)


TenantDep = Annotated[TenantContext, Depends(get_tenant)]


async def enforce_rate_limit(request: Request, limit: Limit, key: str) -> None:
    """Raise 429 when ``key`` exceeded ``limit``. Called inside routes (keys may depend on
    the request body, e.g. the login email)."""
    settings: Settings = request.app.state.settings
    if not settings.rate_limit_enabled:
        return
    limiter: RateLimiter = request.app.state.rate_limiter
    decision = await limiter.hit(limit, key)
    if not decision.allowed:
        log.warning("rate_limited", limit=limit.name, path=request.url.path)
        raise TooManyRequests(
            "Too many requests; try again later", retry_after_seconds=decision.retry_after_seconds
        )


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
