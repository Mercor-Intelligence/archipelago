"""Attachment MCP tools for Workday Help."""

from loguru import logger
from mcp_auth import get_current_user
from schemas.help.attachment_schemas import (
    AddAttachmentRequest,
    AddAttachmentResponse,
    AttachmentSummary,
    ListAttachmentsRequest,
    ListAttachmentsResponse,
)
from services.attachment_service import AttachmentService
from utils.decorators import make_async_background
from validators.business_rules import SUPPORTED_PERSONAS

_attachment_service = AttachmentService()


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
def workday_help_attachments_add(request: AddAttachmentRequest) -> AddAttachmentResponse:
    """Add attachment metadata to a case."""
    actor_persona = _derive_persona(request.actor_persona)
    logger.info(
        f"Adding attachment: case_id={request.case_id}, filename={request.filename}, "
        f"persona={actor_persona}"
    )

    try:
        attachment = _attachment_service.add_attachment(
            case_id=request.case_id,
            filename=request.filename,
            uploader=request.uploader,
            mime_type=request.mime_type,
            size_bytes=request.size_bytes,
            source=request.source,
            external_reference=request.external_reference,
            metadata=request.metadata,
            actor_persona=actor_persona,
        )

        return AddAttachmentResponse(
            attachment_id=attachment["attachment_id"],
            case_id=attachment["case_id"],
            filename=attachment["filename"],
            mime_type=attachment["mime_type"],
            source=attachment["source"],
            external_reference=attachment["external_reference"],
            size_bytes=attachment["size_bytes"],
            uploader=attachment["uploader"],
            uploaded_at=attachment["uploaded_at"],
            metadata=attachment["metadata"],
        )
    except ValueError as e:
        logger.error(f"Error adding attachment: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error adding attachment: {e}")
        raise ValueError(f"E_GEN_001: Failed to add attachment: {e}") from e


@make_async_background
def workday_help_attachments_list(request: ListAttachmentsRequest) -> ListAttachmentsResponse:
    """List attachments for a case with pagination."""
    actor_persona = _derive_persona(request.actor_persona)
    logger.info(f"Listing attachments: case_id={request.case_id}, persona={actor_persona}")

    try:
        result = _attachment_service.list_attachments(
            case_id=request.case_id,
            cursor=request.cursor,
            limit=request.limit,
        )

        attachments = [
            AttachmentSummary(
                attachment_id=a["attachment_id"],
                case_id=a["case_id"],
                filename=a["filename"],
                mime_type=a["mime_type"],
                source=a["source"],
                external_reference=a["external_reference"],
                size_bytes=a["size_bytes"],
                uploader=a["uploader"],
                uploaded_at=a["uploaded_at"],
                metadata=a["metadata"],
            )
            for a in result["attachments"]
        ]

        return ListAttachmentsResponse(
            attachments=attachments,
            next_cursor=result["next_cursor"],
            has_more=result["has_more"],
            limit=result["limit"],
        )
    except ValueError as e:
        logger.error(f"Error listing attachments: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error listing attachments: {e}")
        raise ValueError(f"E_GEN_001: Failed to list attachments: {e}") from e
