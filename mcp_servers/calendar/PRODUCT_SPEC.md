# MCP Mock Product Specification: Calendar Server

---

## 0. App Classification

| Property | Value | Notes |
|-----------|--------|-------|
| **Stateful** | Yes | Maintains calendar events in `.ics` files |
| **Mutable** | Yes | Can create, update, and delete calendar events |
| **Visual** | No | Does not require a GUI or Computer Use |
| **Private** | No | Does not require authentication or authorization |
| **Mock Type** | Full Mock | Operates on local filesystem; no external API dependencies |
| **Setup Flags** | `APP_CALENDAR_DATA_ROOT`, `APP_CALENDAR_LIST_DEFAULT_LIMIT`, `APP_CALENDAR_LIST_MAX_LIMIT` | Environment variables for configuration |

---

## 1. Title

**Calendar Server MCP**

---

## 2. Overview

The Calendar Server provides comprehensive calendar event management capabilities using the iCalendar (`.ics`) format. It enables AI agents to create, read, update, delete, and list calendar events with support for recurrence rules, attendees, reminders, and time zones within a sandboxed filesystem environment.

---

## 3. Approach

### Data Source

- **Filesystem-based**: Events stored in `.ics` files within a configurable data directory (`APP_CALENDAR_DATA_ROOT`)
- **iCalendar Standard**: Uses the RFC 5545 iCalendar format for compatibility
- **No external APIs**: Self-contained calendar management

### Data Modes

| Mode | Description |
|------|-------------|
| **Clean Mode** | Empty calendar; events must be created before use |
| **Offline Mode** | Reactive-only; manages events that exist at request time |

### Core Architecture

| Component | Technology | Purpose |
|-----------|------------|---------|
| Event Storage | iCalendar (.ics) | Store events in standard format |
| Event Parsing | icalendar library | Parse and generate iCalendar data |
| Event Indexing | In-memory | Quick lookup by event ID |
| Timezone Support | pytz | Handle timezone conversions |

### Security

- **Path traversal protection**: All file paths resolved within data root
- **Sandboxed operations**: Cannot access files outside `APP_CALENDAR_DATA_ROOT`

---

## 4. Prioritized User Archetypes and Workflows

| User Type | Goal / Workflow | Primary Tools |
|-----------|-----------------|---------------|
| **Meeting Scheduler** | Create and manage meeting events | `create_event`, `update_event` |
| **Calendar Viewer** | Browse and read calendar events | `list_events`, `read_event` |
| **Event Manager** | Update or cancel events | `update_event`, `delete_event` |
| **Recurring Event Manager** | Set up recurring meetings/events | `create_event` (with recurrence) |
| **Attendee Coordinator** | Manage event attendees | `create_event`, `update_event` |

---

## 5. Extended Functionality (Future Builds)

| Feature | Description | Priority |
|---------|-------------|----------|
| **Calendar Sync** | Import/export to external calendar services | High |
| **Free/Busy Query** | Check availability for scheduling | Medium |
| **Event Search** | Search events by title, description, attendees | Medium |
| **Multi-Calendar Support** | Manage multiple calendars per user | Medium |
| **Calendar Sharing** | Share calendars with other users | Low |
| **Event Attachments** | Attach files to calendar events | Low |

---

## 6. Build Considerations

| Area | Description |
|------|-------------|
| **File Format** | iCalendar (.ics) following RFC 5545 standard |
| **Event ID Format** | `{YYYYMMDD}_{HHMMSS}_{random_suffix}` (e.g., `20251110_140000_abc123`) |
| **Time Formats** | Supports both dateTime with timezone and all-day (date only) events |
| **Recurrence** | Standard RRULE format (e.g., `RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR`) |
| **Timestamps** | Events automatically track `created` and `updated` timestamps |
| **Nested Files** | Supports `.ics` files in nested directory structure |
| **Dependencies** | `icalendar`, `pytz` |

---

## 7. Example MCP Commands

### Create Event

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `create_event` | Create a new calendar event | `summary`, `start`, `end`, `description`, `location`, `attendees`, `recurrence` | EventResponse with event_id |

```
create_event(
  summary="Team Meeting",
  start={"dateTime": "2025-11-10T14:00:00-05:00", "timeZone": "America/New_York"},
  end={"dateTime": "2025-11-10T15:00:00-05:00", "timeZone": "America/New_York"},
  description="Weekly team sync",
  location="Conference Room A",
  attendees=[
    {"email": "alice@example.com", "displayName": "Alice"},
    {"email": "bob@example.com", "displayName": "Bob"}
  ],
  recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO"]
)

→ EventResponse(success=True, event_id="20251110_140000_abc123", message="Event created successfully")
```

### All-Day Event

```
create_event(
  summary="Company Holiday",
  start={"date": "2025-12-25"},
  end={"date": "2025-12-26"},
  description="Christmas Day - Office Closed"
)

→ EventResponse(success=True, event_id="20251225_000000_xyz789", message="Event created successfully")
```

### Read Event

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `read_event` | Retrieve event details | `event_id` | CalendarEvent with full details |

```
read_event(event_id="20251110_140000_abc123")

→ CalendarEvent(
    id="20251110_140000_abc123",
    summary="Team Meeting",
    description="Weekly team sync",
    start={"dateTime": "2025-11-10T14:00:00-05:00", "timeZone": "America/New_York"},
    end={"dateTime": "2025-11-10T15:00:00-05:00", "timeZone": "America/New_York"},
    location="Conference Room A",
    attendees=[...],
    recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO"],
    created="2025-11-09T12:00:00",
    updated="2025-11-09T12:00:00"
  )
```

### Update Event

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `update_event` | Modify existing event | `event_id`, updated fields | EventResponse |

```
update_event(
  event_id="20251110_140000_abc123",
  location="Conference Room B",
  description="Updated: Weekly team sync with agenda review"
)

→ EventResponse(success=True, event_id="20251110_140000_abc123", message="Event updated successfully")
```

### Delete Event

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `delete_event` | Remove an event | `event_id` | EventResponse |

```
delete_event(event_id="20251110_140000_abc123")

→ EventResponse(success=True, event_id="20251110_140000_abc123", message="Event deleted successfully")
```

### List Events

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `list_events` | List events with pagination | `limit`, `offset` | EventListResponse with EventSummary list |

```
list_events(limit=10, offset=0)

→ EventListResponse(
    events=[
      EventSummary(id="20251110_140000_abc123", summary="Team Meeting", start=..., end=...),
      EventSummary(id="20251112_090000_def456", summary="Project Review", start=..., end=...)
    ],
    total_count=25,
    limit=10,
    offset=0
  )
```

### Event Time Formats

| Format | Example | Description |
|--------|---------|-------------|
| DateTime with TZ | `{"dateTime": "2025-11-10T14:00:00-05:00", "timeZone": "America/New_York"}` | Specific time with timezone |
| All-Day (Date) | `{"date": "2025-11-10"}` | All-day event |

### Recurrence Rules (RRULE)

| Pattern | Example | Description |
|---------|---------|-------------|
| Daily | `RRULE:FREQ=DAILY` | Every day |
| Weekly | `RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR` | Mon, Wed, Fri |
| Monthly | `RRULE:FREQ=MONTHLY;BYMONTHDAY=15` | 15th of each month |
| Yearly | `RRULE:FREQ=YEARLY;BYMONTH=12;BYMONTHDAY=25` | Dec 25 yearly |

---

## 8. Notes and References

### Environment Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_CALENDAR_DATA_ROOT` | `/.apps_data/calendar` | Root directory for calendar data |
| `APP_CALENDAR_LIST_DEFAULT_LIMIT` | `50` | Default number of events in list |
| `APP_CALENDAR_LIST_MAX_LIMIT` | `100` | Maximum events per request |
| `TOOLS` | All enabled | Comma-separated list to enable specific tools only |

### Key Behaviors

- **Sorting**: Events listed by start time (chronological order)
- **Auto-timestamps**: `created` and `updated` fields managed automatically
- **Nested Files**: Supports `.ics` files in nested directory structure under data root
- **Event ID**: Generated from timestamp + random suffix for uniqueness

### Dependencies

- [icalendar](https://icalendar.readthedocs.io/) — iCalendar parsing and generation
- [pytz](https://pythonhosted.org/pytz/) — Timezone handling

---


