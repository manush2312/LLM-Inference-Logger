"""Application error taxonomy.

Errors carry their own HTTP status and a stable machine-readable `code`, so the
API layer never has to translate exception types into responses by hand and the
frontend can branch on `code` instead of parsing prose.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for every error this application raises deliberately."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def to_payload(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, **self.context}}


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"


class ProviderNotConfiguredError(AppError):
    """A provider was requested but has no credentials / is not registered."""

    status_code = 400
    code = "provider_not_configured"


class ProviderError(AppError):
    """The upstream LLM provider failed. Wraps the vendor SDK's exception.

    Kept distinct from `AppError` so instrumentation can attribute failures to
    the provider rather than to our own code.
    """

    status_code = 502
    code = "provider_error"

    def __init__(self, message: str, *, provider: str, retryable: bool = False, **ctx: Any):
        super().__init__(message, provider=provider, retryable=retryable, **ctx)
        self.provider = provider
        self.retryable = retryable
