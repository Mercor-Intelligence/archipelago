"""Custom exception hierarchy for Xero online provider."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass
class XeroApiError(RuntimeError):
    """Base error for Xero provider failures."""

    message: str
    status_code: int | None = None
    details: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)


class XeroApiAuthenticationError(XeroApiError):
    """Raised for 401 authentication failures."""


class XeroApiPermissionError(XeroApiError):
    """Raised for 403 authorization failures."""


class XeroApiNotFoundError(XeroApiError):
    """Raised for 404 errors."""


class XeroApiValidationError(XeroApiError):
    """Raised for 400/422 validation issues."""


class XeroApiRateLimitError(XeroApiError):
    """Raised for 429 or client-side budget violations."""

    retry_after: float | None = None
    problem: str | None = None

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        details: Mapping[str, Any] | None = None,
        retry_after: float | None = None,
        problem: str | None = None,
    ):
        super().__init__(message, status_code=status_code, details=details)
        self.retry_after = retry_after
        self.problem = problem


class XeroApiClientError(XeroApiError):
    """Fallback for other 4xx responses."""


class XeroApiServerError(XeroApiError):
    """Raised for 5xx responses after retries."""


class XeroApiNetworkError(XeroApiError):
    """Raised for transport or timeout issues."""


__all__ = [
    "XeroApiError",
    "XeroApiAuthenticationError",
    "XeroApiPermissionError",
    "XeroApiNotFoundError",
    "XeroApiValidationError",
    "XeroApiRateLimitError",
    "XeroApiClientError",
    "XeroApiServerError",
    "XeroApiNetworkError",
]
