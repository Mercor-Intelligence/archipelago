"""Calendar parsing utilities."""

import re
from dataclasses import dataclass
from datetime import date, datetime, time

from icalendar import Calendar


@dataclass
class ParsedEvent:
    uid: str
    summary: str
    start: datetime | date | None
    end: datetime | date | None
    location: str
    rrule: str | None

    @property
    def duration_minutes(self) -> int | None:
        if isinstance(self.start, datetime) and isinstance(self.end, datetime):
            return int((self.end - self.start).total_seconds() / 60)
        return None

    @property
    def is_all_day(self) -> bool:
        """True if event is all-day (date only, not datetime)."""
        return self.start is not None and not isinstance(self.start, datetime)


@dataclass
class ParsedCalendar:
    name: str
    events: list[ParsedEvent]


def parse_ics(content: str) -> ParsedCalendar:
    """Parse .ics content into structured calendar.

    Raises ValueError if content is malformed.
    """
    cal = Calendar.from_ical(content)
    events: list[ParsedEvent] = []
    for component in cal.walk():
        if component.name == "VEVENT":
            dtstart = component.get("dtstart")
            dtend = component.get("dtend")
            rrule = component.get("rrule")
            events.append(
                ParsedEvent(
                    uid=str(component.get("uid", "")),
                    summary=str(component.get("summary", "")),
                    start=dtstart.dt if dtstart else None,
                    end=dtend.dt if dtend else None,
                    location=str(component.get("location", "")),
                    rrule=str(rrule.to_ical().decode()) if rrule else None,
                )
            )
    return ParsedCalendar(name=str(cal.get("X-WR-CALNAME", "Calendar")), events=events)


def parse_date(s: str) -> date | None:
    """Parse date from common formats: YYYY-MM-DD, MM/DD/YYYY, etc."""
    for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y"]:
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _convert_12h_to_24h(hour: int, is_pm: bool) -> int:
    """Convert 12-hour time to 24-hour."""
    if is_pm and hour != 12:
        return hour + 12
    if not is_pm and hour == 12:
        return 0
    return hour


def parse_time(s: str) -> time | None:
    """Parse time from common formats: 14:00, 2:00 PM, 2pm."""
    s = s.strip().upper()

    try:
        # 24-hour: 14:00
        if m := re.match(r"^(\d{1,2}):(\d{2})$", s):
            return time(int(m[1]), int(m[2]))

        # 12-hour with minutes: 2:00 PM
        if m := re.match(r"^(\d{1,2}):(\d{2})\s*(AM|PM)$", s):
            h = _convert_12h_to_24h(int(m[1]), m[3] == "PM")
            return time(h, int(m[2]))

        # 12-hour without minutes: 2PM
        if m := re.match(r"^(\d{1,2})\s*(AM|PM)$", s):
            h = _convert_12h_to_24h(int(m[1]), m[2] == "PM")
            return time(h, 0)
    except ValueError:
        # Invalid hour/minute values (e.g., 25:00, 14:99)
        return None

    return None


def event_on_date(event: ParsedEvent, d: date) -> bool:
    """Check if event falls on the given date."""
    if not event.start:
        return False
    event_date = (
        event.start.date() if isinstance(event.start, datetime) else event.start
    )
    return event_date == d


def event_at_time(
    event: ParsedEvent, d: date | None, t: time | None, tolerance: int = 5
) -> bool:
    """Check if event matches date/time within tolerance minutes.

    Returns False if time is specified but event is all-day.
    """
    if d and not event_on_date(event, d):
        return False
    if t:
        # All-day events cannot match a specific time
        if event.is_all_day:
            return False
        if isinstance(event.start, datetime):
            event_mins = event.start.hour * 60 + event.start.minute
            target_mins = t.hour * 60 + t.minute
            if abs(event_mins - target_mins) > tolerance:
                return False
    return True


def _to_datetime(x: datetime | date) -> datetime:
    """Convert date to naive datetime for comparison.

    Strips timezone info to allow comparison of mixed tz-aware/naive events.
    """
    if isinstance(x, datetime):
        # Strip timezone to make naive for comparison
        return x.replace(tzinfo=None)
    return datetime.combine(x, time.min)


def events_overlap(a: ParsedEvent, b: ParsedEvent) -> bool:
    """Check if two events have overlapping time ranges."""
    if not (a.start and a.end and b.start and b.end):
        return False
    # Type narrowing: after the check above, all are non-None
    a_start = _to_datetime(a.start)
    a_end = _to_datetime(a.end)
    b_start = _to_datetime(b.start)
    b_end = _to_datetime(b.end)
    return a_start < b_end and b_start < a_end


def rrule_freq(rrule: str | None) -> str | None:
    """Extract FREQ value from RRULE string."""
    if rrule and (m := re.search(r"FREQ=(\w+)", rrule)):
        return m[1]
    return None


def rrule_days(rrule: str | None) -> set[str]:
    """Extract BYDAY values from RRULE string."""
    if rrule and (m := re.search(r"BYDAY=([^;]+)", rrule)):
        return {d.strip().upper() for d in m[1].split(",")}
    return set()
