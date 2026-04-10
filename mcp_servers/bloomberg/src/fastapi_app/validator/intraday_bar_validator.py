from fastapi_app.models import (
    DEFAULT_EVENT_TYPE,
    SUPPORTED_EVENT_TYPES,
    SUPPORTED_INTERVALS,
    IntradayBarRequest,
    ValidationError,
)
from fastapi_app.validator import BaseValidator


class IntradayBarValidator(BaseValidator):
    """Validates IntradayBarRequest parameters"""

    @staticmethod
    def validate(request: IntradayBarRequest) -> ValidationError | None:
        """
        Validate the request according to MVP rules matrix
        Returns ValidationError if invalid, None if valid
        """
        # Check requestType
        if request.requestType != "IntradayBarRequest":
            return ValidationError(
                code="BAD_ARGS.UNKNOWN_REQUEST",
                message=f"Invalid requestType: {request.requestType}. Must be 'IntradayBarRequest'",
            )

        # Check security (exactly one)
        if not request.security or not request.security.strip():
            return ValidationError(
                code="BAD_ARGS.ONE_SECURITY_ONLY", message="Exactly one security must be provided"
            )

        # Check eventType
        event_type = request.eventType or DEFAULT_EVENT_TYPE
        if event_type not in SUPPORTED_EVENT_TYPES:
            return ValidationError(
                code="BAD_ARGS.INVALID_EVENTTYPE",
                message=f"Invalid eventType: {event_type}. Supported types: {', '.join(SUPPORTED_EVENT_TYPES)}",
            )

        # Check interval
        if not request.interval or request.interval <= 0:
            return ValidationError(
                code="BAD_ARGS.INVALID_INTERVAL", message="Interval must be a positive integer"
            )

        if request.interval not in SUPPORTED_INTERVALS:
            return ValidationError(
                code="BAD_ARGS.INVALID_INTERVAL",
                message=f"Unsupported interval: {request.interval}. Supported intervals: {', '.join(map(str, SUPPORTED_INTERVALS))}",
            )

        # Check time range
        time_error = IntradayBarValidator.validate_time_range(
            request.startDateTime, request.endDateTime
        )
        if time_error:
            return time_error

        return None
