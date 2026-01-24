"""Unit tests for meta-tools (calendar and calendar_schema)."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools._meta_tools import (
    CalendarInput,
    EventListResult,
    EventResult,
    SchemaInput,
    calendar,
    calendar_schema,
)


class TestCalendarMetaTool:
    """Test the calendar meta-tool."""

    @pytest.mark.asyncio
    async def test_help_action(self, mock_calendar_root):
        """Test help action returns help info."""
        request = CalendarInput(action="help")
        result = await calendar(request)

        assert result.action == "help"
        assert result.error is None
        assert result.help is not None
        assert result.help.tool_name == "calendar"
        assert "create" in result.help.actions
        assert "read" in result.help.actions
        assert "update" in result.help.actions
        assert "delete" in result.help.actions
        assert "list" in result.help.actions

    @pytest.mark.asyncio
    async def test_create_action_success(self, mock_calendar_root):
        """Test successful event creation via meta-tool."""
        now = datetime.now()
        request = CalendarInput(
            action="create",
            summary="Test Meeting",
            start_datetime=(now + timedelta(hours=1)).isoformat(),
            end_datetime=(now + timedelta(hours=2)).isoformat(),
            description="A test meeting",
            location="Conference Room",
        )
        result = await calendar(request)

        assert result.action == "create"
        assert result.error is None
        assert result.create is not None
        assert result.create.success is True
        assert result.create.event_id is not None
        assert result.create.error is None

    @pytest.mark.asyncio
    async def test_create_action_all_day_event(self, mock_calendar_root):
        """Test creating an all-day event via meta-tool."""
        today = datetime.now().date()
        request = CalendarInput(
            action="create",
            summary="All Day Event",
            start_date=today.isoformat(),
            end_date=(today + timedelta(days=1)).isoformat(),
        )
        result = await calendar(request)

        assert result.action == "create"
        assert result.error is None
        assert result.create is not None
        assert result.create.success is True

    @pytest.mark.asyncio
    async def test_create_action_missing_summary(self, mock_calendar_root):
        """Test create action fails when summary is missing."""
        now = datetime.now()
        request = CalendarInput(
            action="create",
            start_datetime=(now + timedelta(hours=1)).isoformat(),
            end_datetime=(now + timedelta(hours=2)).isoformat(),
        )
        result = await calendar(request)

        assert result.action == "create"
        assert result.error is not None
        assert "summary" in result.error.lower()

    @pytest.mark.asyncio
    async def test_create_action_missing_start_time(self, mock_calendar_root):
        """Test create action fails when start time is missing."""
        now = datetime.now()
        request = CalendarInput(
            action="create",
            summary="Test Event",
            end_datetime=(now + timedelta(hours=2)).isoformat(),
        )
        result = await calendar(request)

        assert result.action == "create"
        assert result.error is not None
        assert "start" in result.error.lower()

    @pytest.mark.asyncio
    async def test_create_action_missing_end_time(self, mock_calendar_root):
        """Test create action fails when end time is missing."""
        now = datetime.now()
        request = CalendarInput(
            action="create",
            summary="Test Event",
            start_datetime=(now + timedelta(hours=1)).isoformat(),
        )
        result = await calendar(request)

        assert result.action == "create"
        assert result.error is not None
        assert "end" in result.error.lower()

    @pytest.mark.asyncio
    async def test_create_action_conflicting_start_times(self, mock_calendar_root):
        """Test create action fails when both start_date and start_datetime are provided."""
        now = datetime.now()
        today = now.date()
        request = CalendarInput(
            action="create",
            summary="Test Event",
            start_date=today.isoformat(),
            start_datetime=(now + timedelta(hours=1)).isoformat(),
            end_datetime=(now + timedelta(hours=2)).isoformat(),
        )
        result = await calendar(request)

        assert result.action == "create"
        assert result.error is not None
        assert (
            "start_date" in result.error.lower()
            or "start_datetime" in result.error.lower()
        )
        assert "both" in result.error.lower()

    @pytest.mark.asyncio
    async def test_create_action_conflicting_end_times(self, mock_calendar_root):
        """Test create action fails when both end_date and end_datetime are provided."""
        now = datetime.now()
        today = now.date()
        request = CalendarInput(
            action="create",
            summary="Test Event",
            start_datetime=(now + timedelta(hours=1)).isoformat(),
            end_date=(today + timedelta(days=1)).isoformat(),
            end_datetime=(now + timedelta(hours=2)).isoformat(),
        )
        result = await calendar(request)

        assert result.action == "create"
        assert result.error is not None
        assert (
            "end_date" in result.error.lower() or "end_datetime" in result.error.lower()
        )
        assert "both" in result.error.lower()

    @pytest.mark.asyncio
    async def test_create_action_with_attendees(self, mock_calendar_root):
        """Test creating event with attendees via meta-tool."""
        now = datetime.now()
        request = CalendarInput(
            action="create",
            summary="Meeting with Attendees",
            start_datetime=(now + timedelta(hours=1)).isoformat(),
            end_datetime=(now + timedelta(hours=2)).isoformat(),
            attendees=["alice@example.com", "bob@example.com"],
        )
        result = await calendar(request)

        assert result.action == "create"
        assert result.error is None
        assert result.create is not None
        assert result.create.success is True

    @pytest.mark.asyncio
    async def test_read_action_success(self, mock_calendar_root):
        """Test reading an event via meta-tool."""
        now = datetime.now()
        # First create an event
        create_request = CalendarInput(
            action="create",
            summary="Event to Read",
            start_datetime=(now + timedelta(hours=1)).isoformat(),
            end_datetime=(now + timedelta(hours=2)).isoformat(),
        )
        create_result = await calendar(create_request)
        event_id = create_result.create.event_id

        # Then read it
        read_request = CalendarInput(action="read", event_id=event_id)
        result = await calendar(read_request)

        assert result.action == "read"
        assert result.error is None
        assert result.read is not None
        assert result.read.event["id"] == event_id

    @pytest.mark.asyncio
    async def test_read_action_missing_event_id(self, mock_calendar_root):
        """Test read action fails when event_id is missing."""
        request = CalendarInput(action="read")
        result = await calendar(request)

        assert result.action == "read"
        assert result.error is not None
        assert "event_id" in result.error.lower()

    @pytest.mark.asyncio
    async def test_read_action_nonexistent_event(self, mock_calendar_root):
        """Test reading a non-existent event."""
        request = CalendarInput(action="read", event_id="nonexistent_id")
        result = await calendar(request)

        assert result.action == "read"
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_update_action_success(self, mock_calendar_root):
        """Test updating an event via meta-tool."""
        now = datetime.now()
        # First create an event
        create_request = CalendarInput(
            action="create",
            summary="Original Summary",
            start_datetime=(now + timedelta(hours=1)).isoformat(),
            end_datetime=(now + timedelta(hours=2)).isoformat(),
        )
        create_result = await calendar(create_request)
        event_id = create_result.create.event_id

        # Then update it
        update_request = CalendarInput(
            action="update",
            event_id=event_id,
            summary="Updated Summary",
        )
        result = await calendar(update_request)

        assert result.action == "update"
        assert result.error is None
        assert result.update is not None
        assert result.update.success is True

    @pytest.mark.asyncio
    async def test_update_action_missing_event_id(self, mock_calendar_root):
        """Test update action fails when event_id is missing."""
        request = CalendarInput(action="update", summary="New Summary")
        result = await calendar(request)

        assert result.action == "update"
        assert result.error is not None
        assert "event_id" in result.error.lower()

    @pytest.mark.asyncio
    async def test_update_action_nonexistent_event(self, mock_calendar_root):
        """Test updating a non-existent event."""
        request = CalendarInput(
            action="update",
            event_id="nonexistent_id",
            summary="New Summary",
        )
        result = await calendar(request)

        assert result.action == "update"
        assert result.update is not None
        assert result.update.success is False

    @pytest.mark.asyncio
    async def test_update_action_conflicting_start_times(self, mock_calendar_root):
        """Test update action fails when both start_date and start_datetime are provided."""
        now = datetime.now()
        today = now.date()
        # First create an event
        create_request = CalendarInput(
            action="create",
            summary="Original Event",
            start_datetime=(now + timedelta(hours=1)).isoformat(),
            end_datetime=(now + timedelta(hours=2)).isoformat(),
        )
        create_result = await calendar(create_request)
        event_id = create_result.create.event_id

        # Try to update with conflicting start times
        update_request = CalendarInput(
            action="update",
            event_id=event_id,
            start_date=today.isoformat(),
            start_datetime=(now + timedelta(hours=3)).isoformat(),
        )
        result = await calendar(update_request)

        assert result.action == "update"
        assert result.error is not None
        assert "both" in result.error.lower()

    @pytest.mark.asyncio
    async def test_update_action_conflicting_end_times(self, mock_calendar_root):
        """Test update action fails when both end_date and end_datetime are provided."""
        now = datetime.now()
        today = now.date()
        # First create an event
        create_request = CalendarInput(
            action="create",
            summary="Original Event",
            start_datetime=(now + timedelta(hours=1)).isoformat(),
            end_datetime=(now + timedelta(hours=2)).isoformat(),
        )
        create_result = await calendar(create_request)
        event_id = create_result.create.event_id

        # Try to update with conflicting end times
        update_request = CalendarInput(
            action="update",
            event_id=event_id,
            end_date=(today + timedelta(days=1)).isoformat(),
            end_datetime=(now + timedelta(hours=4)).isoformat(),
        )
        result = await calendar(update_request)

        assert result.action == "update"
        assert result.error is not None
        assert "both" in result.error.lower()

    @pytest.mark.asyncio
    async def test_delete_action_success(self, mock_calendar_root):
        """Test deleting an event via meta-tool."""
        now = datetime.now()
        # First create an event
        create_request = CalendarInput(
            action="create",
            summary="Event to Delete",
            start_datetime=(now + timedelta(hours=1)).isoformat(),
            end_datetime=(now + timedelta(hours=2)).isoformat(),
        )
        create_result = await calendar(create_request)
        event_id = create_result.create.event_id

        # Then delete it
        delete_request = CalendarInput(action="delete", event_id=event_id)
        result = await calendar(delete_request)

        assert result.action == "delete"
        assert result.error is None
        assert result.delete is not None
        assert result.delete.success is True

    @pytest.mark.asyncio
    async def test_delete_action_missing_event_id(self, mock_calendar_root):
        """Test delete action fails when event_id is missing."""
        request = CalendarInput(action="delete")
        result = await calendar(request)

        assert result.action == "delete"
        assert result.error is not None
        assert "event_id" in result.error.lower()

    @pytest.mark.asyncio
    async def test_delete_action_nonexistent_event(self, mock_calendar_root):
        """Test deleting a non-existent event."""
        request = CalendarInput(action="delete", event_id="nonexistent_id")
        result = await calendar(request)

        assert result.action == "delete"
        assert result.delete is not None
        assert result.delete.success is False

    @pytest.mark.asyncio
    async def test_list_action_empty_calendar(self, mock_calendar_root):
        """Test listing events on empty calendar."""
        request = CalendarInput(action="list")
        result = await calendar(request)

        assert result.action == "list"
        assert result.error is None
        assert result.list is not None
        assert result.list.events == []
        assert result.list.count == 0
        assert result.list.has_more is False

    @pytest.mark.asyncio
    async def test_list_action_with_events(self, mock_calendar_root):
        """Test listing events."""
        now = datetime.now()
        # Create some events
        for i in range(3):
            create_request = CalendarInput(
                action="create",
                summary=f"Event {i}",
                start_datetime=(now + timedelta(hours=i)).isoformat(),
                end_datetime=(now + timedelta(hours=i + 1)).isoformat(),
            )
            await calendar(create_request)

        # List them
        request = CalendarInput(action="list")
        result = await calendar(request)

        assert result.action == "list"
        assert result.error is None
        assert result.list is not None
        assert result.list.count == 3
        assert len(result.list.events) == 3

    @pytest.mark.asyncio
    async def test_list_action_with_pagination(self, mock_calendar_root):
        """Test listing events with pagination."""
        now = datetime.now()
        # Create 5 events
        for i in range(5):
            create_request = CalendarInput(
                action="create",
                summary=f"Event {i}",
                start_datetime=(now + timedelta(hours=i)).isoformat(),
                end_datetime=(now + timedelta(hours=i + 1)).isoformat(),
            )
            await calendar(create_request)

        # List with limit
        request = CalendarInput(action="list", page=0, limit=2)
        result = await calendar(request)

        assert result.action == "list"
        assert result.error is None
        assert result.list is not None
        assert result.list.count == 2
        assert result.list.page == 0
        assert result.list.limit == 2
        assert result.list.has_more is True

    @pytest.mark.asyncio
    async def test_list_action_page_2(self, mock_calendar_root):
        """Test listing events on page 2."""
        now = datetime.now()
        # Create 5 events
        for i in range(5):
            create_request = CalendarInput(
                action="create",
                summary=f"Event {i}",
                start_datetime=(now + timedelta(hours=i)).isoformat(),
                end_datetime=(now + timedelta(hours=i + 1)).isoformat(),
            )
            await calendar(create_request)

        # List page 1 (second page, 0-indexed)
        request = CalendarInput(action="list", page=1, limit=2)
        result = await calendar(request)

        assert result.action == "list"
        assert result.error is None
        assert result.list is not None
        assert result.list.count == 2
        assert result.list.page == 1

    @pytest.mark.asyncio
    async def test_list_action_uses_config_default_limit(self, mock_calendar_root):
        """Test that list action uses DEFAULT_LIST_LIMIT from config."""
        from utils.config import DEFAULT_LIST_LIMIT

        # List without specifying limit
        request = CalendarInput(action="list")
        result = await calendar(request)

        assert result.action == "list"
        assert result.error is None
        assert result.list is not None
        # The limit should be from config, not hardcoded 20
        assert result.list.limit == DEFAULT_LIST_LIMIT


class TestCalendarSchemaMetaTool:
    """Test the calendar_schema meta-tool."""

    def test_schema_input(self):
        """Test getting input schema."""
        request = SchemaInput(model="input")
        result = calendar_schema(request)

        assert result.model == "input"
        assert "properties" in result.json_schema
        assert "action" in result.json_schema["properties"]

    def test_schema_output(self):
        """Test getting output schema."""
        request = SchemaInput(model="output")
        result = calendar_schema(request)

        assert result.model == "output"
        assert "properties" in result.json_schema
        assert "action" in result.json_schema["properties"]

    def test_schema_event_result(self):
        """Test getting EventResult schema."""
        request = SchemaInput(model="EventResult")
        result = calendar_schema(request)

        assert result.model == "EventResult"
        assert "properties" in result.json_schema
        assert "success" in result.json_schema["properties"]
        assert "error" in result.json_schema["properties"]

    def test_schema_event_list_result(self):
        """Test getting EventListResult schema."""
        request = SchemaInput(model="EventListResult")
        result = calendar_schema(request)

        assert result.model == "EventListResult"
        assert "properties" in result.json_schema
        assert "events" in result.json_schema["properties"]
        assert "count" in result.json_schema["properties"]
        assert "has_more" in result.json_schema["properties"]

    def test_schema_unknown_model(self):
        """Test getting schema for unknown model."""
        request = SchemaInput(model="UnknownModel")
        result = calendar_schema(request)

        assert result.model == "UnknownModel"
        assert "error" in result.json_schema


class TestEventResultModel:
    """Test EventResult model includes error field."""

    def test_event_result_has_error_field(self):
        """Test that EventResult includes the error field."""
        result = EventResult(
            success=False,
            event_id=None,
            message="Validation failed",
            error="Field 'summary' is required",
        )
        assert result.error == "Field 'summary' is required"

    def test_event_result_error_optional(self):
        """Test that error field is optional."""
        result = EventResult(
            success=True,
            event_id="123",
            message="Created successfully",
        )
        assert result.error is None


class TestEventListResultModel:
    """Test EventListResult model has correct fields."""

    def test_event_list_result_has_count_and_has_more(self):
        """Test that EventListResult has count and has_more fields."""
        result = EventListResult(
            events=[],
            count=0,
            page=0,
            limit=20,
            has_more=False,
        )
        assert result.count == 0
        assert result.has_more is False

    def test_event_list_result_with_events(self):
        """Test EventListResult with events."""
        result = EventListResult(
            events=[{"id": "1"}, {"id": "2"}],
            count=2,
            page=0,
            limit=20,
            has_more=True,
        )
        assert result.count == 2
        assert result.has_more is True
        assert len(result.events) == 2
