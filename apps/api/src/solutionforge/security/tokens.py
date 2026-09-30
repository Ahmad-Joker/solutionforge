"""Access tokens (short-lived JWT) and opaque secrets (refresh/invitation tokens).

Access tokens carry only the user identity. Organization and role are resolved from the
database on every request, so a role change or removal takes effect immediately rather
than when the token expires.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import jwt

from solutionforge.core.clock import utcnow
from solutionforge.core.config import Settings
from solutionforge.core.errors import AuthenticationFailed

_ALGORITHM = "HS256"
_ACCESS_TYPE = "access"


@dataclass(frozen=True, slots=True)
class IssuedAccessToken:
    token: str
    expires_at: datetime


def issue_access_token(user_id: uuid.UUID, settings: Settings) -> IssuedAccessToken:
    now = utcnow()
    expires_at = now + timedelta(seconds=settings.access_token_ttl_seconds)
    claims = {
        "sub": str(user_id),
        "typ": _ACCESS_TYPE,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(claims, settings.jwt_secret_value, algorithm=_ALGORITHM)
    return IssuedAccessToken(token=token, expires_at=expires_at)


def decode_access_token(token: str, settings: Settings) -> uuid.UUID:
    """Return the user id in a valid access token, or raise AuthenticationFailed."""
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret_value,
            algorithms=[_ALGORITHM],  # pinned: never trust the token's own "alg" header
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "typ", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationFailed("Access token expired") from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationFailed("Invalid access token") from exc
    if claims.get("typ") != _ACCESS_TYPE:
        raise AuthenticationFailed("Invalid access token")
    try:
        return uuid.UUID(claims["sub"])
    except (ValueError, TypeError) as exc:
        raise AuthenticationFailed("Invalid access token") from exc


def new_opaque_token() -> str:
    return secrets.token_urlsafe(32)


def hash_opaque_token(token: str) -> str:
    """Opaque tokens are high-entropy, so a fast unsalted hash is appropriate for lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
