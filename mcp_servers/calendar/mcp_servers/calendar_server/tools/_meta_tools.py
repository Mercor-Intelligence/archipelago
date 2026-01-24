"""Meta-tools for LLM agents - consolidated interface with action-based routing."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Import existing tools for delegation
from tools.create_event import CreateEventRequest
from tools.create_event import create_event as _create_event
from tools.delete_event import DeleteEventRequest
from tools.delete_event import delete_event as _delete_event
from tools.list_events import ListEventsRequest
from tools.list_events import list_events as _list_events
from tools.read_event import ReadEventRequest
from tools.read_event import read_event as _read_event
from tools.update_event import UpdateEventRequest
from tools.update_event import update_event as _update_event
from utils.config import DEFAULT_LIST_LIMIT


# ============ Help Response ============
class ActionInfo(BaseModel):
    """Information about an action."""

    model_config = ConfigDict(extra="forbid")
    description: str
    required_params: list[str]
    optional_params: list[str]


class HelpResponse(BaseModel):
    """Help response listing available actions."""

    model_config = ConfigDict(extra="forbid")
    tool_name: str
    description: str
    actions: dict[str, ActionInfo]


# ============ Result Models ============
class EventResult(BaseModel):
    """Result from create/update/delete event."""

    model_config = ConfigDict(extra="forbid")
    success: bool
    event_id: str | None
    message: str
    error: str | None = None


class EventDetailsResult(BaseModel):
    """Result from reading an event."""

    model_config = ConfigDict(extra="forbid")
    event: dict[str, Any]


class EventListResult(BaseModel):
    """Result from listing events."""

    model_config = ConfigDict(extra="forbid")
    events: list[dict[str, Any]]
    count: int  # Number of events in this response (not total in calendar)
    page: int
    limit: int
    has_more: bool  # True if there may be more events on the next page


# ============ Input Model ============
class CalendarInput(BaseModel):
    """Input for calendar meta-tool."""

    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "help",
        "create",
        "read",
        "update",
        "delete",
        "list",
    ] = Field(
        ...,
        description="Action to perform. REQUIRED. Use help to see available actions.",
    )

    # Event identification
    event_id: str | None = Field(
        None, description="Event ID. REQUIRED for read/update/delete actions."
    )

    # Event details for create/update
    summary: str | None = Field(
        None, description="Event title/summary. REQUIRED for create action."
    )
    description: str | None = Field(None, description="Event description")
    location: str | None = Field(None, description="Event location")

    # Time fields
    start_date: str | None = Field(
        None, description="Start date for all-day events (YYYY-MM-DD)"
    )
    start_datetime: str | None = Field(
        None, description="Start datetime (ISO format with timezone)"
    )
    end_date: str | None = Field(
        None, description="End date for all-day events (YYYY-MM-DD)"
    )
    end_datetime: str | None = Field(
        None, description="End datetime (ISO format with timezone)"
    )
    timezone: str | None = Field(
        None, description="Timezone (e.g., 'America/New_York')"
    )

    # Attendees
    attendees: list[str] | None = Field(None, description="List of attendee emails")

    # List/pagination options
    page: int | None = Field(
        None, description="Page number (0-indexed). Use with limit for pagination."
    )
    limit: int | None = Field(
        None, description="Results per page. Use with page for pagination."
    )


# ============ Output Model ============
class CalendarOutput(BaseModel):
    """Output for calendar meta-tool."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(..., description="Action that was performed")
    error: str | None = Field(None, description="Error message if failed")

    # Discovery
    help: HelpResponse | None = None

    # Action-specific results
    create: EventResult | None = None
    read: EventDetailsResult | None = None
    update: EventResult | None = None
    delete: EventResult | None = None
    list: EventListResult | None = None


# ============ Help Definition ============
CALENDAR_HELP = HelpResponse(
    tool_name="calendar",
    description="Calendar operations: create, read, update, delete, and list events.",
    actions={
        "help": ActionInfo(
            description="List all available actions",
            required_params=[],
            optional_params=[],
        ),
        "create": ActionInfo(
            description=(
                "Create a new calendar event. "
                "Must provide start time (start_date OR start_datetime) and "
                "end time (end_date OR end_datetime)."
            ),
            required_params=[
                "summary",
                "start_date|start_datetime",
                "end_date|end_datetime",
            ],
            optional_params=[
                "description",
                "location",
                "timezone",
                "attendees",
            ],
        ),
        "read": ActionInfo(
            description="Read a calendar event by ID",
            required_params=["event_id"],
            optional_params=[],
        ),
        "update": ActionInfo(
            description="Update an existing event",
            required_params=["event_id"],
            optional_params=[
                "summary",
                "description",
                "location",
                "start_date",
                "start_datetime",
                "end_date",
                "end_datetime",
                "timezone",
                "attendees",
            ],
        ),
        "delete": ActionInfo(
            description="Delete a calendar event",
            required_params=["event_id"],
            optional_params=[],
        ),
        "list": ActionInfo(
            description="List calendar events with pagination",
            required_params=[],
            optional_params=["page", "limit"],
        ),
    },
)


# ============ Meta-Tool Implementation ============
async def calendar(request: CalendarInput) -> CalendarOutput:
    """Calendar operations: create, read, update, delete, and list events."""
    match request.action:
        case "help":
            return CalendarOutput(action="help", help=CALENDAR_HELP)

        case "create":
            if not request.summary:
                return CalendarOutput(action="create", error="Required: summary")

            # Validate start time is provided (but not both)
            if not request.start_date and not request.start_datetime:
                return CalendarOutput(
                    action="create",
                    error="Required: start_date or start_datetime",
                )
            if request.start_date and request.start_datetime:
                return CalendarOutput(
                    action="create",
                    error="Cannot specify both start_date and start_datetime",
                )

            # Validate end time is provided (but not both)
            if not request.end_date and not request.end_datetime:
                return CalendarOutput(
                    action="create",
                    error="Required: end_date or end_datetime",
                )
            if request.end_date and request.end_datetime:
                return CalendarOutput(
                    action="create",
                    error="Cannot specify both end_date and end_datetime",
                )

            try:
                # Build start/end time dicts
                start = None
                end = None
                if request.start_date:
                    start = {"date": request.start_date}
                else:  # start_datetime is guaranteed by validation above
                    start = {"dateTime": request.start_datetime}
                    if request.timezone:
                        start["timeZone"] = request.timezone

                if request.end_date:
                    end = {"date": request.end_date}
                else:  # end_datetime is guaranteed by validation above
                    end = {"dateTime": request.end_datetime}
                    if request.timezone:
                        end["timeZone"] = request.timezone

                req = CreateEventRequest(
                    summary=request.summary,
                    description=request.description,
                    location=request.location,
                    start=start,
                    end=end,
                    attendees=(
                        [{"email": e} for e in request.attendees]
                        if request.attendees
                        else None
                    ),
                )
                result = await _create_event(req)
                return CalendarOutput(
                    action="create",
                    create=EventResult(
                        success=result.success,
                        event_id=result.event_id,
                        message=result.message,
                        error=result.error,
                    ),
                )
            except Exception as exc:
                return CalendarOutput(action="create", error=str(exc))

        case "read":
            if not request.event_id:
                return CalendarOutput(action="read", error="Required: event_id")
            try:
                req = ReadEventRequest(event_id=request.event_id)
                result = await _read_event(req)
                return CalendarOutput(
                    action="read",
                    read=EventDetailsResult(event=result.model_dump()),
                )
            except Exception as exc:
                return CalendarOutput(action="read", error=str(exc))

        case "update":
            if not request.event_id:
                return CalendarOutput(action="update", error="Required: event_id")

            # Validate conflicting start time fields
            if request.start_date and request.start_datetime:
                return CalendarOutput(
                    action="update",
                    error="Cannot specify both start_date and start_datetime",
                )

            # Validate conflicting end time fields
            if request.end_date and request.end_datetime:
                return CalendarOutput(
                    action="update",
                    error="Cannot specify both end_date and end_datetime",
                )

            try:
                # Build start/end time dicts if provided
                start = None
                end = None
                if request.start_date:
                    start = {"date": request.start_date}
                elif request.start_datetime:
                    start = {"dateTime": request.start_datetime}
                    if request.timezone:
                        start["timeZone"] = request.timezone

                if request.end_date:
                    end = {"date": request.end_date}
                elif request.end_datetime:
                    end = {"dateTime": request.end_datetime}
                    if request.timezone:
                        end["timeZone"] = request.timezone

                req = UpdateEventRequest(
                    event_id=request.event_id,
                    summary=request.summary,
                    description=request.description,
                    location=request.location,
                    start=start,
                    end=end,
                    attendees=(
                        [{"email": e} for e in request.attendees]
                        if request.attendees
                        else None
                    ),
                )
                result = await _update_event(req)
                return CalendarOutput(
                    action="update",
                    update=EventResult(
                        success=result.success,
                        event_id=result.event_id,
                        message=result.message,
                        error=result.error,
                    ),
                )
            except Exception as exc:
                return CalendarOutput(action="update", error=str(exc))

        case "delete":
            if not request.event_id:
                return CalendarOutput(action="delete", error="Required: event_id")
            try:
                req = DeleteEventRequest(event_id=request.event_id)
                result = await _delete_event(req)
                return CalendarOutput(
                    action="delete",
                    delete=EventResult(
                        success=result.success,
                        event_id=result.event_id,
                        message=result.message,
                        error=result.error,
                    ),
                )
            except Exception as exc:
                return CalendarOutput(action="delete", error=str(exc))

        case "list":
            try:
                # Convert page to offset (ListEventsRequest uses limit/offset)
                page = request.page or 0
                limit = request.limit or DEFAULT_LIST_LIMIT
                offset = page * limit

                req = ListEventsRequest(
                    limit=limit,
                    offset=offset,
                )
                result = await _list_events(req)

                # Check for errors from the underlying list operation
                if result.error:
                    return CalendarOutput(action="list", error=result.error)

                events = [e.model_dump() for e in result.events]
                return CalendarOutput(
                    action="list",
                    list=EventListResult(
                        events=events,
                        count=len(events),
                        page=page,
                        limit=limit,
                        has_more=len(events)
                        == limit,  # If we got exactly limit events, there may be more
                    ),
                )
            except Exception as exc:
                return CalendarOutput(action="list", error=str(exc))

        case _:
            return CalendarOutput(
                action=request.action, error=f"Unknown action: {request.action}"
            )


# ============ Schema Tool ============
class SchemaInput(BaseModel):
    """Input for schema introspection."""

    model_config = ConfigDict(extra="forbid")
    model: str = Field(
        ...,
        description="Model name: 'input', 'output', or a result type",
    )


class SchemaOutput(BaseModel):
    """Output for schema introspection."""

    model_config = ConfigDict(extra="forbid")
    model: str
    json_schema: dict[str, Any]


SCHEMAS: dict[str, type[BaseModel]] = {
    "input": CalendarInput,
    "output": CalendarOutput,
    "EventResult": EventResult,
    "EventDetailsResult": EventDetailsResult,
    "EventListResult": EventListResult,
}


def calendar_schema(request: SchemaInput) -> SchemaOutput:
    """Get JSON schema for calendar input/output models."""
    if request.model not in SCHEMAS:
        available = ", ".join(sorted(SCHEMAS.keys()))
        return SchemaOutput(
            model=request.model,
            json_schema={"error": f"Unknown model. Available: {available}"},
        )
    return SchemaOutput(
        model=request.model,
        json_schema=SCHEMAS[request.model].model_json_schema(),
    )
