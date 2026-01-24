# MCP Mock Product Specification: Chat Server

---

## 0. App Classification

| Property | Value | Notes |
|-----------|--------|-------|
| **Stateful** | Yes | Maintains messages and channel information in JSON files |
| **Mutable** | Yes | Can post messages, reply to threads, add reactions, and delete posts |
| **Visual** | No | Does not require a GUI or Computer Use |
| **Private** | No | Does not require authentication (uses configured user email) |
| **Mock Type** | Full Mock | Operates on local filesystem; simulates Mattermost/Slack-like chat |
| **Setup Flags** | `CHAT_DATA_ROOT`, `CURRENT_USER_EMAIL` | Environment variables for configuration |

---

## 1. Title

**Chat Server MCP**

---

## 2. Overview

The Chat Server provides Mattermost/Slack-like messaging capabilities with support for channels (groups/spaces), threaded conversations, emoji reactions, and user management. It enables AI agents to post messages, reply to threads, browse channel history, and manage team communications within a sandboxed filesystem environment.

---

## 3. Approach

### Data Source

- **Filesystem-based**: Messages and channels stored in JSON files within a configurable data directory (`CHAT_DATA_ROOT`)
- **No external APIs**: Self-contained chat simulation
- **User Identity**: Current user configured via environment variable

### Data Modes

| Mode | Description |
|------|-------------|
| **Clean Mode** | Empty workspace; channels and messages must be created |
| **Offline Mode** | Reactive-only; manages messages and channels that exist at request time |

### Core Architecture

| Component | Technology | Purpose |
|-----------|------------|---------|
| Channel Storage | JSON files | Store channel metadata and members |
| Message Storage | JSON files | Store messages per channel |
| Threading | Topic IDs | Group messages into conversation threads |
| User Management | JSON | Track user profiles and identities |

### Security

- **Path traversal protection**: All file paths resolved within data root
- **Sandboxed operations**: Cannot access files outside `CHAT_DATA_ROOT`

---

## 4. Prioritized User Archetypes and Workflows

| User Type | Goal / Workflow | Primary Tools |
|-----------|-----------------|---------------|
| **Team Communicator** | Post messages to team channels | `post_message` |
| **Thread Participant** | Reply to specific conversations | `reply_to_thread`, `get_thread_replies` |
| **Channel Browser** | Browse channels and message history | `list_channels`, `get_channel_history` |
| **Message Moderator** | Delete inappropriate messages | `delete_post` |
| **Reaction Giver** | React to messages with emojis | `add_reaction` |
| **User Manager** | Look up user profiles | `get_users`, `get_user_profile` |

---

## 5. Extended Functionality (Future Builds)

| Feature | Description | Priority |
|---------|-------------|----------|
| **Channel Creation** | Create new channels/groups | High |
| **Direct Messages** | Private 1:1 messaging | High |
| **Message Editing** | Edit posted messages | Medium |
| **File Attachments** | Attach files to messages | Medium |
| **Message Search** | Search messages by content | Medium |
| **User Mentions** | @mention notifications | Low |
| **Channel Archiving** | Archive inactive channels | Low |

---

## 6. Build Considerations

| Area | Description |
|------|-------------|
| **Storage Structure** | `Groups/{channel_id}/` with `group_info.json` and `messages.json` |
| **Message ID Format** | `Groups/{channel_id}/msg_{random}` |
| **Threading Model** | Messages with same `topic_id` form a thread |
| **Soft Delete** | Deleted messages marked as `DELETED`, not removed |
| **Pagination** | Channels and messages support limit/page pagination |
| **User Identity** | Current user determined by `CURRENT_USER_EMAIL` environment variable |
| **Dependencies** | Standard Python (no external packages required) |

---

## 7. Example MCP Commands

### List Channels

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `list_channels` | List all channels/groups | `limit`, `page` | GroupsListResponse with channel summaries |

```
list_channels(limit=10, page=0)

→ GroupsListResponse(
    groups=[
      {
        "id": "group_123",
        "name": "Engineering Team",
        "member_count": 5,
        "message_count": 142
      },
      {
        "id": "group_456",
        "name": "General",
        "member_count": 25,
        "message_count": 1024
      }
    ],
    total_count=8,
    page=0,
    per_page=10
  )
```

### Get Channel History

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `get_channel_history` | Get messages from a channel | `channel_id`, `limit`, `page` | GroupHistoryResponse with messages |

```
get_channel_history("group_123", limit=10, page=0)

→ GroupHistoryResponse(
    messages=[
      {
        "message_id": "Groups/group_123/msg_456",
        "creator_name": "John Doe",
        "creator_email": "john@example.com",
        "text": "Hello team! Here's the update...",
        "created_date": "2025-11-09T12:00:00",
        "topic_id": "topic_789",
        "reaction_count": 3,
        "is_deleted": false
      }
    ],
    has_next=true,
    has_prev=false,
    page=0,
    per_page=10
  )
```

### Post Message

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `post_message` | Post a new message (starts new thread) | `channel_id`, `message` | MessagePostResponse |

```
post_message("group_123", "Hello everyone! New update available.")

→ MessagePostResponse(
    message_id="Groups/group_123/msg_789",
    group_id="group_123",
    text="Hello everyone! New update available.",
    created_date="2025-11-09T14:30:00",
    topic_id="msg_789",
    is_reply=false
  )
```

### Reply to Thread

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `reply_to_thread` | Reply to an existing message thread | `channel_id`, `post_id`, `message` | MessagePostResponse |

```
reply_to_thread(
  "group_123",
  "Groups/group_123/msg_789",
  "Thanks for the update! I have a question..."
)

→ MessagePostResponse(
    message_id="Groups/group_123/msg_790",
    group_id="group_123",
    text="Thanks for the update! I have a question...",
    created_date="2025-11-09T14:35:00",
    topic_id="msg_789",
    is_reply=true
  )
```

### Add Reaction

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `add_reaction` | Add emoji reaction to message | `channel_id`, `post_id`, `emoji` | Success message |

```
add_reaction("group_123", "Groups/group_123/msg_789", "👍")

→ "Reaction added successfully"

add_reaction("group_123", "Groups/group_123/msg_789", "🎉")

→ "Reaction added successfully"
```

### Get Thread Replies

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `get_thread_replies` | Get all replies to a message | `channel_id`, `post_id` | List of reply messages |

```
get_thread_replies("group_123", "Groups/group_123/msg_789")

→ [
    {message_id: "msg_790", text: "Thanks for the update!...", ...},
    {message_id: "msg_791", text: "I agree, this is great!...", ...}
  ]
```

### Delete Post

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `delete_post` | Soft-delete a message | `channel_id`, `post_id` | Success message |

```
delete_post("group_123", "Groups/group_123/msg_789")

→ "Message deleted successfully"
```

### User Management

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `get_users` | List all workspace users | `limit`, `page` | List of user profiles |
| `get_user_profile` | Get specific user details | `email` | UserProfile |

```
get_users(limit=50, page=0)

→ [
    {"name": "John Doe", "email": "john@example.com", "user_type": "Human"},
    {"name": "Jane Smith", "email": "jane@example.com", "user_type": "Human"}
  ]

get_user_profile("john@example.com")

→ UserProfile(name="John Doe", email="john@example.com", user_type="Human")
```

### Message Structure

| Field | Description |
|-------|-------------|
| `message_id` | Unique ID: `Groups/{channel_id}/msg_{random}` |
| `creator` | User object with name, email, user_type |
| `created_date` | ISO timestamp |
| `text` | Message content |
| `topic_id` | Thread grouping ID (same as message_id for root messages) |
| `reactions` | Array of `{emoji, creator_email}` |
| `message_state` | `ACTIVE` or `DELETED` |

---

## 8. Notes and References

### Environment Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CHAT_DATA_ROOT` | `/.apps_data/chat` | Root directory for chat data |
| `CURRENT_USER_EMAIL` | Configured | Email of the current user for posting |
| `TOOLS` | All enabled | Comma-separated list to enable specific tools only |

### Key Behaviors

- **Threading**: Root messages have `topic_id == message_id.split("/")[-1]`; replies share parent's `topic_id`
- **Soft Delete**: Deleted messages remain in storage with `message_state: DELETED`
- **Reactions**: Multiple users can add same emoji; same user can add different emojis
- **Pagination**: Default limit 30 for history, 100 for channels

### Storage Structure

```
Groups/
  group_123/
    group_info.json    # Channel metadata and members
    messages.json      # All messages in the channel
  group_456/
    group_info.json
    messages.json
```

### Dependencies

- Standard Python libraries (no external packages required)

---


