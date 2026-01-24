import random
import string
from datetime import datetime

from loguru import logger
from models.calendar import (
    CalendarEvent,
    CreateEventRequest,
    EventResponse,
)
from pydantic import ValidationError
from utils.decorators import make_async_background
from utils.ical import add_event_to_calendar


def generate_event_id() -> str:
    """Generate a unique event ID."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{timestamp}_{suffix}"


@make_async_background
def create_event(request: CreateEventRequest) -> EventResponse:
    """Create a new calendar event."""
    try:
        timestamp = datetime.now()
        event_id = generate_event_id()

        event_data = CalendarEvent(
            id=event_id,
            summary=request.summary,
            description=request.description,
            start=request.start,
            end=request.end,
            location=request.location,
            attendees=request.attendees,
            colorId=request.colorId,
            reminders=request.reminders,
            recurrence=request.recurrence,
            created=timestamp.isoformat(),
            updated=timestamp.isoformat(),
        )

        logger.info(f"adding event to calendar: {event_data}")
        add_event_to_calendar(event_data)

        return EventResponse(
            success=True,
            event_id=event_id,
            message="Event created successfully",
            error=None,
        )
    except ValidationError as e:
        error_messages = "; ".join(
            [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()]
        )
        return EventResponse(
            success=False,
            event_id=None,
            message="Validation failed",
            error=error_messages,
        )
    except Exception as exc:
        return EventResponse(
            success=False,
            event_id=None,
            message="Failed to save event",
            error=repr(exc),
        )
