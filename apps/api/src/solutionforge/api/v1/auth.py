from __future__ import annotations

from fastapi import APIRouter, status

from solutionforge.api.deps import CurrentUserDep, RequestMetaDep, SessionDep, SettingsDep
from solutionforge.schemas.identity import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserOut,
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
async def register(body: RegisterRequest, session: SessionDep, meta: RequestMetaDep) -> UserOut:
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
    body: LoginRequest, session: SessionDep, settings: SettingsDep, meta: RequestMetaDep
) -> TokenResponse:
    pair = await auth_service.login(
        session, settings, email=body.email, password=body.password, request=meta
    )
    return _tokens(pair)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest, session: SessionDep, settings: SettingsDep, meta: RequestMetaDep
) -> TokenResponse:
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
