"""Unit tests for read_event tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models.calendar import CreateEventRequest, ReadEventRequest
from tools.create_event import create_event
from tools.read_event import read_event

from .helpers.assertions import (
    assert_event_created,
    assert_event_field,
)


class TestReadEvent:
    """Test event reading functionality."""

    @pytest.mark.asyncio
    async def test_read_existing_event(self, mock_calendar_root, sample_event_datetime):
        """Test reading an existing event."""
        # First create an event
        create_request = CreateEventRequest(**sample_event_datetime)
        create_result = await create_event(create_request)
        event_id = assert_event_created(create_result)

        # Then read it
        read_request = ReadEventRequest(event_id=event_id)
        result = await read_event(read_request)
        assert_event_field(result, "Event ID", event_id)
        assert_event_field(result, "Summary", sample_event_datetime["summary"])
        assert_event_field(result, "Location", sample_event_datetime["location"])

    @pytest.mark.asyncio
    async def test_read_event_with_attendees(
        self, mock_calendar_root, sample_event_with_attendees
    ):
        """Test reading event with attendees."""
        create_request = CreateEventRequest(**sample_event_with_attendees)
        create_result = await create_event(create_request)
        event_id = assert_event_created(create_result)

        read_request = ReadEventRequest(event_id=event_id)
        result = await read_event(read_request)
        # Check for attendees
        assert result.attendees is not None
        assert len(result.attendees) == 2
        emails = [a.email for a in result.attendees]
        assert "client@example.com" in emails
        assert "team@example.com" in emails

    @pytest.mark.asyncio
    async def test_read_event_with_recurrence(
        self, mock_calendar_root, sample_event_with_recurrence
    ):
        """Test reading recurring event."""
        create_request = CreateEventRequest(**sample_event_with_recurrence)
        create_result = await create_event(create_request)
        event_id = assert_event_created(create_result)

        read_request = ReadEventRequest(event_id=event_id)
        result = await read_event(read_request)
        # Check for recurrence
        assert result.recurrence is not None
        recurrence_str = " ".join(result.recurrence)
        assert "RRULE" in recurrence_str or "BYDAY" in recurrence_str

    @pytest.mark.asyncio
    async def test_read_nonexistent_event(self, mock_calendar_root):
        """Test reading non-existent event."""
        read_request = ReadEventRequest(event_id="nonexistent_event_id_12345")
        with pytest.raises(Exception) as exc_info:
            await read_event(read_request)
        assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_read_event_empty_id(self, mock_calendar_root):
        """Test reading event with empty ID."""
        read_request = ReadEventRequest(event_id="")
        with pytest.raises(Exception) as exc_info:
            await read_event(read_request)
        # Empty ID should raise an error
        assert exc_info.value is not None

    @pytest.mark.asyncio
    async def test_read_event_none_id(self, mock_calendar_root):
        """Test reading event with None ID - should fail at request creation."""
        with pytest.raises((TypeError, ValueError)):
            ReadEventRequest(event_id=None)  # type: ignore[arg-type]
