"""Argon2id password hashing.

``standard`` uses argon2-cffi's recommended parameters (~100 ms per hash on a laptop: slow on
purpose). ``fast-insecure-test`` exists only to keep the test suite fast; settings refuse it
outside the ``test`` environment.
"""

from __future__ import annotations

from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

Profile = Literal["standard", "fast-insecure-test"]


_hasher = PasswordHasher()
# Verifying against a dummy hash when a user doesn't exist keeps login latency equal for
# "unknown email" and "wrong password" (no user enumeration by timing).
_dummy_hash = _hasher.hash("solutionforge-timing-equalizer")


def configure(profile: Profile) -> None:
    global _hasher, _dummy_hash
    _hasher = (
        PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
        if profile == "fast-insecure-test"
        else PasswordHasher()
    )
    _dummy_hash = _hasher.hash("solutionforge-timing-equalizer")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _dummy_hash, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
