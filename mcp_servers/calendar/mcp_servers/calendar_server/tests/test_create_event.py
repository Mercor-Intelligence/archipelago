"""Unit tests for create_event tool."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models.calendar import CreateEventRequest
from tools.create_event import create_event

from .helpers.assertions import (
    assert_event_created,
)


class TestCreateEvent:
    """Test event creation functionality."""

    @pytest.mark.asyncio
    async def test_create_basic_event(self, mock_calendar_root, sample_event_datetime):
        """Test creating a basic event with datetime."""
        request = CreateEventRequest(**sample_event_datetime)
        result = await create_event(request)
        event_id = assert_event_created(result)
        assert event_id

    @pytest.mark.asyncio
    async def test_create_all_day_event(
        self, mock_calendar_root, sample_event_date_only
    ):
        """Test creating an all-day event with date only."""
        request = CreateEventRequest(**sample_event_date_only)
        result = await create_event(request)
        event_id = assert_event_created(result)
        assert event_id

    @pytest.mark.asyncio
    async def test_create_event_with_attendees(
        self, mock_calendar_root, sample_event_with_attendees
    ):
        """Test creating event with attendees."""
        request = CreateEventRequest(**sample_event_with_attendees)
        result = await create_event(request)
        event_id = assert_event_created(result)
        assert event_id

    @pytest.mark.asyncio
    async def test_create_event_with_reminders(
        self, mock_calendar_root, sample_event_with_reminders
    ):
        """Test creating event with custom reminders."""
        request = CreateEventRequest(**sample_event_with_reminders)
        result = await create_event(request)
        event_id = assert_event_created(result)
        assert event_id

    @pytest.mark.asyncio
    async def test_create_event_with_recurrence(
        self, mock_calendar_root, sample_event_with_recurrence
    ):
        """Test creating recurring event."""
        request = CreateEventRequest(**sample_event_with_recurrence)
        result = await create_event(request)
        event_id = assert_event_created(result)
        assert event_id

    @pytest.mark.asyncio
    async def test_create_event_minimal_fields(self, mock_calendar_root):
        """Test creating event with only required fields."""
        now = datetime.now()
        request = CreateEventRequest(
            summary="Minimal Event",
            start={"dateTime": (now + timedelta(hours=1)).isoformat()},
            end={"dateTime": (now + timedelta(hours=2)).isoformat()},
        )
        result = await create_event(request)
        event_id = assert_event_created(result)
        assert event_id

    @pytest.mark.asyncio
    async def test_create_event_empty_summary(self, mock_calendar_root):
        """Test validation error for empty summary."""
        now = datetime.now()
        with pytest.raises(ValueError):
            CreateEventRequest(
                summary="",
                start={"dateTime": now.isoformat()},
                end={"dateTime": (now + timedelta(hours=1)).isoformat()},
            )

    @pytest.mark.asyncio
    async def test_create_event_summary_too_long(self, mock_calendar_root):
        """Test validation error for summary exceeding max length."""
        now = datetime.now()
        with pytest.raises(ValueError):
            CreateEventRequest(
                summary="x" * 501,  # MAX_SUMMARY_LENGTH is 500
                start={"dateTime": now.isoformat()},
                end={"dateTime": (now + timedelta(hours=1)).isoformat()},
            )

    @pytest.mark.asyncio
    async def test_create_event_invalid_datetime(self, mock_calendar_root):
        """Test validation error for invalid datetime format."""
        with pytest.raises(ValueError):
            CreateEventRequest(
                summary="Test Event",
                start={"dateTime": "not-a-datetime"},
                end={"dateTime": "also-not-a-datetime"},
            )

    @pytest.mark.asyncio
    async def test_create_event_both_datetime_and_date(self, mock_calendar_root):
        """Test validation error when both dateTime and date are provided."""
        now = datetime.now()
        with pytest.raises(ValueError):
            CreateEventRequest(
                summary="Test Event",
                start={"dateTime": now.isoformat(), "date": "2025-10-20"},
                end={
                    "dateTime": (now + timedelta(hours=1)).isoformat(),
                    "date": "2025-10-21",
                },
            )

    @pytest.mark.asyncio
    async def test_create_event_invalid_email(self, mock_calendar_root):
        """Test validation error for invalid attendee email."""
        now = datetime.now()
        with pytest.raises(ValueError):
            CreateEventRequest(
                summary="Test Event",
                start={"dateTime": now.isoformat()},
                end={"dateTime": (now + timedelta(hours=1)).isoformat()},
                attendees=[{"email": "not-an-email", "displayName": "Invalid"}],
            )
