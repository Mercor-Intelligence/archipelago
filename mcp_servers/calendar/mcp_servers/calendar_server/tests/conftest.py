"""Shared fixtures for calendar server tests."""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def temp_calendar_root(tmp_path):
    """Create a temporary calendar data directory.

    Args:
        tmp_path: pytest's temporary directory fixture

    Yields:
        Path: Path to the temporary calendar directory
    """
    calendar_dir = tmp_path / "calendar"
    calendar_dir.mkdir(exist_ok=True)
    yield calendar_dir


@pytest.fixture
def mock_calendar_root(temp_calendar_root):
    """Mock the CALENDAR_DATA_ROOT to point to temp directory.

    Args:
        temp_calendar_root: Temporary calendar directory fixture

    Yields:
        str: Path to the temporary calendar directory as string
    """
    with patch("utils.config.CALENDAR_DATA_ROOT", str(temp_calendar_root)):
        with patch("utils.path.CALENDAR_DATA_ROOT", str(temp_calendar_root)):
            with patch("utils.ical.resolve_calendar_path") as mock_resolve:
                # Mock resolve_calendar_path to return paths within temp directory
                def resolve_path(rel_path):
                    return str(temp_calendar_root / rel_path)

                mock_resolve.side_effect = resolve_path
                yield str(temp_calendar_root)


@pytest.fixture
def sample_event_datetime():
    """Return a sample event with datetime fields."""
    now = datetime.now()
    return {
        "summary": "Team Meeting",
        "start": {
            "dateTime": (now + timedelta(days=1)).isoformat(),
            "timeZone": "America/New_York",
        },
        "end": {
            "dateTime": (now + timedelta(days=1, hours=1)).isoformat(),
            "timeZone": "America/New_York",
        },
        "description": "Weekly team sync meeting",
        "location": "Conference Room A",
    }


@pytest.fixture
def sample_event_date_only():
    """Return a sample all-day event with date-only fields."""
    today = datetime.now().date()
    return {
        "summary": "Team Offsite",
        "start": {"date": today.isoformat()},
        "end": {"date": (today + timedelta(days=1)).isoformat()},
        "description": "Annual team offsite",
    }


@pytest.fixture
def sample_event_with_attendees():
    """Return a sample event with attendees."""
    now = datetime.now()
    return {
        "summary": "Client Meeting",
        "start": {
            "dateTime": (now + timedelta(days=2)).isoformat(),
            "timeZone": "America/New_York",
        },
        "end": {
            "dateTime": (now + timedelta(days=2, hours=2)).isoformat(),
            "timeZone": "America/New_York",
        },
        "description": "Quarterly business review",
        "location": "Zoom",
        "attendees": [
            {"email": "client@example.com", "displayName": "John Client"},
            {
                "email": "team@example.com",
                "displayName": "Team Lead",
                "responseStatus": "accepted",
            },
        ],
    }


@pytest.fixture
def sample_event_with_reminders():
    """Return a sample event with custom reminders."""
    now = datetime.now()
    return {
        "summary": "Important Deadline",
        "start": {
            "dateTime": (now + timedelta(days=7)).isoformat(),
            "timeZone": "UTC",
        },
        "end": {
            "dateTime": (now + timedelta(days=7, hours=1)).isoformat(),
            "timeZone": "UTC",
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "email", "minutes": 1440},  # 1 day before
                {"method": "popup", "minutes": 60},  # 1 hour before
            ],
        },
    }


@pytest.fixture
def sample_event_with_recurrence():
    """Return a sample recurring event."""
    now = datetime.now()
    return {
        "summary": "Weekly Standup",
        "start": {
            "dateTime": (now + timedelta(days=1)).replace(hour=9, minute=0).isoformat(),
            "timeZone": "America/New_York",
        },
        "end": {
            "dateTime": (now + timedelta(days=1))
            .replace(hour=9, minute=30)
            .isoformat(),
            "timeZone": "America/New_York",
        },
        "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR"],
    }


@pytest.fixture
def mock_list_pagination():
    """Mock list pagination limits."""
    with patch("utils.config.DEFAULT_LIST_LIMIT", 10):
        with patch("utils.config.MAX_LIST_LIMIT", 20):
            yield
