"""Activity logging service for Greenhouse MCP Server.

Provides functions to log activities for mutation operations, including:
- What changed
- Who made the change (persona)
- When it happened
- Related entities (candidate_id, application_id, etc.)
"""

from datetime import UTC, datetime

from db.models.activity import Activity
from mcp_middleware import log_activity as log_activity_to_logger
from sqlalchemy.ext.asyncio import AsyncSession


async def create_activity(
    session: AsyncSession,
    candidate_id: int,
    subject: str,
    body: str | None = None,
    user_id: int | None = None,
    persona: str | None = None,
    application_id: int | None = None,
) -> Activity:
    """Create an activity feed entry.

    Args:
        session: Database session
        candidate_id: ID of the candidate this activity relates to
        subject: Activity subject/title (e.g., "Application submitted")
        body: Optional detailed description
        user_id: ID of the user who performed the action
        persona: Persona who performed the action
        application_id: ID of the related application (if applicable)

    Returns:
        Created Activity instance
    """
    # Create timestamp
    created_at = datetime.now(UTC).isoformat()

    # Create activity record
    activity = Activity(
        candidate_id=candidate_id,
        application_id=application_id,
        user_id=user_id,
        subject=subject,
        body=body,
        created_at=created_at,
    )

    session.add(activity)
    await session.flush()

    # Also log to application logs
    log_activity_to_logger(
        action=subject,
        persona=persona,
        candidate_id=candidate_id,
        details={"body": body, "user_id": user_id, "application_id": application_id},
    )

    return activity


async def log_candidate_created(
    session: AsyncSession,
    candidate_id: int,
    persona: str | None = None,
    user_id: int | None = None,
) -> None:
    """Log activity when a candidate is created.

    Args:
        session: Database session
        candidate_id: ID of the created candidate
        persona: Persona who created the candidate
        user_id: ID of the user who created the candidate
    """
    await create_activity(
        session=session,
        candidate_id=candidate_id,
        subject="Candidate created",
        body=f"Candidate was created by {persona or 'system'}",
        user_id=user_id,
        persona=persona,
    )


async def log_candidate_updated(
    session: AsyncSession,
    candidate_id: int,
    changes: dict[str, tuple[str | None, str | None]],
    persona: str | None = None,
    user_id: int | None = None,
) -> None:
    """Log activity when a candidate is updated.

    Args:
        session: Database session
        candidate_id: ID of the updated candidate
        changes: Dictionary of field -> (old_value, new_value) pairs
        persona: Persona who updated the candidate
        user_id: ID of the user who updated the candidate
    """
    change_summary = ", ".join(f"{field}: {old} → {new}" for field, (old, new) in changes.items())

    await create_activity(
        session=session,
        candidate_id=candidate_id,
        subject="Candidate updated",
        body=f"Updated by {persona or 'system'}: {change_summary}",
        user_id=user_id,
        persona=persona,
    )


async def log_application_created(
    session: AsyncSession,
    candidate_id: int,
    application_id: int,
    job_name: str,
    persona: str | None = None,
    user_id: int | None = None,
) -> None:
    """Log activity when an application is created.

    Args:
        session: Database session
        candidate_id: ID of the candidate
        application_id: ID of the created application
        job_name: Name of the job applied for
        persona: Persona who created the application
        user_id: ID of the user who created the application
    """
    await create_activity(
        session=session,
        candidate_id=candidate_id,
        subject="Application submitted",
        body=f"Applied to {job_name}",
        user_id=user_id,
        persona=persona,
        application_id=application_id,
    )


async def log_application_stage_change(
    session: AsyncSession,
    candidate_id: int,
    application_id: int,
    old_stage: str,
    new_stage: str,
    persona: str | None = None,
    user_id: int | None = None,
) -> None:
    """Log activity when an application moves to a new stage.

    Args:
        session: Database session
        candidate_id: ID of the candidate
        application_id: ID of the application
        old_stage: Previous stage name
        new_stage: New stage name
        persona: Persona who advanced the application
        user_id: ID of the user who advanced the application
    """
    await create_activity(
        session=session,
        candidate_id=candidate_id,
        subject="Application stage changed",
        body=f"Moved from {old_stage} to {new_stage} by {persona or 'system'}",
        user_id=user_id,
        persona=persona,
        application_id=application_id,
    )


async def log_application_rejected(
    session: AsyncSession,
    candidate_id: int,
    application_id: int,
    reason: str | None = None,
    persona: str | None = None,
    user_id: int | None = None,
    candidate_name: str | None = None,
    job_name: str | None = None,
) -> None:
    """Log activity when an application is rejected.

    Args:
        session: Database session
        candidate_id: ID of the candidate
        application_id: ID of the application
        reason: Optional rejection reason
        persona: Persona who rejected the application
        user_id: ID of the user who rejected the application
        candidate_name: Name of the candidate being rejected
        job_name: Name of the job the candidate was rejected for
    """
    # Build descriptive body: "{candidate} was rejected for {job}"
    if candidate_name and job_name:
        body = f"{candidate_name} was rejected for {job_name}"
    elif candidate_name:
        body = f"{candidate_name} was rejected"
    elif job_name:
        body = f"Candidate was rejected for {job_name}"
    else:
        body = "Application rejected"

    if persona:
        body += f" by {persona}"
    if reason:
        body += f": {reason}"

    await create_activity(
        session=session,
        candidate_id=candidate_id,
        subject="Application rejected",
        body=body,
        user_id=user_id,
        persona=persona,
        application_id=application_id,
    )


async def log_application_hired(
    session: AsyncSession,
    candidate_id: int,
    application_id: int,
    start_date: str | None = None,
    persona: str | None = None,
    user_id: int | None = None,
) -> None:
    """Log activity when a candidate is hired.

    Args:
        session: Database session
        candidate_id: ID of the candidate
        application_id: ID of the application
        start_date: Optional start date
        persona: Persona who marked as hired
        user_id: ID of the user who marked as hired
    """
    body = f"Candidate hired by {persona or 'system'}"
    if start_date:
        body += f" (start date: {start_date})"

    await create_activity(
        session=session,
        candidate_id=candidate_id,
        subject="Candidate hired",
        body=body,
        user_id=user_id,
        persona=persona,
        application_id=application_id,
    )


async def log_note_added(
    session: AsyncSession,
    candidate_id: int,
    note_preview: str,
    persona: str | None = None,
    user_id: int | None = None,
    candidate_name: str | None = None,
) -> None:
    """Log activity when a note is added to a candidate.

    Args:
        session: Database session
        candidate_id: ID of the candidate
        note_preview: Preview of the note content (first 100 chars)
        persona: Persona who added the note
        user_id: ID of the user who added the note
        candidate_name: Full name of the candidate (used in the subject)
    """
    await create_activity(
        session=session,
        candidate_id=candidate_id,
        subject=(f"Note added on {candidate_name}" if candidate_name else "Note added"),
        body=f"{persona or 'System'} added a note: {note_preview}",
        user_id=user_id,
        persona=persona,
    )


async def log_tag_added(
    session: AsyncSession,
    candidate_id: int,
    tag_name: str,
    persona: str | None = None,
    user_id: int | None = None,
) -> None:
    """Log activity when a tag is added to a candidate.

    Args:
        session: Database session
        candidate_id: ID of the candidate
        tag_name: Name of the tag
        persona: Persona who added the tag
        user_id: ID of the user who added the tag
    """
    await create_activity(
        session=session,
        candidate_id=candidate_id,
        subject="Tag added",
        body=f"Added tag '{tag_name}'",
        user_id=user_id,
        persona=persona,
    )


async def log_feedback_submitted(
    session: AsyncSession,
    candidate_id: int,
    application_id: int,
    interview_name: str,
    rating: str,
    persona: str | None = None,
    user_id: int | None = None,
) -> None:
    """Log activity when feedback/scorecard is submitted.

    Args:
        session: Database session
        candidate_id: ID of the candidate
        application_id: ID of the application
        interview_name: Name of the interview
        rating: Overall rating
        persona: Persona who submitted feedback
        user_id: ID of the user who submitted feedback
    """
    await create_activity(
        session=session,
        candidate_id=candidate_id,
        subject="Feedback submitted",
        body=f"{persona or 'Interviewer'} submitted {interview_name} feedback (rating: {rating})",
        user_id=user_id,
        persona=persona,
        application_id=application_id,
    )
