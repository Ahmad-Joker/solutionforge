"""Structured domain exceptions.

Services raise these; the API layer maps them to HTTP responses in exactly one place
(``solutionforge.api.errors``). Messages are safe to show to clients: never put secrets,
stack traces or other tenants' identifiers in them.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    code: str = "domain_error"
    status_code: int = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationFailed(DomainError):
    code = "validation_failed"
    status_code = 422


class AuthenticationFailed(DomainError):
    code = "authentication_failed"
    status_code = 401


class PermissionDenied(DomainError):
    code = "permission_denied"
    status_code = 403


class NotFound(DomainError):
    """Also used for resources in other tenants, so existence is never leaked."""

    code = "not_found"
    status_code = 404


class Conflict(DomainError):
    code = "conflict"
    status_code = 409
