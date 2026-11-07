"""Utility functions for working with iCalendar files."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from icalendar import Calendar, Event, vDatetime, vText
from loguru import logger
from models.calendar import (
    CalendarEventAttendee,
    CalendarEventDateTime,
    CalendarEventReminder,
    CalendarEventReminders,
    CalendarOutputEvent,
)
from utils.path import resolve_calendar_path

DEFAULT_CALENDAR_FILE = "calendar.ics"


def _resolve_timezone(name: str | None) -> ZoneInfo | None:
    """Resolve an IANA timezone name, tolerating names the host does not know."""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(f"unknown timeZone {name!r}; storing dateTime as supplied")
        return None


def _convert_datetime_to_ical(dt: CalendarEventDateTime):
    """Convert CalendarEventDateTime to iCalendar vDatetime or date.

    Returns a datetime object for timed events or a date object for all-day events.
    The icalendar library will handle wrapping appropriately.
    """
    if dt.dateTime:
        # Parse ISO datetime string and return datetime object
        parsed = datetime.fromisoformat(dt.dateTime.replace("Z", "+00:00"))
        zone = _resolve_timezone(dt.timeZone)
        if zone is not None:
            # A ZoneInfo tzinfo is what makes icalendar emit TZID=<IANA name>, which
            # its own parser resolves back to the same zone. An offset-less dateTime
            # names a wall time in timeZone; an offset-bearing one already names an
            # instant, so re-express that instant instead of relabelling it.
            parsed = (
                parsed.replace(tzinfo=zone)
                if parsed.tzinfo is None
                else parsed.astimezone(zone)
            )
        elif parsed.tzinfo is not None:
            # icalendar renders a bare UTC offset as TZID="UTC-05:00", which it
            # cannot parse back: the value returns naive and the instant shifts by
            # the offset. UTC serialises as a plain Z form that does round-trip.
            parsed = parsed.astimezone(UTC)
        return vDatetime(parsed)
    elif dt.date:
        # Parse date-only string and return date object (for all-day events)
        parsed = datetime.strptime(dt.date, "%Y-%m-%d").date()
        return parsed  # Return plain date object, not wrapped in vDatetime
    raise ValueError("Either dateTime or date must be provided")


def _convert_ical_to_datetime(ical_dt) -> CalendarEventDateTime:
    """Convert iCalendar datetime to CalendarEventDateTime."""
    if isinstance(ical_dt.dt, datetime):
        # It's a datetime with time
        return CalendarEventDateTime(
            dateTime=ical_dt.dt.isoformat(),
            date=None,
            timeZone=str(ical_dt.dt.tzinfo) if ical_dt.dt.tzinfo else None,
        )
    else:
        # It's a date-only
        return CalendarEventDateTime(
            dateTime=None,
            date=ical_dt.dt.strftime("%Y-%m-%d"),
            timeZone=None,
        )


def _reminder_minutes_from_trigger(trigger) -> int | None:
    """Minutes before event start for a VALARM TRIGGER, or None if not expressible.

    str(trigger) is a Python repr ("vDDDTypes(-1 day, 23:45:00, ...)"), not the iCal
    literal, so the offset has to come from the timedelta itself. Reading it there
    also covers every RFC 5545 duration form at once (-PT15M, -PT1H30M, -P1D, -P1W,
    …) — icalendar picks between those on write, so the writer's own output is only
    readable this way.
    """
    value = trigger.dt if hasattr(trigger, "dt") else trigger
    if not isinstance(value, timedelta):
        # An absolute DATE-TIME trigger carries no offset from DTSTART on its own.
        return None
    minutes = round(-value.total_seconds() / 60)
    # Reminders are expressed as minutes *before* the start, so a trigger scheduled
    # after it has no representation here.
    return minutes if minutes >= 0 else None


def _normalize_ical_property_list(value):
    """Return an iCalendar property as a list without iterating scalar values."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _strip_mailto_scheme(value: str) -> str:
    """Strip mailto scheme regardless of casing."""
    if value.lower().startswith("mailto:"):
        return value[len("mailto:") :]
    return value


def calendar_event_to_ical(event: CalendarOutputEvent) -> Event:
    """Convert a CalendarOutputEvent to an iCalendar Event."""
    ical_event = Event()

    # Required fields
    ical_event.add("uid", event.id)
    ical_event.add("dtstart", _convert_datetime_to_ical(event.start))
    ical_event.add("dtend", _convert_datetime_to_ical(event.end))
    ical_event.add(
        "dtstamp", datetime.fromisoformat(event.created.replace("Z", "+00:00"))
    )
    ical_event.add(
        "created", datetime.fromisoformat(event.created.replace("Z", "+00:00"))
    )
    ical_event.add(
        "last-modified", datetime.fromisoformat(event.updated.replace("Z", "+00:00"))
    )

    # Optional fields
    if event.summary:
        ical_event.add("summary", vText(event.summary))

    if event.description:
        ical_event.add("description", vText(event.description))

    if event.location:
        ical_event.add("location", vText(event.location))

    if event.attendees:
        for attendee in event.attendees:
            attendee_str = f"mailto:{attendee.email}"
            params = {}
            if attendee.displayName:
                params["CN"] = attendee.displayName
            if attendee.responseStatus:
                # Map our status to iCalendar PARTSTAT
                status_map = {
                    "needsAction": "NEEDS-ACTION",
                    "declined": "DECLINED",
                    "tentative": "TENTATIVE",
                    "accepted": "ACCEPTED",
                }
                params["PARTSTAT"] = status_map.get(
                    attendee.responseStatus, "NEEDS-ACTION"
                )
            ical_event.add("attendee", attendee_str, parameters=params)

    if event.colorId:
        ical_event.add("color", event.colorId)

    # Handle reminders as VALARM components
    if event.reminders and event.reminders.overrides:
        for reminder in event.reminders.overrides:
            from icalendar import Alarm

            alarm = Alarm()
            alarm.add("action", "DISPLAY" if reminder.method == "popup" else "EMAIL")
            alarm.add("trigger", timedelta(minutes=-reminder.minutes))
            alarm.add("description", event.summary or "Reminder")
            ical_event.add_component(alarm)

    # Handle recurrence
    if event.recurrence:
        for rule in event.recurrence:
            ical_event.add("rrule", rule)

    return ical_event


def ical_event_to_calendar_event(ical_event: Event) -> CalendarOutputEvent:
    """Convert an iCalendar Event to a CalendarOutputEvent."""
    # Extract required fields
    event_id = str(ical_event.get("uid"))

    # Convert start and end times
    dtstart = ical_event.get("dtstart")
    dtend = ical_event.get("dtend")

    if not dtstart or not dtend:
        raise ValueError("Event must have start and end times")

    start = _convert_ical_to_datetime(dtstart)
    end = _convert_ical_to_datetime(dtend)

    # Get timestamps
    created_dt = ical_event.get("created")
    created = created_dt.dt.isoformat() if created_dt else datetime.now().isoformat()

    last_modified_dt = ical_event.get("last-modified")
    updated = last_modified_dt.dt.isoformat() if last_modified_dt else created

    # Optional fields
    summary = str(ical_event.get("summary")) if ical_event.get("summary") else None
    description = (
        str(ical_event.get("description")) if ical_event.get("description") else None
    )
    location = str(ical_event.get("location")) if ical_event.get("location") else None
    color_id = str(ical_event.get("color")) if ical_event.get("color") else None

    # Parse attendees
    attendees = []
    for att in _normalize_ical_property_list(ical_event.get("attendee")):
        email = _strip_mailto_scheme(str(att))
        params = att.params if hasattr(att, "params") else {}

        display_name = params.get("CN") if params else None

        # Map iCalendar PARTSTAT to our response status
        partstat = params.get("PARTSTAT") if params else None
        status_map: dict[
            str, Literal["needsAction", "declined", "tentative", "accepted"]
        ] = {
            "NEEDS-ACTION": "needsAction",
            "DECLINED": "declined",
            "TENTATIVE": "tentative",
            "ACCEPTED": "accepted",
        }
        response_status: (
            Literal["needsAction", "declined", "tentative", "accepted"] | None
        ) = status_map.get(str(partstat).upper()) if partstat else None

        attendees.append(
            CalendarEventAttendee(
                email=email,
                displayName=display_name,
                responseStatus=response_status,
            )
        )

    # Parse reminders from VALARM components
    reminders = None
    alarms = []
    for component in ical_event.walk():
        if component.name == "VALARM":
            trigger = component.get("trigger")
            action = str(component.get("action", "DISPLAY"))

            if trigger is not None:
                minutes = _reminder_minutes_from_trigger(trigger)
                if minutes is not None:
                    method = "popup" if action.upper() == "DISPLAY" else "email"
                    alarms.append(CalendarEventReminder(method=method, minutes=minutes))

    if alarms:
        reminders = CalendarEventReminders(useDefault=False, overrides=alarms)

    # Parse recurrence rules
    recurrence = None
    rrule = ical_event.get("rrule")
    if rrule:
        if isinstance(rrule, list):
            recurrence = [str(r) for r in rrule]
        else:
            recurrence = [str(rrule)]

    return CalendarOutputEvent(
        id=event_id,
        summary=summary,
        description=description,
        start=start,
        end=end,
        location=location,
        attendees=attendees if attendees else None,
        colorId=color_id,
        reminders=reminders,
        recurrence=recurrence,
        created=created,
        updated=updated,
    )


def get_all_ical_files() -> list[Path]:
    """Get all .ics files in the calendar data directory (recursively)."""
    calendar_dir = Path(resolve_calendar_path(""))
    if not calendar_dir.exists():
        return []

    return sorted(calendar_dir.rglob("*.ics"))


def read_calendar_from_file(file_path: Path) -> Calendar:
    """Read and parse an iCalendar file."""
    with open(file_path, "rb") as f:
        return Calendar.from_ical(f.read())  # type: ignore[return-value, arg-type]


def write_calendar_to_file(calendar: Calendar, file_path: Path) -> None:
    """Write a Calendar object to an iCalendar file."""
    with open(file_path, "wb") as f:
        f.write(calendar.to_ical())


def find_event_in_calendars(event_id: str) -> tuple[Calendar, Event, Path] | None:
    """
    Find an event by ID across all calendar files.

    Returns:
        A tuple of (Calendar, Event, file_path) if found, None otherwise.
    """
    for ical_file in get_all_ical_files():
        try:
            calendar = read_calendar_from_file(ical_file)
            for component in calendar.walk():
                if component.name == "VEVENT":
                    uid = str(component.get("uid", ""))
                    if uid == event_id:
                        return (calendar, component, ical_file)  # type: ignore[return-value]
        except Exception:
            continue

    return None


def get_all_events() -> list[CalendarOutputEvent]:
    """Get all events from all calendar files."""
    events = []
    logger.info(f"all ical files: {len(get_all_ical_files())}")
    for ical_file in get_all_ical_files():
        try:
            calendar = read_calendar_from_file(ical_file)
            for component in calendar.walk():
                if component.name == "VEVENT":
                    try:
                        event = ical_event_to_calendar_event(component)  # type: ignore[arg-type]
                        events.append(event)
                    except Exception:
                        continue
        except Exception:
            continue

    return events


def add_event_to_calendar(
    event: CalendarOutputEvent, calendar_file: str | None = None
) -> None:
    """
    Add an event to a calendar file.

    Args:
        event: The CalendarOutputEvent to add
        calendar_file: The calendar file name (optional, defaults to DEFAULT_CALENDAR_FILE)
    """
    if calendar_file is None:
        calendar_file = DEFAULT_CALENDAR_FILE

    calendar_dir = Path(resolve_calendar_path(""))
    calendar_dir.mkdir(parents=True, exist_ok=True)

    file_path = calendar_dir / calendar_file

    # Load existing calendar or create new one
    if file_path.exists():
        logger.info(f"calendar file exists: {file_path}")
        calendar = read_calendar_from_file(file_path)
    else:
        logger.info(f"calendar file does not exist: {file_path}")
        calendar = Calendar()
        calendar.add("prodid", "-//Archipelago Calendar Server//EN")
        calendar.add("version", "2.0")

    # Add the event
    ical_event = calendar_event_to_ical(event)
    calendar.add_component(ical_event)

    # Write back to file
    write_calendar_to_file(calendar, file_path)


def update_event_in_calendar(event: CalendarOutputEvent) -> bool:
    """
    Update an existing event in its calendar file.

    Returns:
        True if event was found and updated, False otherwise.
    """
    result = find_event_in_calendars(event.id)
    if not result:
        return False

    calendar, old_event, file_path = result

    # Remove the old event
    calendar.subcomponents.remove(old_event)

    # Add the updated event
    new_ical_event = calendar_event_to_ical(event)
    calendar.add_component(new_ical_event)

    # Write back to file
    write_calendar_to_file(calendar, file_path)

    return True


def delete_event_from_calendar(event_id: str) -> bool:
    """
    Delete an event from its calendar file.

    Returns:
        True if event was found and deleted, False otherwise.
    """
    result = find_event_in_calendars(event_id)
    if not result:
        return False

    calendar, event, file_path = result

    # Remove the event
    calendar.subcomponents.remove(event)

    # Write back to file
    write_calendar_to_file(calendar, file_path)

    return True
