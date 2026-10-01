"""Encryption at rest for connector credentials.

Fernet (AES-128-CBC + HMAC-SHA256, authenticated) with a key *ring*: the first key encrypts,
every key can decrypt, so keys rotate by prepending a new one and re-encrypting at leisure
(:meth:`CredentialCipher.rotate`). Plaintext credentials exist only in memory for the duration
of a tool call; the API never returns them.
"""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class CredentialError(Exception):
    pass


class CredentialCipher:
    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise ValueError("at least one credentials key is required")
        try:
            self._fernet = MultiFernet([Fernet(k.encode()) for k in keys])
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid credentials key (expected Fernet base64 key)") from exc

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()

    def encrypt(self, secret: dict[str, Any]) -> bytes:
        return self._fernet.encrypt(json.dumps(secret, sort_keys=True).encode())

    def decrypt(self, token: bytes) -> dict[str, Any]:
        try:
            value = json.loads(self._fernet.decrypt(token))
        except InvalidToken:
            raise CredentialError(
                "credentials cannot be decrypted (key missing or rotated out)"
            ) from None
        if not isinstance(value, dict):
            raise CredentialError("credentials payload is not an object")
        return value

    def rotate(self, token: bytes) -> bytes:
        """Re-encrypt under the current primary key."""
        return self._fernet.rotate(token)
