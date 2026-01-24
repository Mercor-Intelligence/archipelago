from pydantic import BaseModel, ConfigDict


class GroupInfoResponse(BaseModel):
    """Individual group information"""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    member_count: int = 0
    message_count: int = 0

    def __str__(self) -> str:
        return f"Group: {self.name} (ID: {self.id}, Members: {self.member_count}, Messages: {self.message_count})"


class GroupsListResponse(BaseModel):
    """Response for listing groups"""

    model_config = ConfigDict(extra="forbid")

    groups: list[GroupInfoResponse]
    total_count: int
    page: int
    per_page: int
    error: str | None = None

    def __str__(self) -> str:
        if self.error:
            return f"Failed to list groups: {self.error}"

        if not self.groups:
            return "No groups found"

        lines = [
            f"Found {self.total_count} group(s) (page {self.page + 1}, showing {len(self.groups)}):",
            "",
        ]

        for idx, group in enumerate(self.groups, 1):
            lines.append(f"{idx}. {group.name}")
            lines.append(f"   ID: {group.id}")
            lines.append(f"   Members: {group.member_count}")
            lines.append(f"   Messages: {group.message_count}")
            lines.append("")

        return "\n".join(lines).strip()


class MessageInfo(BaseModel):
    """Individual message information (Google Chat format)"""

    model_config = ConfigDict(extra="ignore")

    message_id: str
    creator_name: str
    creator_email: str
    text: str
    created_date: str
    topic_id: str
    reaction_count: int = 0
    is_deleted: bool = False

    def __str__(self) -> str:
        lines = [
            f"Message ID: {self.message_id}",
            f"From: {self.creator_name} ({self.creator_email})",
            f"Posted: {self.created_date}",
        ]
        if self.is_deleted:
            lines.append("Status: DELETED")
        else:
            if self.reaction_count > 0:
                lines.append(f"Reactions: {self.reaction_count}")
            lines.append("")
            lines.append(self.text)
        return "\n".join(lines)


class GroupHistoryResponse(BaseModel):
    """Response for group message history"""

    model_config = ConfigDict(extra="forbid")

    messages: list[MessageInfo]
    has_next: bool
    has_prev: bool
    page: int
    per_page: int
    error: str | None = None

    def __str__(self) -> str:
        if self.error:
            return f"Failed to get group history: {self.error}"

        if not self.messages:
            return "No messages found in this group"

        lines = [
            f"Group History (page {self.page + 1}, {len(self.messages)} message(s)):",
            "",
        ]

        for idx, msg in enumerate(self.messages, 1):
            status = " [DELETED]" if msg.is_deleted else ""
            lines.append(f"{idx}. {msg.created_date}{status}")
            lines.append(f"   From: {msg.creator_name}")
            lines.append(f"   Message ID: {msg.message_id}")
            if not msg.is_deleted:
                preview = msg.text[:100] + "..." if len(msg.text) > 100 else msg.text
                lines.append(f"   {preview}")
            lines.append("")

        nav_info = []
        if self.has_prev:
            nav_info.append("← Previous page available")
        if self.has_next:
            nav_info.append("Next page available →")
        if nav_info:
            lines.append(" | ".join(nav_info))

        return "\n".join(lines).strip()


class MessagePostResponse(BaseModel):
    """Response for posting a message"""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    group_id: str
    text: str
    created_date: str
    topic_id: str
    is_reply: bool = False
    error: str | None = None

    def __str__(self) -> str:
        if self.error:
            return f"Failed to post message: {self.error}"

        post_type = "Reply posted" if self.is_reply else "Message posted"
        lines = [
            f"{post_type} successfully!",
            f"Message ID: {self.message_id}",
            f"Group ID: {self.group_id}",
            f"Posted at: {self.created_date}",
            "",
            f"Message: {self.text}",
        ]
        return "\n".join(lines)


class ReactionResponse(BaseModel):
    """Response for adding a reaction"""

    model_config = ConfigDict(extra="forbid")

    post_id: str
    user_id: str
    emoji_name: str
    create_at: str  # ISO format datetime
    error: str | None = None

    def __str__(self) -> str:
        if self.error:
            return f"Failed to add reaction: {self.error}"

        return (
            f"Reaction added successfully!\n"
            f"Emoji: :{self.emoji_name}:\n"
            f"Post ID: {self.post_id}\n"
            f"Added at: {self.create_at}"
        )


class DeletePostResponse(BaseModel):
    """Response for deleting a post"""

    model_config = ConfigDict(extra="forbid")

    post_id: str
    deleted_replies: int = 0
    deleted_reactions: int = 0
    error: str | None = None

    def __str__(self) -> str:
        if self.error:
            return f"Failed to delete post: {self.error}"

        lines = [
            f"Post {self.post_id} deleted successfully!",
            f"- Deleted {self.deleted_replies} thread reply/replies",
            f"- Deleted {self.deleted_reactions} reaction(s)",
        ]
        return "\n".join(lines)


class ThreadRepliesResponse(BaseModel):
    """Response for getting thread replies"""

    model_config = ConfigDict(extra="forbid")

    posts: list[MessageInfo]
    root_post: MessageInfo | None = None
    error: str | None = None

    def __str__(self) -> str:
        if self.error:
            return f"Failed to get thread replies: {self.error}"

        if not self.posts:
            return "No replies found in this thread"

        lines = [f"Thread with {len(self.posts)} message(s):", ""]

        if self.root_post:
            lines.append("=== ORIGINAL POST ===")
            lines.append(
                f"From: {self.root_post.creator_name} ({self.root_post.creator_email})"
            )
            lines.append(f"Posted: {self.root_post.created_date}")
            if not self.root_post.is_deleted:
                lines.append(self.root_post.text)
            else:
                lines.append("[DELETED]")
            lines.append("")

        if len(self.posts) > 0:
            lines.append("=== REPLIES ===")
            for idx, msg in enumerate(self.posts, 1):
                status = " [DELETED]" if msg.is_deleted else ""
                lines.append(f"{idx}. {msg.created_date}{status}")
                lines.append(f"   From: {msg.creator_name}")
                if not msg.is_deleted:
                    lines.append(f"   {msg.text}")
                lines.append("")

        return "\n".join(lines).strip()


class UserInfo(BaseModel):
    """Individual user information"""

    model_config = ConfigDict(extra="ignore")

    id: str
    username: str
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    nickname: str = ""
    position: str = ""
    roles: str = ""
    is_bot: bool = False

    def __str__(self) -> str:
        full_name = f"{self.first_name} {self.last_name}".strip()
        lines = [
            f"User: {self.username}",
            f"ID: {self.id}",
        ]
        if full_name:
            lines.append(f"Name: {full_name}")
        if self.nickname:
            lines.append(f"Nickname: {self.nickname}")
        if self.email:
            lines.append(f"Email: {self.email}")
        if self.position:
            lines.append(f"Position: {self.position}")
        if self.is_bot:
            lines.append("Type: Bot")
        return "\n".join(lines)


class UsersListResponse(BaseModel):
    """Response for listing users"""

    model_config = ConfigDict(extra="forbid")

    users: list[UserInfo]
    total_count: int
    page: int
    per_page: int
    error: str | None = None

    def __str__(self) -> str:
        if self.error:
            return f"Failed to list users: {self.error}"

        if not self.users:
            return "No users found"

        lines = [
            f"Found {self.total_count} user(s) (page {self.page + 1}, showing {len(self.users)}):",
            "",
        ]

        for idx, user in enumerate(self.users, 1):
            full_name = f"{user.first_name} {user.last_name}".strip()
            bot_marker = " [BOT]" if user.is_bot else ""
            lines.append(f"{idx}. @{user.username}{bot_marker}")
            lines.append(f"   ID: {user.id}")
            if full_name:
                lines.append(f"   Name: {full_name}")
            if user.email:
                lines.append(f"   Email: {user.email}")
            lines.append("")

        return "\n".join(lines).strip()


class UserProfileResponse(BaseModel):
    """Response for user profile"""

    model_config = ConfigDict(extra="forbid")

    id: str
    username: str
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    nickname: str = ""
    position: str = ""
    roles: str = ""
    locale: str = ""
    timezone: dict = {}
    is_bot: bool = False
    bot_description: str = ""
    last_picture_update: int = 0
    create_at: str | None = None
    update_at: str | None = None
    error: str | None = None

    def __str__(self) -> str:
        if self.error:
            return f"Failed to get user profile: {self.error}"

        full_name = f"{self.first_name} {self.last_name}".strip()

        lines = [
            "=== USER PROFILE ===",
            f"Username: @{self.username}",
            f"ID: {self.id}",
        ]

        if full_name:
            lines.append(f"Name: {full_name}")
        if self.nickname:
            lines.append(f"Nickname: {self.nickname}")
        if self.email:
            lines.append(f"Email: {self.email}")
        if self.position:
            lines.append(f"Position: {self.position}")

        lines.append("")
        lines.append(f"Bot: {'Yes' if self.is_bot else 'No'}")
        if self.is_bot and self.bot_description:
            lines.append(f"Bot Description: {self.bot_description}")

        if self.roles:
            lines.append(f"Roles: {self.roles}")
        if self.locale:
            lines.append(f"Locale: {self.locale}")

        if self.create_at:
            lines.append(f"Created: {self.create_at}")
        if self.update_at:
            lines.append(f"Updated: {self.update_at}")

        return "\n".join(lines)


class ErrorResponse(BaseModel):
    """Generic error response"""

    model_config = ConfigDict(extra="forbid")

    error: str

    def __str__(self) -> str:
        return f"Error: {self.error}"
