"""
Error Models for Bloomberg BLPAPI Compliance

This module defines error models that match Bloomberg's BLPAPI schema.
All error structures follow the exact format specified in the Bloomberg Mock Spec.
"""

from enum import Enum

from pydantic import BaseModel, Field


class ErrorCategory(str, Enum):
    """Bloomberg error categories."""

    BAD_FIELD = "BAD_FIELD"
    BAD_SECURITY = "BAD_SECURITY"
    FIELD_NOT_APPLICABLE = "FIELD_NOT_APPLICABLE"
    INVALID_SECURITY = "INVALID_SECURITY"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    TIMEOUT = "TIMEOUT"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    INVALID_INTERVAL = "INVALID_INTERVAL"
    INVALID_DATE = "INVALID_DATE"
    UNKNOWN = "UNKNOWN"


class FieldException(BaseModel):
    """
    Field-level exception for unsupported or invalid fields.

    Example:
        {
            "fieldId": "TRADE_COUNT",
            "errorInfo": {
                "source": "bloomberg-emulator",
                "code": 9,
                "category": "BAD_FIELD",
                "message": "Field not available",
                "subcategory": "NOT_APPLICABLE_TO_HIST_DATA"
            }
        }
    """

    fieldId: str = Field(..., description="Bloomberg field mnemonic (e.g., 'TRADE_COUNT')")
    errorInfo: "ErrorInfo" = Field(..., description="Error details")


class ErrorInfo(BaseModel):
    """
    Detailed error information following Bloomberg BLPAPI format.
    """

    source: str = Field(default="bloomberg-emulator", description="Error source")
    code: int = Field(..., description="Bloomberg error code")
    category: ErrorCategory = Field(..., description="Error category")
    message: str = Field(..., description="Human-readable error message")
    subcategory: str | None = Field(default=None, description="Error subcategory")


class SecurityError(BaseModel):
    """
    Security-level error for invalid or unauthorized securities.

    Example:
        {
            "security": "INVALID US Equity",
            "errorInfo": {
                "source": "bloomberg-emulator",
                "code": 15,
                "category": "BAD_SECURITY",
                "message": "Unknown/Invalid securityInvalid security",
                "subcategory": "INVALID_SECURITY"
            }
        }
    """

    security: str = Field(..., description="Security identifier")
    errorInfo: ErrorInfo = Field(..., description="Error details")


class ResponseError(BaseModel):
    """
    Top-level response error for request failures.

    Example:
        {
            "requestId": "req-123",
            "errorInfo": {
                "source": "bloomberg-emulator",
                "code": 1,
                "category": "TIMEOUT",
                "message": "Request timeout after 30 seconds"
            }
        }
    """

    requestId: str = Field(..., description="Request identifier")
    errorInfo: ErrorInfo = Field(..., description="Error details")


# Error code mappings (Bloomberg-compatible)
ERROR_CODES = {
    "TIMEOUT": 1,
    "CONNECTION_ERROR": 2,
    "NETWORK_ERROR": 2,
    "RATE_LIMIT_EXCEEDED": 3,
    "NOT_AUTHORIZED": 4,
    "INVALID_REQUEST": 5,
    "INVALID_DATE": 6,
    "INVALID_INTERVAL": 7,
    "INVALID_SECURITY": 8,
    "BAD_FIELD": 9,
    "FIELD_NOT_APPLICABLE": 10,
    "UNKNOWN": 99,
}


def create_field_exception(
    field_id: str, reason: str, category: ErrorCategory = ErrorCategory.BAD_FIELD
) -> FieldException:
    """Helper to create field exceptions."""
    return FieldException(
        fieldId=field_id,
        errorInfo=ErrorInfo(
            source="bloomberg-emulator",
            code=ERROR_CODES.get(category.value, ERROR_CODES["UNKNOWN"]),
            category=category,
            message=reason,
            subcategory=(
                "NOT_APPLICABLE_TO_HIST_DATA"
                if category == ErrorCategory.FIELD_NOT_APPLICABLE
                else None
            ),
        ),
    )


def create_security_error(security: str, reason: str) -> SecurityError:
    """Helper to create security errors."""
    return SecurityError(
        security=security,
        errorInfo=ErrorInfo(
            source="bloomberg-emulator",
            code=ERROR_CODES["INVALID_SECURITY"],
            category=ErrorCategory.INVALID_SECURITY,
            message=reason,
            subcategory="INVALID_SECURITY",
        ),
    )


def create_response_error(request_id: str, error_type: str, message: str) -> ResponseError:
    """Helper to create response errors."""
    return ResponseError(
        requestId=request_id,
        errorInfo=ErrorInfo(
            source="bloomberg-emulator",
            code=ERROR_CODES.get(error_type, ERROR_CODES["UNKNOWN"]),
            category=(
                ErrorCategory[error_type]
                if error_type in ErrorCategory.__members__
                else ErrorCategory.UNKNOWN
            ),
            message=message,
        ),
    )


def is_connection_error(exc: BaseException) -> bool:
    """Check if an exception is a network/DNS/connectivity error."""
    connection_types: tuple[type[BaseException], ...] = (ConnectionError, OSError)
    try:
        import httpx

        connection_types = (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.NetworkError,
            ConnectionError,
            OSError,
        )
    except ImportError:
        pass
    if isinstance(exc, connection_types):
        return True
    msg = str(exc).lower()
    connection_patterns = [
        "name resolution",
        "temporary failure in name resolution",
        "connection attempts failed",
        "all connection attempts failed",
        "connect timeout",
        "network is unreachable",
        "connection refused",
        "dns",
        "errno -3",
        "errno -2",
    ]
    return any(s in msg for s in connection_patterns)


def is_timeout_error(exc: BaseException) -> bool:
    """Check if an exception is a timeout error."""
    try:
        import httpx

        if isinstance(exc, httpx.TimeoutException | TimeoutError):
            return True
    except ImportError:
        if isinstance(exc, TimeoutError):
            return True
    return "timeout" in str(exc).lower()


def create_connection_error(security: str, reason: str) -> SecurityError:
    """Helper to create connection/network errors."""
    return SecurityError(
        security=security,
        errorInfo=ErrorInfo(
            source="bloomberg-emulator",
            code=ERROR_CODES.get("CONNECTION_ERROR", 2),
            category=ErrorCategory.CONNECTION_ERROR,
            message=reason,
            subcategory="NETWORK_ERROR",
        ),
    )


def classify_and_create_error(security: str, exc: BaseException) -> SecurityError:
    """Classify an exception and create the appropriate SecurityError."""
    reason = f"Error: {exc}"
    # Check timeout before connection - TimeoutError is subclass of OSError
    if is_timeout_error(exc):
        return SecurityError(
            security=security,
            errorInfo=ErrorInfo(
                source="bloomberg-emulator",
                code=ERROR_CODES.get("TIMEOUT", 1),
                category=ErrorCategory.TIMEOUT,
                message=reason,
                subcategory="TIMEOUT",
            ),
        )
    if is_connection_error(exc):
        return create_connection_error(security, reason)
    return create_security_error(security, reason)
