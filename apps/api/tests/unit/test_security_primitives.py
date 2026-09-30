from __future__ import annotations

import base64
import json
import uuid
from datetime import timedelta

import jwt
import pytest

from solutionforge.core.clock import utcnow
from solutionforge.core.config import Environment, Settings
from solutionforge.core.errors import AuthenticationFailed
from solutionforge.security import passwords, tokens

SECRET = "unit-test-secret-that-is-at-least-32-chars"


@pytest.fixture
def settings() -> Settings:
    return Settings(environment=Environment.TEST, jwt_secret=SECRET)  # type: ignore[arg-type]


# ----------------------------------------------------------------- passwords


def test_password_roundtrip() -> None:
    h = passwords.hash_password("s3cure-passphrase!")
    assert h.startswith("$argon2id$")
    assert passwords.verify_password("s3cure-passphrase!", h)
    assert not passwords.verify_password("wrong", h)


def test_verify_against_missing_or_corrupt_hash_is_false() -> None:
    assert not passwords.verify_password("anything", None)
    assert not passwords.verify_password("solutionforge-timing-equalizer", None)  # dummy hash input
    assert not passwords.verify_password("anything", "not-a-hash")


# ----------------------------------------------------------------- access tokens


def test_access_token_roundtrip(settings: Settings) -> None:
    uid = uuid.uuid4()
    issued = tokens.issue_access_token(uid, settings)
    assert tokens.decode_access_token(issued.token, settings) == uid


def _claims(settings: Settings, **overrides: object) -> dict[str, object]:
    now = utcnow()
    base: dict[str, object] = {
        "sub": str(uuid.uuid4()),
        "typ": "access",
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": int((utcnow() - timedelta(seconds=5)).timestamp())},  # expired
        {"aud": "some-other-service"},
        {"iss": "evil-issuer"},
        {"typ": "refresh"},
        {"sub": "not-a-uuid"},
        {"sub": None},  # missing required claim
    ],
)
def test_invalid_claims_rejected(settings: Settings, overrides: dict[str, object]) -> None:
    token = jwt.encode(_claims(settings, **overrides), SECRET, algorithm="HS256")
    with pytest.raises(AuthenticationFailed):
        tokens.decode_access_token(token, settings)


def test_wrong_signing_key_rejected(settings: Settings) -> None:
    token = jwt.encode(
        _claims(settings), "another-secret-that-is-32-chars-long!!", algorithm="HS256"
    )
    with pytest.raises(AuthenticationFailed):
        tokens.decode_access_token(token, settings)


def test_alg_none_rejected(settings: Settings) -> None:
    def b64(d: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    token = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(_claims(settings))}."
    with pytest.raises(AuthenticationFailed):
        tokens.decode_access_token(token, settings)


def test_garbage_token_rejected(settings: Settings) -> None:
    with pytest.raises(AuthenticationFailed):
        tokens.decode_access_token("definitely.not.ajwt", settings)


def test_opaque_tokens_are_unique_and_hash_deterministically() -> None:
    a, b = tokens.new_opaque_token(), tokens.new_opaque_token()
    assert a != b and len(a) >= 40
    assert tokens.hash_opaque_token(a) == tokens.hash_opaque_token(a)
    assert tokens.hash_opaque_token(a) != a


# ----------------------------------------------------------------- config


def test_production_requires_jwt_secret() -> None:
    with pytest.raises(ValueError, match="SF_JWT_SECRET"):
        Settings(environment=Environment.PRODUCTION, jwt_secret=None)


def test_short_jwt_secret_rejected() -> None:
    with pytest.raises(ValueError, match="at least 32"):
        Settings(environment=Environment.DEV, jwt_secret="short")  # type: ignore[arg-type]


def test_dev_generates_ephemeral_secret() -> None:
    s = Settings(environment=Environment.DEV, jwt_secret=None)
    assert len(s.jwt_secret_value) >= 32


def test_blank_secrets_in_env_files_mean_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: `cp .env.example .env` left SF_JWT_SECRET= blank, which crashed startup,
    and a blank SF_ANTHROPIC_API_KEY= would have enabled the provider with an empty key."""
    monkeypatch.setenv("SF_JWT_SECRET", "")
    monkeypatch.setenv("SF_ANTHROPIC_API_KEY", "  ")
    s = Settings(environment=Environment.DEV)
    assert s.anthropic_api_key is None
    assert len(s.jwt_secret_value) >= 32  # dev generates an ephemeral one
    with pytest.raises(ValueError, match="SF_JWT_SECRET"):
        Settings(environment=Environment.PRODUCTION)
