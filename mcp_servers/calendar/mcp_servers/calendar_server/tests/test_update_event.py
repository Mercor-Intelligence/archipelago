"""Unit tests for update_event tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models.calendar import CreateEventRequest, ReadEventRequest, UpdateEventRequest
from tools.create_event import create_event
from tools.read_event import read_event
from tools.update_event import update_event

from .helpers.assertions import (
    assert_event_created,
    assert_event_field,
    assert_event_not_found,
    assert_event_updated,
)


class TestUpdateEvent:
    """Test event update functionality."""

    @pytest.mark.asyncio
    async def test_update_event_summary(
        self, mock_calendar_root, sample_event_datetime
    ):
        """Test updating event summary."""
        create_request = CreateEventRequest(**sample_event_datetime)
        create_result = await create_event(create_request)
        event_id = assert_event_created(create_result)

        update_request = UpdateEventRequest(
            event_id=event_id, summary="Updated Summary"
        )
        result = await update_event(update_request)
        assert_event_updated(result)

        # Verify the update
        read_request = ReadEventRequest(event_id=event_id)
        read_result = await read_event(read_request)
        assert_event_field(read_result, "Summary", "Updated Summary")

    @pytest.mark.asyncio
    async def test_update_event_location(
        self, mock_calendar_root, sample_event_datetime
    ):
        """Test updating event location."""
        create_request = CreateEventRequest(**sample_event_datetime)
        create_result = await create_event(create_request)
        event_id = assert_event_created(create_result)

        update_request = UpdateEventRequest(event_id=event_id, location="New Location")
        result = await update_event(update_request)
        assert_event_updated(result)

        read_request = ReadEventRequest(event_id=event_id)
        read_result = await read_event(read_request)
        assert_event_field(read_result, "Location", "New Location")

    @pytest.mark.asyncio
    async def test_update_event_multiple_fields(
        self, mock_calendar_root, sample_event_datetime
    ):
        """Test updating multiple fields at once."""
        create_request = CreateEventRequest(**sample_event_datetime)
        create_result = await create_event(create_request)
        event_id = assert_event_created(create_result)

        update_request = UpdateEventRequest(
            event_id=event_id,
            summary="New Summary",
            description="New Description",
        )
        result = await update_event(update_request)
        assert_event_updated(result)

        read_request = ReadEventRequest(event_id=event_id)
        read_result = await read_event(read_request)
        assert_event_field(read_result, "Summary", "New Summary")
        assert_event_field(read_result, "Description", "New Description")

    @pytest.mark.asyncio
    async def test_update_event_attendees(
        self, mock_calendar_root, sample_event_datetime
    ):
        """Test updating event attendees."""
        create_request = CreateEventRequest(**sample_event_datetime)
        create_result = await create_event(create_request)
        event_id = assert_event_created(create_result)

        new_attendees = [
            {"email": "new@example.com", "displayName": "New Person"},
            {"email": "another@example.com", "displayName": "Another Person"},
        ]
        update_request = UpdateEventRequest(event_id=event_id, attendees=new_attendees)
        result = await update_event(update_request)
        assert_event_updated(result)

        read_request = ReadEventRequest(event_id=event_id)
        read_result = await read_event(read_request)
        # Check for attendees
        assert read_result.attendees is not None
        assert len(read_result.attendees) == 2
        emails = [a.email for a in read_result.attendees]
        assert "new@example.com" in emails
        assert "another@example.com" in emails

    @pytest.mark.asyncio
    async def test_update_nonexistent_event(self, mock_calendar_root):
        """Test updating non-existent event."""
        update_request = UpdateEventRequest(
            event_id="nonexistent_id_12345",
            summary="New Summary",
        )
        result = await update_event(update_request)
        assert_event_not_found(result)

    @pytest.mark.asyncio
    async def test_update_event_invalid_summary(
        self, mock_calendar_root, sample_event_datetime
    ):
        """Test validation error when updating with invalid summary."""
        create_request = CreateEventRequest(**sample_event_datetime)
        create_result = await create_event(create_request)
        event_id = assert_event_created(create_result)

        # Empty summary should raise validation error at request creation
        with pytest.raises(ValueError):
            UpdateEventRequest(event_id=event_id, summary="")

    @pytest.mark.asyncio
    async def test_update_event_empty_id(self, mock_calendar_root):
        """Test updating event with empty ID."""
        update_request = UpdateEventRequest(event_id="", summary="New Summary")
        result = await update_event(update_request)
        # Empty ID should return failure
        assert not result.success
