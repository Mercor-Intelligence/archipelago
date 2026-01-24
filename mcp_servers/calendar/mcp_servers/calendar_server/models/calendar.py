import re
from datetime import datetime
from re import Pattern
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from utils.config import (
    DEFAULT_LIST_LIMIT,
    MAX_DESCRIPTION_LENGTH,
    MAX_LIST_LIMIT,
    MAX_LOCATION_LENGTH,
    MAX_SUMMARY_LENGTH,
)


class CalendarEventAttendee(BaseModel):
    """Model for an event attendee."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(..., description="Attendee's email address")
    displayName: str | None = Field(None, description="Attendee's display name")
    responseStatus: (
        Literal["needsAction", "declined", "tentative", "accepted"] | None
    ) = Field(None, description="Attendee's response status")

    _EMAIL_PATTERN: ClassVar[Pattern[str]] = re.compile(
        r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    )

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: str) -> str:
        if not value or not cls._EMAIL_PATTERN.match(value):
            raise ValueError("Invalid email address")
        return value


class CalendarEventReminder(BaseModel):
    """Model for an event reminder override."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["email", "popup"] = Field(..., description="Reminder method")
    minutes: int = Field(..., description="Minutes before event to remind", ge=0)


class CalendarEventDateTime(BaseModel):
    """Model for event date/time."""

    model_config = ConfigDict(extra="forbid")

    dateTime: str | None = Field(None, description="ISO format datetime")
    date: str | None = Field(None, description="Date only (YYYY-MM-DD)")
    timeZone: str | None = Field(None, description="Time zone (e.g., America/New_York)")

    @model_validator(mode="after")
    def validate_date_or_datetime(self):
        """Ensure either dateTime or date is present, but not both."""
        if self.dateTime and self.date:
            raise ValueError("Cannot specify both dateTime and date")
        if not self.dateTime and not self.date:
            raise ValueError("Must specify either dateTime or date")
        return self

    @field_validator("dateTime")
    @classmethod
    def _validate_datetime(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return value
        except ValueError as e:
            raise ValueError(f"Invalid ISO datetime format: {value}") from e

    @field_validator("date")
    @classmethod
    def _validate_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            datetime.strptime(value, "%Y-%m-%d")
            return value
        except ValueError as e:
            raise ValueError(
                f"Invalid date format (expected YYYY-MM-DD): {value}"
            ) from e


class CalendarEventReminders(BaseModel):
    """Model for event reminders."""

    model_config = ConfigDict(extra="forbid")

    useDefault: bool = Field(default=True, description="Use default reminders")
    overrides: list[CalendarEventReminder] | None = Field(
        None, description="Custom reminder overrides"
    )


class CalendarEvent(BaseModel):
    """Model for a complete calendar event stored in JSON."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Unique event identifier")
    summary: str | None = Field(None, description="Event summary/title")
    description: str | None = Field(None, description="Event description")
    start: CalendarEventDateTime = Field(..., description="Event start time")
    end: CalendarEventDateTime = Field(..., description="Event end time")
    location: str | None = Field(None, description="Event location")
    attendees: list[CalendarEventAttendee] | None = Field(
        None, description="Event attendees"
    )
    colorId: str | None = Field(None, description="Event color ID")
    reminders: CalendarEventReminders | None = Field(
        None, description="Event reminders"
    )
    recurrence: list[str] | None = Field(None, description="Recurrence rules (RRULE)")
    created: str = Field(..., description="ISO timestamp when event was created")
    updated: str = Field(..., description="ISO timestamp when event was last updated")

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, value: str | None) -> str | None:
        if value is not None and len(value) > MAX_SUMMARY_LENGTH:
            raise ValueError(f"Summary must be {MAX_SUMMARY_LENGTH} characters or less")
        return value

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        if value is not None and len(value) > MAX_DESCRIPTION_LENGTH:
            raise ValueError(
                f"Description must be {MAX_DESCRIPTION_LENGTH} characters or less"
            )
        return value

    @field_validator("location")
    @classmethod
    def _validate_location(cls, value: str | None) -> str | None:
        if value is not None and len(value) > MAX_LOCATION_LENGTH:
            raise ValueError(
                f"Location must be {MAX_LOCATION_LENGTH} characters or less"
            )
        return value

    def __str__(self) -> str:
        """Format event data for display."""
        lines = [
            f"Event ID: {self.id}",
            f"Summary: {self.summary or 'N/A'}",
        ]

        if self.description:
            lines.append(f"Description: {self.description}")

        # Format start/end times
        if self.start.dateTime:
            lines.append(f"Start: {self.start.dateTime}")
        else:
            lines.append(f"Start Date: {self.start.date}")

        if self.end.dateTime:
            lines.append(f"End: {self.end.dateTime}")
        else:
            lines.append(f"End Date: {self.end.date}")

        if self.location:
            lines.append(f"Location: {self.location}")

        if self.attendees:
            lines.append(f"Attendees ({len(self.attendees)}):")
            for attendee in self.attendees:
                status = (
                    f" [{attendee.responseStatus}]" if attendee.responseStatus else ""
                )
                name = attendee.displayName or attendee.email
                lines.append(f"  - {name}{status}")

        if self.reminders:
            lines.append(
                f"Reminders: {'Default' if self.reminders.useDefault else 'Custom'}"
            )
            if self.reminders.overrides:
                for reminder in self.reminders.overrides:
                    lines.append(
                        f"  - {reminder.method}: {reminder.minutes} minutes before"
                    )

        if self.recurrence:
            lines.append("Recurrence:")
            for rule in self.recurrence:
                lines.append(f"  - {rule}")

        lines.extend(
            [
                f"Created: {self.created}",
                f"Updated: {self.updated}",
            ]
        )

        return "\n".join(lines)


class CreateEventInput(BaseModel):
    """Input model for creating a calendar event."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(..., description="Event summary/title")
    description: str | None = Field(None, description="Event description")
    start: CalendarEventDateTime = Field(..., description="Event start time")
    end: CalendarEventDateTime = Field(..., description="Event end time")
    location: str | None = Field(None, description="Event location")
    attendees: list[CalendarEventAttendee] | None = Field(
        None, description="Event attendees"
    )
    colorId: str | None = Field(None, description="Event color ID")
    reminders: CalendarEventReminders | None = Field(
        None, description="Event reminders"
    )
    recurrence: list[str] | None = Field(None, description="Recurrence rules (RRULE)")

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Summary cannot be empty")
        if len(value) > MAX_SUMMARY_LENGTH:
            raise ValueError(f"Summary must be {MAX_SUMMARY_LENGTH} characters or less")
        return value

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        if value is not None and len(value) > MAX_DESCRIPTION_LENGTH:
            raise ValueError(
                f"Description must be {MAX_DESCRIPTION_LENGTH} characters or less"
            )
        return value

    @field_validator("location")
    @classmethod
    def _validate_location(cls, value: str | None) -> str | None:
        if value is not None and len(value) > MAX_LOCATION_LENGTH:
            raise ValueError(
                f"Location must be {MAX_LOCATION_LENGTH} characters or less"
            )
        return value


class UpdateEventInput(BaseModel):
    """Input model for updating a calendar event."""

    model_config = ConfigDict(extra="forbid")

    summary: str | None = Field(None, description="Event summary/title")
    description: str | None = Field(None, description="Event description")
    start: CalendarEventDateTime | None = Field(None, description="Event start time")
    end: CalendarEventDateTime | None = Field(None, description="Event end time")
    location: str | None = Field(None, description="Event location")
    attendees: list[CalendarEventAttendee] | None = Field(
        None, description="Event attendees"
    )
    colorId: str | None = Field(None, description="Event color ID")
    reminders: CalendarEventReminders | None = Field(
        None, description="Event reminders"
    )
    recurrence: list[str] | None = Field(None, description="Recurrence rules (RRULE)")

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, value: str | None) -> str | None:
        if value is not None:
            if not value.strip():
                raise ValueError("Summary cannot be empty")
            if len(value) > MAX_SUMMARY_LENGTH:
                raise ValueError(
                    f"Summary must be {MAX_SUMMARY_LENGTH} characters or less"
                )
        return value

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        if value is not None and len(value) > MAX_DESCRIPTION_LENGTH:
            raise ValueError(
                f"Description must be {MAX_DESCRIPTION_LENGTH} characters or less"
            )
        return value

    @field_validator("location")
    @classmethod
    def _validate_location(cls, value: str | None) -> str | None:
        if value is not None and len(value) > MAX_LOCATION_LENGTH:
            raise ValueError(
                f"Location must be {MAX_LOCATION_LENGTH} characters or less"
            )
        return value


class EventSummary(BaseModel):
    """Summary model for listing events."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., description="Event ID")
    summary: str | None = Field(None, description="Event summary/title")
    start: CalendarEventDateTime = Field(..., description="Event start time")
    end: CalendarEventDateTime = Field(..., description="Event end time")

    def __str__(self) -> str:
        """Format event summary for display."""
        start_str = self.start.dateTime or self.start.date or "N/A"
        end_str = self.end.dateTime or self.end.date or "N/A"
        return (
            f"Event ID: {self.id}\n"
            f"Summary: {self.summary or 'N/A'}\n"
            f"Start: {start_str}\n"
            f"End: {end_str}"
        )


class EventResponse(BaseModel):
    """Response model for event operations."""

    model_config = ConfigDict(extra="forbid")

    success: bool = Field(..., description="Whether the operation was successful")
    event_id: str | None = Field(None, description="Unique identifier for the event")
    message: str = Field(..., description="Human-readable status message")
    error: str | None = Field(None, description="Error message if operation failed")

    @field_validator("event_id")
    @classmethod
    def _validate_event_id(cls, value: str | None, info) -> str | None:
        """Ensure event_id is present when success is True."""
        if info.data.get("success") and not value:
            raise ValueError("event_id must be present when success is True")
        return value

    def __str__(self) -> str:
        """Format response for display."""
        if not self.success:
            return f"Failed: {self.error or self.message}"
        return f"{self.message} (Event ID: {self.event_id})"


class CreateEventRequest(BaseModel):
    """Request model for creating an event (wraps CreateEventInput)."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(..., description="Event summary/title")
    description: str | None = Field(None, description="Event description")
    start: CalendarEventDateTime = Field(..., description="Event start time")
    end: CalendarEventDateTime = Field(..., description="Event end time")
    location: str | None = Field(None, description="Event location")
    attendees: list[CalendarEventAttendee] | None = Field(
        None, description="Event attendees"
    )
    colorId: str | None = Field(None, description="Event color ID")
    reminders: CalendarEventReminders | None = Field(
        None, description="Event reminders"
    )
    recurrence: list[str] | None = Field(None, description="Recurrence rules (RRULE)")

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Summary cannot be empty")
        if len(value) > MAX_SUMMARY_LENGTH:
            raise ValueError(f"Summary must be {MAX_SUMMARY_LENGTH} characters or less")
        return value

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        if value is not None and len(value) > MAX_DESCRIPTION_LENGTH:
            raise ValueError(
                f"Description must be {MAX_DESCRIPTION_LENGTH} characters or less"
            )
        return value

    @field_validator("location")
    @classmethod
    def _validate_location(cls, value: str | None) -> str | None:
        if value is not None and len(value) > MAX_LOCATION_LENGTH:
            raise ValueError(
                f"Location must be {MAX_LOCATION_LENGTH} characters or less"
            )
        return value


class UpdateEventRequest(BaseModel):
    """Request model for updating an event."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., description="Event ID to update")
    summary: str | None = Field(None, description="Event summary/title")
    description: str | None = Field(None, description="Event description")
    start: CalendarEventDateTime | None = Field(None, description="Event start time")
    end: CalendarEventDateTime | None = Field(None, description="Event end time")
    location: str | None = Field(None, description="Event location")
    attendees: list[CalendarEventAttendee] | None = Field(
        None, description="Event attendees"
    )
    colorId: str | None = Field(None, description="Event color ID")
    reminders: CalendarEventReminders | None = Field(
        None, description="Event reminders"
    )
    recurrence: list[str] | None = Field(None, description="Recurrence rules (RRULE)")

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, value: str | None) -> str | None:
        if value is not None:
            if not value.strip():
                raise ValueError("Summary cannot be empty")
            if len(value) > MAX_SUMMARY_LENGTH:
                raise ValueError(
                    f"Summary must be {MAX_SUMMARY_LENGTH} characters or less"
                )
        return value

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        if value is not None and len(value) > MAX_DESCRIPTION_LENGTH:
            raise ValueError(
                f"Description must be {MAX_DESCRIPTION_LENGTH} characters or less"
            )
        return value

    @field_validator("location")
    @classmethod
    def _validate_location(cls, value: str | None) -> str | None:
        if value is not None and len(value) > MAX_LOCATION_LENGTH:
            raise ValueError(
                f"Location must be {MAX_LOCATION_LENGTH} characters or less"
            )
        return value


class ReadEventRequest(BaseModel):
    """Request model for reading an event."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., description="Event ID to read")


class DeleteEventRequest(BaseModel):
    """Request model for deleting an event."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., description="Event ID to delete")


class ListEventsRequest(BaseModel):
    """Request model for listing events."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(
        default=DEFAULT_LIST_LIMIT,
        description="Maximum number of events to return",
        ge=1,
        le=MAX_LIST_LIMIT,
    )
    offset: int = Field(
        default=0,
        description="Number of events to skip",
        ge=0,
    )


class EventListResponse(BaseModel):
    """Response model for listing events."""

    model_config = ConfigDict(extra="forbid")

    events: list[EventSummary] = Field(..., description="List of event summaries")
    error: str | None = Field(None, description="Error message if listing failed")

    def __str__(self) -> str:
        """Format event list for display."""
        if self.error:
            return f"Failed to list events: {self.error}"

        if not self.events:
            return "No events found"

        lines = [f"Found {len(self.events)} event(s):", ""]

        for idx, event in enumerate(self.events, 1):
            lines.append(f"{idx}. {event}")
            lines.append("")

        return "\n".join(lines).strip()
