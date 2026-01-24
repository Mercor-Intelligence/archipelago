"""Unit tests for list_events tool."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models.calendar import CreateEventRequest, ListEventsRequest
from tools.create_event import create_event
from tools.list_events import list_events

from .helpers.assertions import (
    assert_event_count,
    assert_event_created,
    assert_event_in_list,
    assert_event_not_in_list,
    assert_events_ordered_by_time,
)


class TestListEvents:
    """Test event listing functionality."""

    @pytest.mark.asyncio
    async def test_list_empty_calendar(self, mock_calendar_root):
        """Test listing events when calendar is empty."""
        request = ListEventsRequest()
        result = await list_events(request)
        assert_event_count(result, 0)

    @pytest.mark.asyncio
    async def test_list_single_event(self, mock_calendar_root, sample_event_datetime):
        """Test listing calendar with single event."""
        create_request = CreateEventRequest(**sample_event_datetime)
        create_result = await create_event(create_request)
        event_id = assert_event_created(create_result)

        list_request = ListEventsRequest()
        result = await list_events(list_request)
        assert_event_count(result, 1)
        assert_event_in_list(result, event_id)

    @pytest.mark.asyncio
    async def test_list_multiple_events(self, mock_calendar_root):
        """Test listing multiple events."""
        now = datetime.now()
        event_ids = []

        for i in range(3):
            request = CreateEventRequest(
                summary=f"Event {i}",
                start={"dateTime": (now + timedelta(hours=i)).isoformat()},
                end={"dateTime": (now + timedelta(hours=i + 1)).isoformat()},
            )
            result = await create_event(request)
            event_id = assert_event_created(result)
            event_ids.append(event_id)

        list_request = ListEventsRequest()
        result = await list_events(list_request)
        assert_event_count(result, 3)
        for event_id in event_ids:
            assert_event_in_list(result, event_id)

    @pytest.mark.asyncio
    async def test_list_events_sorted_by_time(self, mock_calendar_root):
        """Test that events are sorted by start time."""
        now = datetime.now()
        event_ids = []

        # Create events in reverse chronological order
        for i in range(3, 0, -1):
            request = CreateEventRequest(
                summary=f"Event at hour {i}",
                start={"dateTime": (now + timedelta(hours=i)).isoformat()},
                end={"dateTime": (now + timedelta(hours=i + 1)).isoformat()},
            )
            result = await create_event(request)
            event_id = assert_event_created(result)
            event_ids.append(event_id)

        list_request = ListEventsRequest()
        result = await list_events(list_request)
        # Events should be listed in chronological order (reverse of creation)
        assert_events_ordered_by_time(result, list(reversed(event_ids)))

    @pytest.mark.asyncio
    async def test_list_events_with_limit(
        self, mock_calendar_root, mock_list_pagination
    ):
        """Test listing events with limit parameter."""
        now = datetime.now()

        # Create 15 events
        for i in range(15):
            request = CreateEventRequest(
                summary=f"Event {i}",
                start={"dateTime": (now + timedelta(hours=i)).isoformat()},
                end={"dateTime": (now + timedelta(hours=i + 1)).isoformat()},
            )
            await create_event(request)

        # List with limit=5
        list_request = ListEventsRequest(limit=5)
        result = await list_events(list_request)
        # Should return 5 events
        assert len(result.events) == 5

    @pytest.mark.asyncio
    async def test_list_events_with_offset(
        self, mock_calendar_root, mock_list_pagination
    ):
        """Test listing events with offset parameter."""
        now = datetime.now()
        event_ids = []

        # Create 15 events
        for i in range(15):
            request = CreateEventRequest(
                summary=f"Event {i}",
                start={"dateTime": (now + timedelta(hours=i)).isoformat()},
                end={"dateTime": (now + timedelta(hours=i + 1)).isoformat()},
            )
            result = await create_event(request)
            event_id = assert_event_created(result)
            event_ids.append(event_id)

        # List with offset=5
        list_request = ListEventsRequest(limit=5, offset=5)
        result = await list_events(list_request)
        # First 5 events should not be in result
        for i in range(5):
            assert_event_not_in_list(result, event_ids[i])
        # Events 5-9 should be in result
        for i in range(5, 10):
            assert_event_in_list(result, event_ids[i])

    @pytest.mark.asyncio
    async def test_list_events_invalid_limit(
        self, mock_calendar_root, sample_event_datetime
    ):
        """Test listing with invalid limit (should raise validation error)."""
        create_request = CreateEventRequest(**sample_event_datetime)
        await create_event(create_request)

        # Negative limit should raise validation error
        with pytest.raises(ValueError):
            ListEventsRequest(limit=-5)

    @pytest.mark.asyncio
    async def test_list_events_exceeds_max_limit(
        self, mock_calendar_root, sample_event_datetime
    ):
        """Test listing with limit exceeding max (should raise validation error)."""
        create_request = CreateEventRequest(**sample_event_datetime)
        await create_event(create_request)

        # Limit exceeding max (100) should raise validation error
        with pytest.raises(ValueError):
            ListEventsRequest(limit=101)
