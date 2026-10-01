from __future__ import annotations

from fastapi import APIRouter, Request, status

from solutionforge.api.deps import (
    CurrentUserDep,
    RequestMetaDep,
    SessionDep,
    SettingsDep,
    client_ip,
    enforce_rate_limit,
)
from solutionforge.schemas.identity import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from solutionforge.security.ratelimit import (
    LOGIN_PER_ACCOUNT,
    LOGIN_PER_IP,
    REFRESH_PER_IP,
    REGISTER_PER_IP,
)
from solutionforge.services import auth_service
from solutionforge.services.auth_service import TokenPair

router = APIRouter(prefix="/auth", tags=["auth"])


def _tokens(pair: TokenPair) -> TokenResponse:
    return TokenResponse(
        access_token=pair.access_token,
        access_expires_at=pair.access_expires_at,
        refresh_token=pair.refresh_token,
        refresh_expires_at=pair.refresh_expires_at,
    )


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest, request: Request, session: SessionDep, meta: RequestMetaDep
) -> UserOut:
    await enforce_rate_limit(request, REGISTER_PER_IP, client_ip(request))
    user = await auth_service.register(
        session,
        email=body.email,
        password=body.password,
        display_name=body.display_name,
        request=meta,
    )
    return UserOut.model_validate(user)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    meta: RequestMetaDep,
) -> TokenResponse:
    # Per-IP stops spraying many accounts; per-account stops distributed guessing of one.
    await enforce_rate_limit(request, LOGIN_PER_IP, client_ip(request))
    await enforce_rate_limit(request, LOGIN_PER_ACCOUNT, auth_service.normalize_email(body.email))
    pair = await auth_service.login(
        session, settings, email=body.email, password=body.password, request=meta
    )
    return _tokens(pair)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    meta: RequestMetaDep,
) -> TokenResponse:
    await enforce_rate_limit(request, REFRESH_PER_IP, client_ip(request))
    pair = await auth_service.refresh(
        session, settings, refresh_token=body.refresh_token, request=meta
    )
    return _tokens(pair)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshRequest, session: SessionDep, meta: RequestMetaDep) -> None:
    await auth_service.logout(session, refresh_token=body.refresh_token, request=meta)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUserDep) -> UserOut:
    return UserOut.model_validate(user)
