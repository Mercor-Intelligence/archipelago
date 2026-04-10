"""Error types and handling for offline provider.

This module defines error codes and helper functions for generating
standardized error responses in offline mode.
"""

from enum import Enum
from typing import Any


class OfflineErrorCode(str, Enum):
    """Error codes for offline mode failures."""

    SYMBOL_NOT_AVAILABLE = "SYMBOL_NOT_AVAILABLE"
    DATABASE_NOT_INITIALIZED = "DATABASE_NOT_INITIALIZED"
    DATABASE_ERROR = "DATABASE_ERROR"
    DATA_NOT_AVAILABLE = "DATA_NOT_AVAILABLE"
    NOT_SUPPORTED = "NOT_SUPPORTED"


def make_error_response(
    code: OfflineErrorCode,
    message: str,
    suggestion: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Create a standardized error response.

    Args:
        code: The error code from OfflineErrorCode enum
        message: Human-readable error message
        suggestion: Optional actionable guidance for the user
        **extra: Additional fields to include in the response

    Returns:
        A dictionary with error information structured consistently.
    """
    error: dict[str, Any] = {
        "code": code.value,
        "message": message,
    }
    if suggestion:
        error["suggestion"] = suggestion

    return {
        "data": None,
        "error": error,
        **extra,
    }


def symbol_not_available(symbol: str, available_symbols: list[str] | None = None) -> dict[str, Any]:
    """Create error response for unavailable symbol.

    Args:
        symbol: The requested symbol that is not available
        available_symbols: Optional list of available symbols for suggestion

    Returns:
        Standardized error response for symbol not available.
    """
    suggestion = "Run the download script to add this symbol to your offline database."
    if available_symbols:
        sample = available_symbols[:5]
        suggestion = f"Available symbols include: {', '.join(sample)}. {suggestion}"

    return make_error_response(
        code=OfflineErrorCode.SYMBOL_NOT_AVAILABLE,
        message=f"Symbol '{symbol}' not available in offline mode",
        suggestion=suggestion,
        symbol=symbol,
    )


def data_not_available(
    data_type: str,
    symbol: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Create error response for unavailable data.

    Args:
        data_type: The type of data that is not available
        symbol: Optional symbol the data was requested for
        **extra: Additional fields to include in the response

    Returns:
        Standardized error response for data not available.
    """
    if symbol:
        message = f"No {data_type} data available for {symbol}"
    else:
        message = f"No {data_type} data available"

    return make_error_response(
        code=OfflineErrorCode.DATA_NOT_AVAILABLE,
        message=message,
        suggestion="Run the download script to fetch this data.",
        **extra,
    )


def database_not_initialized() -> dict[str, Any]:
    """Create error response for uninitialized database.

    Returns:
        Standardized error response for database not initialized.
    """
    return make_error_response(
        code=OfflineErrorCode.DATABASE_NOT_INITIALIZED,
        message="Offline database not found or not initialized",
        suggestion="Run: uv run python -m scripts.download_fixtures",
    )


def database_error(error_msg: str) -> dict[str, Any]:
    """Create error response for database errors.

    Args:
        error_msg: The original error message from the database

    Returns:
        Standardized error response for database errors.
    """
    return make_error_response(
        code=OfflineErrorCode.DATABASE_ERROR,
        message=f"Database error: {error_msg}",
        suggestion="Consider re-downloading fixtures or checking database integrity.",
    )


def not_supported(feature: str) -> dict[str, Any]:
    """Create error response for unsupported features.

    Args:
        feature: The feature that is not supported in offline mode

    Returns:
        Standardized error response for unsupported features.
    """
    return make_error_response(
        code=OfflineErrorCode.NOT_SUPPORTED,
        message=f"{feature} is not supported in offline mode",
        suggestion="Use online mode with an API key for this feature.",
    )
