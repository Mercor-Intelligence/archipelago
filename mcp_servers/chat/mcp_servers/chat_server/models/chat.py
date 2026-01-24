from typing import Literal

from pydantic import BaseModel, Field


class UserProfile(BaseModel):
    """User profile information (Google Chat format)"""

    name: str
    email: str
    user_type: str = "Human"  # "Human" or "Bot"


class MembershipInfo(BaseModel):
    """User's group membership information"""

    group_name: str
    group_id: str
    membership_state: str = "MEMBER_JOINED"


class UserInfo(BaseModel):
    """Complete user information including memberships"""

    user: UserProfile
    membership_info: list[MembershipInfo] = Field(default_factory=list)


class GroupMember(BaseModel):
    """Group member information"""

    name: str
    email: str
    user_type: str = "Human"


class GroupInfo(BaseModel):
    """Group/Space information"""

    name: str
    members: list[GroupMember]


class EmojiReaction(BaseModel):
    """Emoji information for a reaction"""

    unicode: str


class MessageReaction(BaseModel):
    """Message reaction"""

    emoji: EmojiReaction
    reactor_emails: list[str]


class DriveMetadata(BaseModel):
    """Google Drive file metadata in annotations"""

    id: str
    title: str
    thumbnail_url: str = ""


class FormatMetadata(BaseModel):
    """Text formatting metadata"""

    format_type: str  # e.g., "BOLD", "ITALIC", "BULLETED_LIST", etc.


class InteractionData(BaseModel):
    """Interaction data for links"""

    url: dict[str, str] = Field(default_factory=dict)


class VideoCallMetadata(BaseModel):
    """Video call meeting metadata"""

    meeting_space: dict[str, str] = Field(default_factory=dict)


class Annotation(BaseModel):
    """Message annotation (links, formatting, attachments)"""

    start_index: int
    length: int
    drive_metadata: DriveMetadata | None = None
    format_metadata: FormatMetadata | None = None
    interaction_data: InteractionData | None = None
    video_call_metadata: VideoCallMetadata | None = None


class DeletionMetadata(BaseModel):
    """Information about message deletion"""

    deletion_type: str  # e.g., "CREATOR", "ADMIN"


class ChatMessage(BaseModel):
    """Chat message (Google Chat format)"""

    creator: UserProfile | None = None
    created_date: str | None = None
    text: str = ""
    topic_id: str
    message_id: str
    reactions: list[MessageReaction] = Field(default_factory=list)
    annotations: list[Annotation] = Field(default_factory=list)
    message_state: Literal["DELETED"] | None = None
    deleted_date: str | None = None
    deletion_metadata: DeletionMetadata | None = None


class MessagesContainer(BaseModel):
    """Container for all messages in a group"""

    messages: list[ChatMessage] = Field(default_factory=list)
