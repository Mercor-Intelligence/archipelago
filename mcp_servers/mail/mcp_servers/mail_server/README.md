# Rls Mail MCP Server

Mercor RLS Mail MCP

## Tools

### 1. `mail`

Mail operations: send, read, list, search, reply, reply_all, and forward emails.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'send', 'read', 'list', 'search', 'reply', 'reply_all', 'forward'] | Ellipsis | Action to perform |
| `mail_id` | string? | null | Mail ID for read/reply/forward |
| `from_email` | string? | null | Sender email address |
| `to_email` | string | array[string]? | null | Recipient email address(es) |
| `subject` | string? | null | Email subject line |
| `body` | string? | null | Email or message body content. REQUIRED for send. |
| `cc` | string | array[string]? | null | CC recipients, comma-separated emails. |
| `bcc` | string | array[string]? | null | BCC recipients, comma-separated emails. |
| `attachments` | array[string]? | null | File paths to attach |
| `body_format` | enum['plain', 'html']? | null | Body format: 'plain' or 'html' |
| `thread_id` | string? | null | Thread identifier for grouping |
| `in_reply_to` | string? | null | Message-ID being replied to |
| `references` | array[string]? | null | List of referenced Message-IDs |
| `page` | integer? | null | Page number (0-indexed) |
| `limit` | integer? | null | Results per page |
| `offset` | integer? | null | Number of mails to skip |
| `search_from` | string? | null | Filter by sender email |
| `search_to` | string? | null | Filter by recipient email |
| `search_subject` | string? | null | Filter by subject (partial match) |
| `after_date` | string? | null | Filter emails after this date (YYYY-MM-DD or ISO format) |
| `before_date` | string? | null | Filter emails before this date (YYYY-MM-DD or ISO format) |
| `search_thread_id` | string? | null | Filter by thread ID |

---

### 2. `mail_schema`

Get JSON schema for mail input/output models.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `model` | string | Ellipsis | Model name: 'input', 'output', or a result type like 'SendResult', 'MailListResult' |

---
