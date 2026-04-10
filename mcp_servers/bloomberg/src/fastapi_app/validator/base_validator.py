from datetime import datetime

from fastapi_app.models import ValidationError


class BaseValidator:
    @staticmethod
    def validate_time_range(start: str, end: str) -> ValidationError | None:
        """Validate start and end datetime strings"""
        if not start or not end:
            return ValidationError(
                code="BAD_ARGS.INVALID_TIME_RANGE",
                message="Both startDateTime and endDateTime are required",
            )

        try:
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return ValidationError(
                code="BAD_ARGS.INVALID_TIME_RANGE", message="Invalid ISO 8601 datetime format"
            )

        if start_dt >= end_dt:
            return ValidationError(
                code="BAD_ARGS.INVALID_TIME_RANGE",
                message="startDateTime must be before endDateTime",
            )

        return None
