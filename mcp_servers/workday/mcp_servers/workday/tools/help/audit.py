"""Audit MCP tool for Workday Help."""

from loguru import logger
from schemas.help.audit_schemas import (
    AuditEntry,
    QueryAuditHistoryRequest,
    QueryAuditHistoryResponse,
)
from services.audit_service import AuditService
from utils.decorators import make_async_background

# Initialize service
_audit_service = AuditService()


@make_async_background
def workday_help_audit_query_history(
    request: QueryAuditHistoryRequest,
) -> QueryAuditHistoryResponse:
    """Query audit history with filters and pagination."""
    logger.info(
        f"Querying audit history: case_id={request.case_id}, "
        f"actor={request.actor}, action_type={request.action_type}"
    )

    try:
        result = _audit_service.query_history(
            case_id=request.case_id,
            actor=request.actor,
            action_type=request.action_type,
            created_after=request.created_after,
            created_before=request.created_before,
            cursor=request.cursor,
            limit=request.limit,
        )

        entries = [
            AuditEntry(
                log_id=e["log_id"],
                case_id=e["case_id"],
                entity_type=e["entity_type"],
                entity_id=e["entity_id"],
                action=e["action"],
                actor=e["actor"],
                actor_persona=e["actor_persona"],
                created_at=e["created_at"],
                changes=e["changes"],
                rationale=e["rationale"],
                metadata=e["metadata"],
            )
            for e in result["audit_log"]
        ]

        return QueryAuditHistoryResponse(
            audit_log=entries,
            next_cursor=result["next_cursor"],
            has_more=result["has_more"],
            limit=result["limit"],
        )
    except ValueError as e:
        logger.error(f"Error querying audit history: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error querying audit history: {e}")
        raise ValueError(f"E_GEN_001: Failed to query audit history: {e}") from e
