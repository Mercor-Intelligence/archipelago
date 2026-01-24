# MCP Mock Product Specification: Mail Server

---

## 0. App Classification

| Property | Value | Notes |
|-----------|--------|-------|
| **Stateful** | Yes | Maintains emails in mbox format file |
| **Mutable** | Yes | Can send, reply, and forward emails |
| **Visual** | No | Does not require a GUI or Computer Use |
| **Private** | No | Does not require authentication or authorization |
| **Mock Type** | Full Mock | Operates on local mbox file; no external SMTP/IMAP |
| **Setup Flags** | `MAIL_DATA_ROOT`, `APP_MAIL_LIST_DEFAULT_LIMIT`, `APP_MAIL_LIST_MAX_LIMIT` | Environment variables for configuration |

---

## 1. Title

**Mail Server MCP**

---

## 2. Overview

The Mail Server provides email management capabilities using the mbox file format for storage. It enables AI agents to send emails, read messages, reply to threads, forward emails, and search through email history — all within a sandboxed filesystem environment with proper email threading support.

---

## 3. Approach

### Data Source

- **Filesystem-based**: Emails stored in mbox format within a configurable data directory (`MAIL_DATA_ROOT`)
- **mbox Standard**: Uses standard Unix mailbox format for compatibility
- **No external APIs**: Self-contained email simulation (no SMTP/IMAP)

### Data Modes

| Mode | Description |
|------|-------------|
| **Clean Mode** | Empty mailbox; emails must be sent to create data |
| **Offline Mode** | Reactive-only; manages emails that exist at request time |

### Core Architecture

| Component | Technology | Purpose |
|-----------|------------|---------|
| Email Storage | mbox format | Standard Unix mailbox storage |
| Email Parsing | Python mailbox | Parse and generate email messages |
| Threading | Standard headers | In-Reply-To, References, Thread-ID |
| File Locking | fcntl/mailbox | Prevent concurrent access corruption |

### Security

- **Path traversal protection**: All file paths resolved within data root
- **File locking**: Prevents concurrent access corruption
- **Sandboxed operations**: Cannot access files outside `MAIL_DATA_ROOT`

---

## 4. Prioritized User Archetypes and Workflows

| User Type | Goal / Workflow | Primary Tools |
|-----------|-----------------|---------------|
| **Email Sender** | Compose and send new emails | `send_mail` |
| **Email Reader** | Read and browse emails | `read_mail`, `list_mails` |
| **Thread Participant** | Reply to email conversations | `reply_mail`, `reply_all_mail` |
| **Email Forwarder** | Forward emails to others | `forward_mail` |
| **Email Searcher** | Find specific emails | `search_mail` |

---

## 5. Extended Functionality (Future Builds)

| Feature | Description | Priority |
|---------|-------------|----------|
| **Folders/Labels** | Organize emails into folders | High |
| **Delete/Archive** | Delete or archive emails | High |
| **Draft Saving** | Save draft emails | Medium |
| **Email Templates** | Pre-defined email templates | Medium |
| **Attachment Download** | Download/save attachments | Medium |
| **Thread View** | Get entire thread as structured data | Medium |
| **Mark Read/Unread** | Track read status | Low |

---

## 6. Build Considerations

| Area | Description |
|------|-------------|
| **File Format** | Standard mbox format (single file, "From " separator) |
| **Mail ID Format** | `<{timestamp}.{random}@{domain}>` |
| **Threading** | Standard email headers: In-Reply-To, References, Thread-ID |
| **Reply Prefix** | Automatically adds "Re: " to subject |
| **Forward Prefix** | Automatically adds "Fwd: " to subject |
| **File Locking** | Mailbox locking prevents concurrent access |
| **Append-Only** | No delete support (append-only storage) |
| **Dependencies** | Standard Python mailbox library |

---

## 7. Example MCP Commands

### Send Mail

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `send_mail` | Send a new email | `from_email`, `to_email`, `subject`, `body`, `cc`, `bcc`, `attachments` | MailResponse with mail_id |

```
send_mail(
  from_email="sender@example.com",
  to_email=["recipient@example.com", "other@example.com"],
  subject="Project Update",
  body="Here's the latest update on the project...",
  cc="manager@example.com",
  body_format="plain"
)

→ MailResponse(
    success=True,
    mail_id="<20251109120000.abc123@example.com>",
    recipients_count=3,
    message="Mail sent successfully"
  )
```

### Send HTML Email

```
send_mail(
  from_email="sender@example.com",
  to_email="recipient@example.com",
  subject="Welcome!",
  body="<h1>Welcome</h1><p>Thanks for joining us!</p>",
  body_format="html"
)

→ MailResponse(success=True, mail_id="<...>", ...)
```

### Read Mail

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `read_mail` | Retrieve full email details | `mail_id` | MailData with complete email |

```
read_mail(mail_id="<20251109120000.abc123@example.com>")

→ MailData(
    mail_id="<20251109120000.abc123@example.com>",
    from_email="sender@example.com",
    to=["recipient@example.com", "other@example.com"],
    cc=["manager@example.com"],
    subject="Project Update",
    body="Here's the latest update on the project...",
    body_format="plain",
    timestamp="Thu, 09 Nov 2025 12:00:00 -0500",
    thread_id="<thread-id>",
    in_reply_to=null,
    attachments=[]
  )
```

### List Mails

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `list_mails` | List emails with pagination | `limit`, `offset` | MailListResponse with MailSummary list |

```
list_mails(limit=10, offset=0)

→ MailListResponse(
    mails=[
      {
        "mail_id": "<id>",
        "timestamp": "Thu, 09 Nov 2025 12:00:00 -0500",
        "from": "sender@example.com",
        "to": ["recipient@example.com"],
        "subject": "Project Update",
        "thread_id": "<thread-id>",
        "in_reply_to": null
      }
    ],
    total_count=25,
    limit=10,
    offset=0
  )
```

### Reply Mail

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `reply_mail` | Reply to original sender | `original_mail_id`, `body`, `attachments` | MailResponse |

```
reply_mail(
  original_mail_id="<20251109120000.abc123@example.com>",
  body="Thanks for the update! I have a question..."
)

→ MailResponse(
    success=True,
    mail_id="<20251109130000.def456@example.com>",
    recipients_count=1,
    message="Reply sent successfully"
  )
# Automatically:
# - Addresses reply to original sender
# - Adds "Re: " prefix to subject
# - Preserves thread_id and references
```

### Reply All Mail

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `reply_all_mail` | Reply to all recipients | `original_mail_id`, `body`, `attachments` | MailResponse |

```
reply_all_mail(
  original_mail_id="<20251109120000.abc123@example.com>",
  body="Thanks everyone! Here's my response..."
)

→ MailResponse(success=True, mail_id="<...>", recipients_count=3, ...)
# Includes all original To and CC recipients
```

### Forward Mail

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `forward_mail` | Forward email to new recipients | `original_mail_id`, `to_email`, `additional_body`, `cc`, `bcc` | MailResponse |

```
forward_mail(
  original_mail_id="<20251109120000.abc123@example.com>",
  to_email="newrecipient@example.com",
  additional_body="FYI - please review this."
)

→ MailResponse(success=True, mail_id="<...>", ...)
# Automatically adds "Fwd: " prefix to subject
```

### Search Mail

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `search_mail` | Search emails by content | `query`, `search_in`, `limit`, `offset` | MailListResponse |

```
search_mail(query="project update", search_in="subject", limit=5)

→ MailListResponse(mails=[...matching emails...])

search_mail(query="budget", search_in="all", limit=10)

→ MailListResponse(mails=[...matching emails...])
```

### Search Options

| Value | Description |
|-------|-------------|
| `subject` | Search in subject line only |
| `from` | Search in sender email |
| `body` | Search in email body |
| `all` | Search in all fields (default) |

---

## 8. Notes and References

### Environment Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MAIL_DATA_ROOT` | `/.apps_data/mail` | Root directory for mbox file |
| `APP_MAIL_LIST_DEFAULT_LIMIT` | `50` | Default number of emails in list |
| `APP_MAIL_LIST_MAX_LIMIT` | Configurable | Maximum emails per request |
| `TOOLS` | All enabled | Comma-separated list to enable specific tools only |

### Key Behaviors

- **Threading**: Standard email threading via In-Reply-To, References, Thread-ID headers
- **Reply Prefix**: "Re: " added automatically (not duplicated if already present)
- **Forward Prefix**: "Fwd: " added automatically
- **File Locking**: Returns "Mailbox is currently busy" if lock unavailable
- **Append-Only**: Emails cannot be deleted (append-only mbox format)
- **Nested Files**: Supports mbox file in nested directory structure

### Email Structure

```python
{
  "mail_id": "<unique-message-id>",
  "from_email": "sender@example.com",
  "to": ["recipient@example.com"],
  "cc": ["manager@example.com"],
  "bcc": ["hidden@example.com"],
  "subject": "Email Subject",
  "body": "Email body content",
  "body_format": "plain",  # or "html"
  "attachments": ["/path/to/file.pdf"],
  "timestamp": "Thu, 09 Nov 2025 12:00:00 -0500",
  "thread_id": "<thread-unique-id>",
  "in_reply_to": "<parent-message-id>",
  "references": ["<msg1>", "<msg2>"]
}
```

### Dependencies

- Standard Python mailbox library (no external packages required)

---


