"""Error classification and structured error handling for EDGAR SEC tools.

Provides utilities to classify network/API exceptions into structured error
categories (CONNECTION_ERROR, TIMEOUT_ERROR, RATE_LIMITED, etc.) and format
them into user-friendly messages suitable for LLM consumption.
"""

import socket

import httpx
from loguru import logger

_CONNECTION_ERROR_PATTERNS = [
    "name resolution",
    "temporary failure",
    "connection attempts failed",
    "connect call failed",
    "nodename nor servname",
    "name or service not known",
    "network is unreachable",
    "no route to host",
    "connection refused",
    "connection reset",
    "broken pipe",
    "ssl: certificate",
    "ssl handshake",
]

_TIMEOUT_ERROR_PATTERNS = [
    "timed out",
    "timeout",
    "deadline exceeded",
    "read operation timed out",
]


def is_connection_error(exc: Exception) -> bool:
    """Check if an exception is a DNS/connection error."""
    if isinstance(exc, socket.gaierror | ConnectionError | OSError):
        if isinstance(exc, OSError) and not isinstance(exc, ConnectionError):
            error_str = str(exc).lower()
            return any(p in error_str for p in _CONNECTION_ERROR_PATTERNS)
        return True
    if isinstance(exc, httpx.ConnectError):
        return True
    error_str = str(exc).lower()
    return any(p in error_str for p in _CONNECTION_ERROR_PATTERNS)


def is_timeout_error(exc: Exception) -> bool:
    """Check if an exception is a timeout error."""
    if isinstance(exc, httpx.TimeoutException | TimeoutError):
        return True
    error_str = str(exc).lower()
    return any(p in error_str for p in _TIMEOUT_ERROR_PATTERNS)


def classify_error(exc: Exception) -> tuple[str, str, bool]:
    """Classify an exception into (error_type, user_message, is_retryable).

    Returns:
        Tuple of (error_type, user_friendly_message, should_retry)
    """
    if is_connection_error(exc):
        return (
            "CONNECTION_ERROR",
            "SEC EDGAR API is currently unreachable. Please retry.",
            True,
        )

    if is_timeout_error(exc):
        return (
            "TIMEOUT_ERROR",
            "SEC EDGAR API request timed out. The service may be slow — please retry.",
            True,
        )

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 404:
            return (
                "NOT_FOUND",
                "Resource not found on SEC EDGAR.",
                False,
            )
        if status == 429:
            return (
                "RATE_LIMITED",
                "SEC EDGAR rate limit exceeded. Please wait and retry.",
                True,
            )
        if status >= 500:
            return (
                "API_ERROR",
                f"SEC EDGAR server error (HTTP {status}). Please retry later.",
                True,
            )
        return (
            "API_ERROR",
            f"SEC EDGAR API error (HTTP {status}).",
            False,
        )

    if isinstance(exc, ValueError):
        return ("VALIDATION_ERROR", str(exc), False)

    return ("UNKNOWN_ERROR", str(exc), False)


def format_tool_error(exc: Exception, tool_name: str = "") -> str:
    """Format an exception into a structured, user-friendly error message.

    Returns a message like:
        "[CONNECTION_ERROR] SEC EDGAR API is currently unreachable. Please retry."
    """
    error_type, message, retryable = classify_error(exc)
    prefix = f"[{error_type}]"

    if tool_name:
        logger.warning(f"{tool_name}: {error_type} - {exc}")
    else:
        logger.warning(f"{error_type} - {exc}")

    parts = [prefix, message]
    if retryable and "retry" not in message.lower():
        parts.append("This is a transient error — please retry.")
    return " ".join(parts)
