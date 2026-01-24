from loguru import logger
from models.chat import ChatMessage, GroupInfo, MessagesContainer, UserProfile
from models.requests import ReplyToThreadRequest
from models.responses import MessagePostResponse
from utils.config import CURRENT_USER_EMAIL
from utils.decorators import make_async_background
from utils.storage import (
    generate_message_id,
    get_formatted_date,
    load_json,
    save_json,
)


@make_async_background
def reply_to_thread(request: ReplyToThreadRequest) -> MessagePostResponse:
    """Reply to a specific message thread in a group."""
    try:
        group_info_data = load_json(f"Groups/{request.channel_id}", "group_info.json")
        if not group_info_data:
            raise ValueError(f"Group {request.channel_id} not found")

        group_info = GroupInfo.model_validate(group_info_data)

        messages_data = load_json(f"Groups/{request.channel_id}", "messages.json")
        if not messages_data:
            raise ValueError(f"Messages file not found for group {request.channel_id}")

        messages_container = MessagesContainer.model_validate(messages_data)

        parent_message = None
        for msg in messages_container.messages:
            if msg.message_id == request.post_id:
                parent_message = msg
                break

        if not parent_message:
            raise ValueError(f"Message {request.post_id} not found")

        current_user = None
        for member in group_info.members:
            if member.email == CURRENT_USER_EMAIL:
                current_user = UserProfile(
                    name=member.name,
                    email=member.email,
                    user_type=member.user_type,
                )
                break

        if not current_user:
            current_user = UserProfile(
                name="Current User",
                email=CURRENT_USER_EMAIL,
                user_type="Human",
            )

        topic_id = parent_message.topic_id
        message_id = generate_message_id(request.channel_id, topic_id, is_reply=True)
        created_date = get_formatted_date()

        reply_message = ChatMessage(
            creator=current_user,
            created_date=created_date,
            text=request.message,
            topic_id=topic_id,
            message_id=message_id,
        )

        messages_container.messages.append(reply_message)

        save_json(
            f"Groups/{request.channel_id}",
            "messages.json",
            messages_container.model_dump(),
        )

        return MessagePostResponse(
            message_id=message_id,
            group_id=request.channel_id,
            text=request.message,
            created_date=created_date,
            topic_id=topic_id,
            is_reply=True,
        )

    except Exception as e:
        logger.error(f"Error replying to thread: {e}")
        raise ValueError(f"Error replying to thread: {e}") from e
