# Rls Calendar MCP Server

Mercor RLS Calendar MCP

## Tools

### 1. `calendar`

Calendar operations: create, read, update, delete, and list events.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'create', 'read', 'update', 'delete', 'list'] | Ellipsis | Action to perform. REQUIRED. Use help to see available actions. |
| `event_id` | string? | null | Event ID. REQUIRED for read/update/delete actions. |
| `summary` | string? | null | Event title/summary. REQUIRED for create action. |
| `description` | string? | null | Event description |
| `location` | string? | null | Event location |
| `start_date` | string? | null | Start date for all-day events (YYYY-MM-DD) |
| `start_datetime` | string? | null | Start datetime (ISO format with timezone) |
| `end_date` | string? | null | End date for all-day events (YYYY-MM-DD) |
| `end_datetime` | string? | null | End datetime (ISO format with timezone) |
| `timezone` | string? | null | Timezone (e.g., 'America/New_York') |
| `attendees` | array[string]? | null | List of attendee emails |
| `page` | integer? | null | Page number (0-indexed). Use with limit for pagination. |
| `limit` | integer? | null | Results per page. Use with page for pagination. |

---

### 2. `calendar_schema`

Get JSON schema for calendar input/output models.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `model` | string | Ellipsis | Model name: 'input', 'output', or a result type |

---
