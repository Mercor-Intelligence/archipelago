"""Meta-tools for LLM agents - consolidated interface with action-based routing."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Import existing tools for delegation
from tools.add_reaction import AddReactionRequest
from tools.add_reaction import add_reaction as _add_reaction
from tools.delete_post import DeletePostRequest
from tools.delete_post import delete_post as _delete_post
from tools.get_channel_history import (
    GetChannelHistoryRequest,
)
from tools.get_channel_history import (
    get_channel_history as _get_channel_history,
)
from tools.get_thread_replies import (
    GetThreadRepliesRequest,
)
from tools.get_thread_replies import (
    get_thread_replies as _get_thread_replies,
)
from tools.get_user_profile import (
    GetUserProfileRequest,
)
from tools.get_user_profile import (
    get_user_profile as _get_user_profile,
)
from tools.get_users import GetUsersRequest
from tools.get_users import get_users as _get_users
from tools.list_channels import ListChannelsRequest
from tools.list_channels import list_channels as _list_channels
from tools.post_message import PostMessageRequest
from tools.post_message import post_message as _post_message
from tools.reply_to_thread import (
    ReplyToThreadRequest,
)
from tools.reply_to_thread import (
    reply_to_thread as _reply_to_thread,
)


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
class ChannelsResult(BaseModel):
    """Result from listing channels."""

    model_config = ConfigDict(extra="forbid")
    channels: list[dict[str, Any]]
    total: int
    page: int
    limit: int


class HistoryResult(BaseModel):
    """Result from getting channel history."""

    model_config = ConfigDict(extra="forbid")
    messages: list[dict[str, Any]]
    channel_id: str
    total: int
    page: int


class MessageResult(BaseModel):
    """Result from posting/replying to a message."""

    model_config = ConfigDict(extra="forbid")
    message_id: str
    channel_id: str
    content: str
    timestamp: str


class ReactionResult(BaseModel):
    """Result from adding a reaction."""

    model_config = ConfigDict(extra="forbid")
    post_id: str
    emoji: str
    added: bool


class RepliesResult(BaseModel):
    """Result from getting thread replies."""

    model_config = ConfigDict(extra="forbid")
    replies: list[dict[str, Any]]
    root_message: dict[str, Any]
    total_replies: int


class UsersResult(BaseModel):
    """Result from listing users."""

    model_config = ConfigDict(extra="forbid")
    users: list[dict[str, Any]]
    total: int
    page: int


class ProfileResult(BaseModel):
    """Result from getting user profile."""

    model_config = ConfigDict(extra="forbid")
    user: dict[str, Any]


class DeleteResult(BaseModel):
    """Result from deleting a post."""

    model_config = ConfigDict(extra="forbid")
    post_id: str
    deleted: bool


# ============ Input Model ============
class ChatInput(BaseModel):
    """Input for chat meta-tool."""

    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "help",
        "list_channels",
        "get_history",
        "post",
        "reply",
        "react",
        "get_replies",
        "list_users",
        "get_profile",
        "delete",
    ] = Field(
        ...,
        description="Action to perform. REQUIRED. Use help to see available actions.",
    )

    # Channel operations
    channel_id: str | None = Field(None, description="Channel/group ID")

    # Message operations
    post_id: str | None = Field(None, description="Message/post ID")
    message: str | None = Field(None, description="Message content for post/reply")
    emoji: str | None = Field(None, description="Emoji for reaction (e.g., '👍')")

    # User operations
    user_id: str | None = Field(None, description="User ID for get_profile")

    # Pagination
    page: int | None = Field(None, description="Page number (0-indexed)")
    limit: int | None = Field(
        None, description="Results per page. Use with page for pagination."
    )


# ============ Output Model ============
class ChatOutput(BaseModel):
    """Output for chat meta-tool."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(..., description="Action that was performed")
    error: str | None = Field(None, description="Error message if failed")

    # Discovery
    help: HelpResponse | None = None

    # Action-specific results
    list_channels: ChannelsResult | None = None
    get_history: HistoryResult | None = None
    post: MessageResult | None = None
    reply: MessageResult | None = None
    react: ReactionResult | None = None
    get_replies: RepliesResult | None = None
    list_users: UsersResult | None = None
    get_profile: ProfileResult | None = None
    delete: DeleteResult | None = None


# ============ Help Definition ============
CHAT_HELP = HelpResponse(
    tool_name="chat",
    description="Chat operations: channels, messages, reactions, and users.",
    actions={
        "help": ActionInfo(
            description="List all available actions",
            required_params=[],
            optional_params=[],
        ),
        "list_channels": ActionInfo(
            description="List available channels/groups",
            required_params=[],
            optional_params=["page", "limit"],
        ),
        "get_history": ActionInfo(
            description="Get message history from a channel",
            required_params=["channel_id"],
            optional_params=["page", "limit"],
        ),
        "post": ActionInfo(
            description="Post a new message to a channel",
            required_params=["channel_id", "message"],
            optional_params=[],
        ),
        "reply": ActionInfo(
            description="Reply to a message thread",
            required_params=["channel_id", "post_id", "message"],
            optional_params=[],
        ),
        "react": ActionInfo(
            description="Add a reaction emoji to a message",
            required_params=["channel_id", "post_id", "emoji"],
            optional_params=[],
        ),
        "get_replies": ActionInfo(
            description="Get replies in a message thread",
            required_params=["channel_id", "post_id"],
            optional_params=[],
        ),
        "list_users": ActionInfo(
            description="List users in the workspace",
            required_params=[],
            optional_params=["page", "limit"],
        ),
        "get_profile": ActionInfo(
            description="Get a user's profile",
            required_params=["user_id"],
            optional_params=[],
        ),
        "delete": ActionInfo(
            description="Delete a message (soft delete)",
            required_params=["channel_id", "post_id"],
            optional_params=[],
        ),
    },
)


# ============ Meta-Tool Implementation ============
async def chat(request: ChatInput) -> ChatOutput:
    """Chat operations: channels, messages, reactions, and users."""
    match request.action:
        case "help":
            return ChatOutput(action="help", help=CHAT_HELP)

        case "list_channels":
            try:
                req = ListChannelsRequest(
                    page=request.page if request.page is not None else 0,
                    limit=request.limit if request.limit is not None else 20,
                )
                result = await _list_channels(req)
                return ChatOutput(
                    action="list_channels",
                    list_channels=ChannelsResult(
                        channels=[g.model_dump() for g in result.groups],
                        total=result.total_count,
                        page=result.page,
                        limit=result.per_page,
                    ),
                )
            except Exception as exc:
                return ChatOutput(action="list_channels", error=str(exc))

        case "get_history":
            if not request.channel_id:
                return ChatOutput(action="get_history", error="Required: channel_id")
            try:
                req = GetChannelHistoryRequest(
                    channel_id=request.channel_id,
                    page=request.page if request.page is not None else 0,
                    limit=request.limit if request.limit is not None else 20,
                )
                result = await _get_channel_history(req)
                return ChatOutput(
                    action="get_history",
                    get_history=HistoryResult(
                        messages=[m.model_dump() for m in result.messages],
                        channel_id=request.channel_id,
                        total=len(result.messages),
                        page=result.page,
                    ),
                )
            except Exception as exc:
                return ChatOutput(action="get_history", error=str(exc))

        case "post":
            if not request.channel_id or request.message is None:
                return ChatOutput(action="post", error="Required: channel_id, message")
            try:
                req = PostMessageRequest(
                    channel_id=request.channel_id,
                    message=request.message,
                )
                result = await _post_message(req)
                return ChatOutput(
                    action="post",
                    post=MessageResult(
                        message_id=result.message_id,
                        channel_id=result.group_id,
                        content=result.text,
                        timestamp=result.created_date,
                    ),
                )
            except Exception as exc:
                return ChatOutput(action="post", error=str(exc))

        case "reply":
            if not request.channel_id or not request.post_id or request.message is None:
                return ChatOutput(
                    action="reply", error="Required: channel_id, post_id, message"
                )
            try:
                req = ReplyToThreadRequest(
                    channel_id=request.channel_id,
                    post_id=request.post_id,
                    message=request.message,
                )
                result = await _reply_to_thread(req)
                return ChatOutput(
                    action="reply",
                    reply=MessageResult(
                        message_id=result.message_id,
                        channel_id=result.group_id,
                        content=result.text,
                        timestamp=result.created_date,
                    ),
                )
            except Exception as exc:
                return ChatOutput(action="reply", error=str(exc))

        case "react":
            if not request.channel_id or not request.post_id or not request.emoji:
                return ChatOutput(
                    action="react", error="Required: channel_id, post_id, emoji"
                )
            try:
                req = AddReactionRequest(
                    channel_id=request.channel_id,
                    post_id=request.post_id,
                    emoji_name=request.emoji,
                )
                result = await _add_reaction(req)
                return ChatOutput(
                    action="react",
                    react=ReactionResult(
                        post_id=result.post_id,
                        emoji=result.emoji_name,
                        added=True,
                    ),
                )
            except Exception as exc:
                return ChatOutput(action="react", error=str(exc))

        case "get_replies":
            if not request.channel_id or not request.post_id:
                return ChatOutput(
                    action="get_replies", error="Required: channel_id, post_id"
                )
            try:
                req = GetThreadRepliesRequest(
                    channel_id=request.channel_id,
                    post_id=request.post_id,
                )
                result = await _get_thread_replies(req)
                return ChatOutput(
                    action="get_replies",
                    get_replies=RepliesResult(
                        replies=[r.model_dump() for r in result.posts],
                        root_message=result.root_post.model_dump()
                        if result.root_post
                        else {},
                        total_replies=len(result.posts),
                    ),
                )
            except Exception as exc:
                return ChatOutput(action="get_replies", error=str(exc))

        case "list_users":
            try:
                req = GetUsersRequest(
                    page=request.page if request.page is not None else 0,
                    limit=request.limit if request.limit is not None else 20,
                )
                result = await _get_users(req)
                return ChatOutput(
                    action="list_users",
                    list_users=UsersResult(
                        users=[u.model_dump() for u in result.users],
                        total=result.total_count,
                        page=result.page,
                    ),
                )
            except Exception as exc:
                return ChatOutput(action="list_users", error=str(exc))

        case "get_profile":
            if not request.user_id:
                return ChatOutput(action="get_profile", error="Required: user_id")
            try:
                req = GetUserProfileRequest(user_id=request.user_id)
                result = await _get_user_profile(req)
                return ChatOutput(
                    action="get_profile",
                    get_profile=ProfileResult(user=result.model_dump()),
                )
            except Exception as exc:
                return ChatOutput(action="get_profile", error=str(exc))

        case "delete":
            if not request.channel_id or not request.post_id:
                return ChatOutput(
                    action="delete", error="Required: channel_id, post_id"
                )
            try:
                req = DeletePostRequest(
                    channel_id=request.channel_id,
                    post_id=request.post_id,
                )
                result = await _delete_post(req)
                return ChatOutput(
                    action="delete",
                    delete=DeleteResult(
                        post_id=result.post_id,
                        deleted=True,
                    ),
                )
            except Exception as exc:
                return ChatOutput(action="delete", error=str(exc))

        case _:
            return ChatOutput(
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
    "input": ChatInput,
    "output": ChatOutput,
    "ChannelsResult": ChannelsResult,
    "HistoryResult": HistoryResult,
    "MessageResult": MessageResult,
    "ReactionResult": ReactionResult,
    "RepliesResult": RepliesResult,
    "UsersResult": UsersResult,
    "ProfileResult": ProfileResult,
    "DeleteResult": DeleteResult,
}


def chat_schema(request: SchemaInput) -> SchemaOutput:
    """Get JSON schema for chat input/output models."""
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
