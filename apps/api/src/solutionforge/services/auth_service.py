"""Registration, login, refresh-token rotation and logout."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.core.clock import utcnow
from solutionforge.core.config import Settings
from solutionforge.core.errors import AuthenticationFailed, Conflict
from solutionforge.domain.audit import AuditEventType
from solutionforge.domain.identity import RefreshToken, User
from solutionforge.security import passwords, tokens
from solutionforge.services import audit_service
from solutionforge.services.audit_service import RequestMeta

_INVALID_CREDENTIALS = "Invalid email or password"
_INVALID_REFRESH = "Invalid or expired refresh token"


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def register(
    session: AsyncSession, *, email: str, password: str, display_name: str, request: RequestMeta
) -> User:
    email = normalize_email(email)
    if await session.scalar(sa.select(User.id).where(User.email == email)) is not None:
        raise Conflict("An account with this email already exists")
    user = User(
        email=email, password_hash=passwords.hash_password(password), display_name=display_name
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:  # concurrent registration with the same email
        await session.rollback()
        raise Conflict("An account with this email already exists") from exc
    audit_service.record(
        session,
        event_type=AuditEventType.USER_REGISTERED,
        request=request,
        actor_user_id=user.id,
        resource_type="user",
        resource_id=user.id,
    )
    await session.commit()
    return user


async def login(
    session: AsyncSession, settings: Settings, *, email: str, password: str, request: RequestMeta
) -> TokenPair:
    email = normalize_email(email)
    user = await session.scalar(sa.select(User).where(User.email == email))
    # Always run a hash verification, even for unknown users (constant-ish timing).
    ok = passwords.verify_password(password, user.password_hash if user else None)
    if not ok or user is None or not user.is_active:
        audit_service.record(
            session,
            event_type=AuditEventType.LOGIN_FAILED,
            request=request,
            actor_user_id=user.id if user else None,
            # Never log the attempted password; the email is kept for brute-force forensics.
            metadata={"email": email},
        )
        await session.commit()
        raise AuthenticationFailed(_INVALID_CREDENTIALS)

    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(password)
    user.last_login_at = utcnow()
    pair = _issue_pair(session, settings, user.id, family_id=uuid.uuid4())
    audit_service.record(
        session,
        event_type=AuditEventType.LOGIN_SUCCEEDED,
        request=request,
        actor_user_id=user.id,
        resource_type="user",
        resource_id=user.id,
    )
    await session.commit()
    return pair


async def refresh(
    session: AsyncSession, settings: Settings, *, refresh_token: str, request: RequestMeta
) -> TokenPair:
    now = utcnow()
    token_hash = tokens.hash_opaque_token(refresh_token)
    stored = await session.scalar(
        sa.select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    if stored is None:
        raise AuthenticationFailed(_INVALID_REFRESH)

    if stored.revoked_at is not None:
        # A rotated token was replayed: assume theft and kill the whole login session.
        await _revoke_family(session, stored.family_id, now)
        audit_service.record(
            session,
            event_type=AuditEventType.REFRESH_TOKEN_REUSE,
            request=request,
            actor_user_id=stored.user_id,
            metadata={"family_id": str(stored.family_id)},
        )
        await session.commit()
        raise AuthenticationFailed(_INVALID_REFRESH)

    if stored.expires_at <= now:
        raise AuthenticationFailed(_INVALID_REFRESH)

    user = await session.get(User, stored.user_id)
    if user is None or not user.is_active:
        raise AuthenticationFailed(_INVALID_REFRESH)

    # Compare-and-set so two concurrent refreshes with the same token cannot both succeed.
    result = await session.execute(
        sa.update(RefreshToken)
        .where(RefreshToken.id == stored.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        raise AuthenticationFailed(_INVALID_REFRESH)

    pair = _issue_pair(session, settings, user.id, family_id=stored.family_id)
    await session.commit()
    return pair


async def logout(session: AsyncSession, *, refresh_token: str, request: RequestMeta) -> None:
    """Revoke the login session a refresh token belongs to (RFC 7009 semantics).

    Possession of the token is the credential, so this works even after the access token
    expired. Unknown tokens are a silent no-op: the endpoint is not a token-validity oracle.
    """
    token_hash = tokens.hash_opaque_token(refresh_token)
    stored = await session.scalar(
        sa.select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    if stored is None:
        return
    await _revoke_family(session, stored.family_id, utcnow())
    audit_service.record(
        session, event_type=AuditEventType.LOGOUT, request=request, actor_user_id=stored.user_id
    )
    await session.commit()


async def get_active_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthenticationFailed("Invalid access token")
    return user


def _issue_pair(
    session: AsyncSession, settings: Settings, user_id: uuid.UUID, *, family_id: uuid.UUID
) -> TokenPair:
    now = utcnow()
    access = tokens.issue_access_token(user_id, settings)
    raw_refresh = tokens.new_opaque_token()
    refresh_expires = now + timedelta(seconds=settings.refresh_token_ttl_seconds)
    session.add(
        RefreshToken(
            user_id=user_id,
            family_id=family_id,
            token_hash=tokens.hash_opaque_token(raw_refresh),
            created_at=now,
            expires_at=refresh_expires,
        )
    )
    return TokenPair(
        access_token=access.token,
        access_expires_at=access.expires_at,
        refresh_token=raw_refresh,
        refresh_expires_at=refresh_expires,
    )


async def _revoke_family(session: AsyncSession, family_id: uuid.UUID, now: datetime) -> None:
    await session.execute(
        sa.update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
