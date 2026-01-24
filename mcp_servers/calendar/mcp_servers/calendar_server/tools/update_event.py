from datetime import datetime

from models.calendar import (
    CalendarEvent,
    EventResponse,
    UpdateEventRequest,
)
from pydantic import ValidationError
from utils.decorators import make_async_background
from utils.ical import (
    find_event_in_calendars,
    ical_event_to_calendar_event,
    update_event_in_calendar,
)


@make_async_background
def update_event(request: UpdateEventRequest) -> EventResponse:
    """Update an existing calendar event."""
    # Find the event in all calendar files
    result = find_event_in_calendars(request.event_id)

    if not result:
        return EventResponse(
            success=False,
            event_id=None,
            message="Event not found",
            error=f"Event not found with ID: {request.event_id}",
        )

    try:
        _, ical_event, _ = result
        existing_event = ical_event_to_calendar_event(ical_event)
    except Exception as e:
        return EventResponse(
            success=False,
            event_id=None,
            message="Failed to read existing event",
            error=repr(e),
        )

    update_data = request.model_dump(exclude_none=True, exclude={"event_id"})

    updated_event_dict = existing_event.model_dump()
    updated_event_dict.update(update_data)
    updated_event_dict["updated"] = datetime.now().isoformat()

    try:
        updated_event = CalendarEvent.model_validate(updated_event_dict)
    except ValidationError as e:
        error_messages = "; ".join(
            [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()]
        )
        return EventResponse(
            success=False,
            event_id=None,
            message="Updated event validation failed",
            error=error_messages,
        )

    try:
        success = update_event_in_calendar(updated_event)
        if not success:
            return EventResponse(
                success=False,
                event_id=None,
                message="Failed to update event in calendar",
                error="Event could not be found or updated",
            )
    except Exception as exc:
        return EventResponse(
            success=False,
            event_id=None,
            message="Failed to save updated event",
            error=repr(exc),
        )

    return EventResponse(
        success=True,
        event_id=request.event_id,
        message="Event updated successfully",
        error=None,
    )
