"""
Bloomberg-compliant error handling middleware.

Provides standardized error responses that match Bloomberg BLPAPI schema.
"""

import logging
import traceback
from typing import Any

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from shared.models.error_models import (
    ErrorCategory,
    ErrorInfo,
    create_response_error,
    is_connection_error,
    is_timeout_error,
)

logger = logging.getLogger(__name__)


class BloombergErrorMiddleware(BaseHTTPMiddleware):
    """
    Middleware to catch unhandled exceptions and return Bloomberg-compliant errors.

    Converts Python exceptions into standardized error responses that match
    the Bloomberg BLPAPI error format.
    """

    async def dispatch(self, request: Request, call_next):
        """
        Process request and catch any unhandled exceptions.

        Args:
            request: Incoming HTTP request
            call_next: Next middleware/handler in chain

        Returns:
            Response (normal or error)
        """
        try:
            response = await call_next(request)
            return response

        except ValueError as exc:
            # Invalid input data
            logger.warning(f"Validation error: {exc}", exc_info=True)
            return self._create_error_response(
                request=request,
                error_type="INVALID_REQUEST",
                message=str(exc),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        except TimeoutError as exc:
            # Request timeout
            logger.error(f"Timeout error: {exc}", exc_info=True)
            return self._create_error_response(
                request=request,
                error_type="TIMEOUT",
                message=f"Request timeout: {exc}",
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            )

        except PermissionError as exc:
            # Not authorized
            logger.warning(f"Authorization error: {exc}", exc_info=True)
            return self._create_error_response(
                request=request,
                error_type="NOT_AUTHORIZED",
                message=str(exc),
                status_code=status.HTTP_403_FORBIDDEN,
            )

        except Exception as exc:
            # Classify connection/timeout errors - must NOT be labeled INVALID_SECURITY
            logger.error(f"Unhandled exception: {exc}", exc_info=True)
            logger.debug(f"Traceback: {traceback.format_exc()}")

            if is_timeout_error(exc):
                return self._create_error_response(
                    request=request,
                    error_type="TIMEOUT",
                    message=f"Request timeout: {exc}",
                    status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                )
            if is_connection_error(exc):
                return self._create_error_response(
                    request=request,
                    error_type="CONNECTION_ERROR",
                    message=f"Network connectivity error: {exc}",
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                )

            return self._create_error_response(
                request=request,
                error_type="UNKNOWN",
                message=f"Internal server error: {exc}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def _create_error_response(
        self,
        request: Request,
        error_type: str,
        message: str,
        status_code: int,
    ) -> JSONResponse:
        """
        Create Bloomberg-compliant error response.

        Args:
            request: HTTP request
            error_type: Error type (maps to ErrorCategory)
            message: Human-readable error message
            status_code: HTTP status code

        Returns:
            JSONResponse with Bloomberg error format
        """
        # Extract request ID if available
        request_id = getattr(request.state, "request_id", "unknown")

        # Create Bloomberg-compliant error
        error = create_response_error(request_id=request_id, error_type=error_type, message=message)

        return JSONResponse(
            status_code=status_code,
            content=error.model_dump(),
        )


def create_field_error_response(
    field_id: str, reason: str, category: ErrorCategory = ErrorCategory.BAD_FIELD
) -> dict[str, Any]:
    """
    Create field-level error response (for field exceptions).

    Args:
        field_id: Bloomberg field mnemonic
        reason: Error reason
        category: Error category

    Returns:
        Dictionary with field exception format
    """
    from shared.models.error_models import ERROR_CODES

    return {
        "fieldId": field_id,
        "errorInfo": ErrorInfo(
            source="bloomberg-emulator",
            code=ERROR_CODES.get(category.value, ERROR_CODES["UNKNOWN"]),
            category=category,
            message=reason,
            subcategory=(
                "NOT_APPLICABLE_TO_HIST_DATA"
                if category == ErrorCategory.FIELD_NOT_APPLICABLE
                else None
            ),
        ).model_dump(),
    }


def create_security_error_response(security: str, reason: str) -> dict[str, Any]:
    """
    Create security-level error response.

    Args:
        security: Security identifier
        reason: Error reason

    Returns:
        Dictionary with security error format
    """
    from shared.models.error_models import ERROR_CODES

    return {
        "security": security,
        "errorInfo": ErrorInfo(
            source="bloomberg-emulator",
            code=ERROR_CODES["INVALID_SECURITY"],
            category=ErrorCategory.INVALID_SECURITY,
            message=reason,
            subcategory="INVALID_SECURITY",
        ).model_dump(),
    }
