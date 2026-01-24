"""Unit tests for delete_event tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models.calendar import CreateEventRequest, DeleteEventRequest, ReadEventRequest
from tools.create_event import create_event
from tools.delete_event import delete_event
from tools.read_event import read_event

from .helpers.assertions import (
    assert_event_created,
    assert_event_deleted,
    assert_event_not_found,
)


class TestDeleteEvent:
    """Test event deletion functionality."""

    @pytest.mark.asyncio
    async def test_delete_existing_event(
        self, mock_calendar_root, sample_event_datetime
    ):
        """Test deleting an existing event."""
        create_request = CreateEventRequest(**sample_event_datetime)
        create_result = await create_event(create_request)
        event_id = assert_event_created(create_result)

        delete_request = DeleteEventRequest(event_id=event_id)
        result = await delete_event(delete_request)
        assert_event_deleted(result)

        # Verify event is deleted
        read_request = ReadEventRequest(event_id=event_id)
        with pytest.raises(Exception) as exc_info:
            await read_event(read_request)
        assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_delete_nonexistent_event(self, mock_calendar_root):
        """Test deleting non-existent event."""
        delete_request = DeleteEventRequest(event_id="nonexistent_id_12345")
        result = await delete_event(delete_request)
        assert_event_not_found(result)

    @pytest.mark.asyncio
    async def test_delete_event_empty_id(self, mock_calendar_root):
        """Test deleting event with empty ID."""
        delete_request = DeleteEventRequest(event_id="")
        result = await delete_event(delete_request)
        # Empty ID should return failure
        assert not result.success

    @pytest.mark.asyncio
    async def test_delete_event_none_id(self, mock_calendar_root):
        """Test deleting event with None ID - should fail at request creation."""
        with pytest.raises((TypeError, ValueError)):
            DeleteEventRequest(event_id=None)  # type: ignore[arg-type]
