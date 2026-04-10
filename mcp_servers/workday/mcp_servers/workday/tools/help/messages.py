"""Message MCP tools for Workday Help."""

from loguru import logger
from mcp_auth import get_current_user
from schemas.help.message_schemas import (
    AddMessageRequest,
    AddMessageResponse,
    GetMessageResponse,
    SearchMessagesRequest,
    SearchMessagesResponse,
)
from services.message_service import MessageService
from utils.decorators import make_async_background
from validators.business_rules import SUPPORTED_PERSONAS

_message_service = MessageService()


def _derive_persona(
    actor_persona: str | None,
    *,
    default_persona: str = "case_owner",
) -> str:
    """Derive a valid persona from request or user roles.

    Searches user's roles for a compatible persona from SUPPORTED_PERSONAS.
    Falls back to default_persona if no compatible role is found.
    """
    if actor_persona:
        return actor_persona

    user = get_current_user()
    user_roles = user.get("roles") or []
    for role in user_roles:
        if role in SUPPORTED_PERSONAS:
            return role

    return default_persona


@make_async_background
def workday_help_messages_add(request: AddMessageRequest) -> AddMessageResponse:
    """Add a message to a case."""
    actor_persona = _derive_persona(request.actor_persona)
    logger.info(
        f"Adding message: case_id={request.case_id}, direction={request.direction}, "
        f"persona={actor_persona}"
    )

    try:
        message = _message_service.add_message(
            case_id=request.case_id,
            direction=request.direction,
            sender=request.sender,
            body=request.body,
            actor=request.actor,
            audience=request.audience,
            metadata=request.metadata,
            actor_persona=actor_persona,
        )

        return AddMessageResponse(
            message_id=message["message_id"],
            case_id=message["case_id"],
            direction=message["direction"],
            sender=message["sender"],
            audience=message["audience"],
            body=message["body"],
            created_at=message["created_at"],
            metadata=message["metadata"],
        )
    except ValueError as e:
        logger.error(f"Error adding message: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error adding message: {e}")
        raise ValueError(f"E_GEN_001: Failed to add message: {e}") from e


@make_async_background
def workday_help_messages_search(request: SearchMessagesRequest) -> SearchMessagesResponse:
    """Search messages by filters with pagination."""
    logger.info(
        f"Searching messages: case_id={request.case_id}, direction={request.direction}, "
        f"sender={request.sender}"
    )

    try:
        result = _message_service.search_messages(
            message_id=request.message_id,
            case_id=request.case_id,
            direction=request.direction,
            sender=request.sender,
            created_after=request.created_after,
            created_before=request.created_before,
            cursor=request.cursor,
            limit=request.limit,
        )

        messages = [
            GetMessageResponse(
                message_id=m["message_id"],
                case_id=m["case_id"],
                direction=m["direction"],
                sender=m["sender"],
                audience=m["audience"],
                body=m["body"],
                created_at=m["created_at"],
                metadata=m["metadata"],
            )
            for m in result["messages"]
        ]

        return SearchMessagesResponse(
            messages=messages,
            next_cursor=result["next_cursor"],
            has_more=result["has_more"],
            limit=result["limit"],
        )
    except ValueError as e:
        logger.error(f"Error searching messages: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error searching messages: {e}")
        raise ValueError(f"E_GEN_001: Failed to search messages: {e}") from e
