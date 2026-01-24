"""Integration tests for calendar operations."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models.calendar import (
    CreateEventRequest,
    DeleteEventRequest,
    ListEventsRequest,
    ReadEventRequest,
    UpdateEventRequest,
)
from tools.create_event import create_event
from tools.delete_event import delete_event
from tools.list_events import list_events
from tools.read_event import read_event
from tools.update_event import update_event

from .helpers.assertions import (
    assert_event_count,
    assert_event_created,
    assert_event_deleted,
    assert_event_field,
    assert_event_in_list,
    assert_event_updated,
)


class TestIntegration:
    """Test integrated calendar operations."""

    @pytest.mark.asyncio
    async def test_full_event_lifecycle(
        self, mock_calendar_root, sample_event_datetime
    ):
        """Test complete lifecycle: create → read → update → delete."""
        # Create
        create_request = CreateEventRequest(**sample_event_datetime)
        create_result = await create_event(create_request)
        event_id = assert_event_created(create_result)

        # Read
        read_request = ReadEventRequest(event_id=event_id)
        read_result = await read_event(read_request)
        assert_event_field(read_result, "Summary", sample_event_datetime["summary"])

        # Update
        update_request = UpdateEventRequest(
            event_id=event_id, summary="Updated Summary"
        )
        update_result = await update_event(update_request)
        assert_event_updated(update_result)

        # Verify update
        read_result2 = await read_event(read_request)
        assert_event_field(read_result2, "Summary", "Updated Summary")

        # Delete
        delete_request = DeleteEventRequest(event_id=event_id)
        delete_result = await delete_event(delete_request)
        assert_event_deleted(delete_result)

        # Verify deletion
        with pytest.raises(Exception) as exc_info:
            await read_event(read_request)
        assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_create_read_multiple_events_different_types(
        self, mock_calendar_root, sample_event_datetime, sample_event_date_only
    ):
        """Test creating both datetime and date-only events."""
        # Create datetime event
        request1 = CreateEventRequest(**sample_event_datetime)
        result1 = await create_event(request1)
        event_id1 = assert_event_created(result1)

        # Create date-only event
        request2 = CreateEventRequest(**sample_event_date_only)
        result2 = await create_event(request2)
        event_id2 = assert_event_created(result2)

        # List both
        list_request = ListEventsRequest()
        list_result = await list_events(list_request)
        assert_event_count(list_result, 2)
        assert_event_in_list(list_result, event_id1)
        assert_event_in_list(list_result, event_id2)

    @pytest.mark.asyncio
    async def test_update_preserves_other_fields(
        self, mock_calendar_root, sample_event_datetime
    ):
        """Test that updating one field doesn't change others."""
        create_request = CreateEventRequest(**sample_event_datetime)
        create_result = await create_event(create_request)
        event_id = assert_event_created(create_result)

        # Update only location
        update_request = UpdateEventRequest(event_id=event_id, location="New Location")
        update_result = await update_event(update_request)
        assert_event_updated(update_result)

        # Verify other fields are preserved
        read_request = ReadEventRequest(event_id=event_id)
        read_result = await read_event(read_request)
        assert_event_field(read_result, "Summary", sample_event_datetime["summary"])
        assert_event_field(read_result, "Location", "New Location")
        assert_event_field(
            read_result, "Description", sample_event_datetime["description"]
        )

    @pytest.mark.asyncio
    async def test_multiple_updates_to_same_event(
        self, mock_calendar_root, sample_event_datetime
    ):
        """Test performing multiple updates to the same event."""
        create_request = CreateEventRequest(**sample_event_datetime)
        create_result = await create_event(create_request)
        event_id = assert_event_created(create_result)

        # First update
        update1 = UpdateEventRequest(event_id=event_id, summary="First Update")
        await update_event(update1)

        # Second update
        update2 = UpdateEventRequest(
            event_id=event_id, location="Second Update Location"
        )
        await update_event(update2)

        # Third update
        update3 = UpdateEventRequest(
            event_id=event_id, description="Third Update Description"
        )
        await update_event(update3)

        # Verify all updates applied
        read_request = ReadEventRequest(event_id=event_id)
        read_result = await read_event(read_request)
        assert_event_field(read_result, "Summary", "First Update")
        assert_event_field(read_result, "Location", "Second Update Location")
        assert_event_field(read_result, "Description", "Third Update Description")
