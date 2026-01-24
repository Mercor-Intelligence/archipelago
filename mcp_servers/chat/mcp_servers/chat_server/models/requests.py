from pydantic import BaseModel, ConfigDict, Field


class ListChannelsRequest(BaseModel):
    """Request model for listing channels."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(
        default=100, ge=1, description="Maximum number of channels to return"
    )
    page: int = Field(default=0, ge=0, description="Page number (0-indexed)")


class GetChannelHistoryRequest(BaseModel):
    """Request model for getting channel history."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(..., description="Channel ID")
    limit: int = Field(
        default=30, ge=1, description="Maximum number of messages to return"
    )
    page: int = Field(default=0, ge=0, description="Page number (0-indexed)")


class PostMessageRequest(BaseModel):
    """Request model for posting a message."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(..., description="Channel ID")
    message: str = Field(..., description="Message text")


class ReplyToThreadRequest(BaseModel):
    """Request model for replying to a thread."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(..., description="Channel ID")
    post_id: str = Field(..., description="Post ID to reply to")
    message: str = Field(..., description="Reply message text")


class AddReactionRequest(BaseModel):
    """Request model for adding a reaction."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(..., description="Channel ID")
    post_id: str = Field(..., description="Post ID to react to")
    emoji_name: str = Field(..., description="Emoji name or unicode character")


class GetThreadRepliesRequest(BaseModel):
    """Request model for getting thread replies."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(..., description="Channel ID")
    post_id: str = Field(..., description="Post ID to get replies for")


class DeletePostRequest(BaseModel):
    """Request model for deleting a post."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(..., description="Channel ID")
    post_id: str = Field(..., description="Post ID to delete")


class GetUserProfileRequest(BaseModel):
    """Request model for getting user profile."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., description="User ID")


class GetUsersRequest(BaseModel):
    """Request model for getting users list."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(
        default=100, ge=1, description="Maximum number of users to return"
    )
    page: int = Field(default=0, ge=0, description="Page number (0-indexed)")
