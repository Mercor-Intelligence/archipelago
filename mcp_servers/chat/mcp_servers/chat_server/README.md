# Rls Chat MCP Server

Mercor RLS Mattermost MCP

## Tools

### 1. `chat`

Chat operations: channels, messages, reactions, and users.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'list_channels', 'get_history', 'post', 'reply', 'react', 'get_replies', 'list_users', 'get_profile', 'delete'] | Ellipsis | Action to perform. REQUIRED. Use help to see available actions. |
| `channel_id` | string? | null | Channel/group ID |
| `post_id` | string? | null | Message/post ID |
| `message` | string? | null | Message content for post/reply |
| `emoji` | string? | null | Emoji for reaction (e.g., '👍') |
| `user_id` | string? | null | User ID for get_profile |
| `page` | integer? | null | Page number (0-indexed) |
| `limit` | integer? | null | Results per page. Use with page for pagination. |

---

### 2. `chat_schema`

Get JSON schema for chat input/output models.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `model` | string | Ellipsis | Model name: 'input', 'output', or a result type |

---
