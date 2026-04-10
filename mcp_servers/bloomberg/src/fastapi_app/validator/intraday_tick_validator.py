from fastapi_app.models import (
    INTRADAY_TICK_REQUEST,
    INTRADAY_TICK_SUPPORTED_EVENT_TYPES,
    IntradayTickRequest,
    ValidationError,
)
from fastapi_app.validator import BaseValidator


class IntradayTickValidator(BaseValidator):
    """Validates IntradayTickRequest parameters"""

    @staticmethod
    def validate(request: IntradayTickRequest) -> ValidationError | None:
        """
        Validate the request according to MVP rules matrix
        Returns ValidationError if invalid, None if valid
        """
        # Check requestType
        if request.requestType != INTRADAY_TICK_REQUEST:
            return ValidationError(
                code="BAD_ARGS.UNKNOWN_REQUEST",
                message=f"Invalid requestType: {request.requestType}. Must be '{INTRADAY_TICK_REQUEST}'",
            )

        # Check security (exactly one)
        if not request.security or not request.security.strip():
            return ValidationError(
                code="BAD_ARGS.ONE_SECURITY_ONLY", message="Exactly one security must be provided"
            )

        # Check eventTypes and make sure it is not empty
        if not request.eventTypes or len(request.eventTypes) == 0:
            return ValidationError(
                code="BAD_ARGS.INVALID_EVENTTYPE", message="At least one eventType must be provided"
            )
        unsupported_event_types = [
            event
            for event in request.eventTypes
            if event not in INTRADAY_TICK_SUPPORTED_EVENT_TYPES
        ]
        if unsupported_event_types:
            return ValidationError(
                code="BAD_ARGS.INVALID_EVENTTYPE",
                message=f"Invalid eventTypes: {', '.join(unsupported_event_types)}. Supported types: {', '.join(INTRADAY_TICK_SUPPORTED_EVENT_TYPES)}",
            )

        # Check time range
        time_error = IntradayTickValidator.validate_time_range(
            request.startDateTime, request.endDateTime
        )
        if time_error:
            return time_error

        return None
