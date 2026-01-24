from models.calendar import DeleteEventRequest, EventResponse
from utils.decorators import make_async_background
from utils.ical import delete_event_from_calendar


@make_async_background
def delete_event(request: DeleteEventRequest) -> EventResponse:
    """Delete a calendar event by its event ID."""
    # Delete the event from its calendar file
    try:
        success = delete_event_from_calendar(request.event_id)
        if not success:
            return EventResponse(
                success=False,
                event_id=None,
                message="Event not found",
                error=f"Event not found with ID: {request.event_id}",
            )
    except Exception as exc:
        return EventResponse(
            success=False,
            event_id=None,
            message="Failed to delete event",
            error=repr(exc),
        )

    return EventResponse(
        success=True,
        event_id=request.event_id,
        message="Event deleted successfully",
        error=None,
    )
