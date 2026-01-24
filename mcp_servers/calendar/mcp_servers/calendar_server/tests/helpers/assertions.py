"""Custom assertion helpers for calendar server tests."""

from models.calendar import CalendarEvent, EventListResponse, EventResponse


def assert_success(result: EventResponse, expected_message: str | None = None):
    """Assert that operation succeeded.

    Args:
        result: The EventResponse from calendar operation
        expected_message: Optional message substring to check for
    """
    assert result.success, (
        f"Expected success but got failure: {result.message} (error: {result.error})"
    )

    if expected_message is not None:
        assert expected_message.lower() in result.message.lower(), (
            f"Expected '{expected_message}' in message but got: {result.message}"
        )


def assert_failure(result: EventResponse, error_substring: str | None = None):
    """Assert that operation failed.

    Args:
        result: The EventResponse from calendar operation
        error_substring: Optional error substring to check for
    """
    assert not result.success, f"Expected failure but got success: {result.message}"

    if error_substring is not None:
        error_text = result.error or result.message
        assert error_substring.lower() in error_text.lower(), (
            f"Expected error '{error_substring}' but got: {error_text}"
        )


def assert_event_created(result: EventResponse) -> str:
    """Assert event was created successfully and return event ID.

    Args:
        result: The EventResponse from create_event

    Returns:
        The created event_id
    """
    assert result.success, (
        f"Event creation failed: {result.message} (error: {result.error})"
    )
    assert result.event_id is not None, "Event ID should be present on success"
    return result.event_id


def assert_event_updated(result: EventResponse):
    """Assert event was updated successfully.

    Args:
        result: The EventResponse from update_event
    """
    assert result.success, (
        f"Event update failed: {result.message} (error: {result.error})"
    )


def assert_event_deleted(result: EventResponse):
    """Assert event was deleted successfully.

    Args:
        result: The EventResponse from delete_event
    """
    assert result.success, (
        f"Event deletion failed: {result.message} (error: {result.error})"
    )


def assert_event_field(result: CalendarEvent, field_name: str, expected_value: str):
    """Assert event contains expected field value.

    Args:
        result: The CalendarEvent from read_event
        field_name: Name of the field to check (case-insensitive)
        expected_value: Expected value for the field
    """
    field_map = {
        "event id": result.id,
        "summary": result.summary,
        "description": result.description,
        "location": result.location,
    }

    field_lower = field_name.lower()
    assert field_lower in field_map, f"Unknown field '{field_name}'"

    actual_value = field_map[field_lower]
    assert actual_value is not None, f"Field '{field_name}' is None"
    assert expected_value.lower() in actual_value.lower(), (
        f"Expected value '{expected_value}' not found in field '{field_name}'. "
        f"Actual: {actual_value}"
    )


def assert_validation_error(result: EventResponse, field: str | None = None):
    """Assert that a validation error occurred.

    Args:
        result: The EventResponse from calendar operation
        field: Optional field name that should be in the error
    """
    assert not result.success, (
        f"Expected validation error but got success: {result.message}"
    )

    error_text = (result.error or result.message).lower()
    error_keywords = ["validation", "invalid", "value error", "failed"]
    has_error = any(keyword in error_text for keyword in error_keywords)
    assert has_error, f"Expected validation error but got: {error_text}"

    if field is not None:
        assert field.lower() in error_text, (
            f"Expected field '{field}' in validation error but got: {error_text}"
        )


def assert_event_not_found(result: EventResponse):
    """Assert that event was not found.

    Args:
        result: The EventResponse from calendar operation
    """
    assert not result.success, (
        f"Expected event not found but got success: {result.message}"
    )
    error_text = (result.error or result.message).lower()
    assert (
        "not found" in error_text
        or "does not exist" in error_text
        or "invalid" in error_text
    ), f"Expected 'not found' error but got: {error_text}"


def assert_event_count(result: EventListResponse, expected_count: int):
    """Assert the number of events in list result.

    Args:
        result: The EventListResponse from list_events
        expected_count: Expected number of events
    """
    assert result.error is None, f"List operation failed: {result.error}"
    actual_count = len(result.events)
    assert actual_count == expected_count, (
        f"Expected {expected_count} events but got {actual_count}"
    )


def assert_event_in_list(result: EventListResponse, event_id: str):
    """Assert event ID appears in list result.

    Args:
        result: The EventListResponse from list_events
        event_id: Event ID to check for
    """
    assert result.error is None, f"List operation failed: {result.error}"
    event_ids = [e.id for e in result.events]
    assert event_id in event_ids, (
        f"Event ID '{event_id}' not found in list: {event_ids}"
    )


def assert_event_not_in_list(result: EventListResponse, event_id: str):
    """Assert event ID does not appear in list result.

    Args:
        result: The EventListResponse from list_events
        event_id: Event ID to check for absence
    """
    assert result.error is None, f"List operation failed: {result.error}"
    event_ids = [e.id for e in result.events]
    assert event_id not in event_ids, (
        f"Event ID '{event_id}' unexpectedly found in list: {event_ids}"
    )


def assert_events_ordered_by_time(result: EventListResponse, event_ids: list[str]):
    """Assert events appear in the expected time order.

    Args:
        result: The EventListResponse from list_events
        event_ids: List of event IDs in expected order
    """
    assert result.error is None, f"List operation failed: {result.error}"
    actual_ids = [e.id for e in result.events]

    # Check that all expected IDs are present
    for eid in event_ids:
        assert eid in actual_ids, f"Event ID '{eid}' not found in result"

    # Check order
    actual_positions = {eid: i for i, eid in enumerate(actual_ids)}
    for i in range(len(event_ids) - 1):
        pos_current = actual_positions[event_ids[i]]
        pos_next = actual_positions[event_ids[i + 1]]
        assert pos_current < pos_next, (
            f"Events not in expected order. "
            f"Event '{event_ids[i]}' should come before '{event_ids[i + 1]}'"
        )


def assert_attendee_in_event(result: CalendarEvent, email: str):
    """Assert attendee email appears in event.

    Args:
        result: The CalendarEvent from read_event
        email: Attendee email to check for
    """
    assert result.attendees is not None, "Event has no attendees"
    emails = [a.email.lower() for a in result.attendees]
    assert email.lower() in emails, (
        f"Attendee '{email}' not found in event attendees: {emails}"
    )


def assert_recurrence_in_event(result: CalendarEvent, rrule: str):
    """Assert recurrence rule appears in event.

    Args:
        result: The CalendarEvent from read_event
        rrule: RRULE string to check for (e.g., "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR")
    """
    assert result.recurrence is not None, "Event has no recurrence"
    recurrence_str = " ".join(result.recurrence).upper()
    assert rrule.upper() in recurrence_str, (
        f"Recurrence rule '{rrule}' not found in: {result.recurrence}"
    )
