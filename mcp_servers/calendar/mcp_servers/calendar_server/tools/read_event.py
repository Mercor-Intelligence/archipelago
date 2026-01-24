from models.calendar import CalendarEvent, ReadEventRequest
from pydantic import ValidationError
from utils.decorators import make_async_background
from utils.ical import find_event_in_calendars, ical_event_to_calendar_event


@make_async_background
def read_event(request: ReadEventRequest) -> CalendarEvent:
    """Read a calendar event by its event ID."""
    # Find the event in all calendar files
    result = find_event_in_calendars(request.event_id)

    if not result:
        raise ValueError(f"Event not found with ID: {request.event_id}")

    # Parse the event
    try:
        _, ical_event, _ = result
        event_data = ical_event_to_calendar_event(ical_event)
        return event_data
    except ValidationError as e:
        error_messages = "; ".join(
            [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()]
        )
        raise ValueError(f"Event data validation failed: {error_messages}") from e
    except Exception as e:
        raise ValueError(f"Failed to read event: {repr(e)}") from e
