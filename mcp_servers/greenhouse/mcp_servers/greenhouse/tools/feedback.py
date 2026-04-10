"""Feedback tool wiring for the Greenhouse MCP server."""

from auth.permissions import Permission as Perm
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError, ToolError
from mcp_auth import require_scopes
from schemas import ListFeedbackInput, ListFeedbackOutput, ScorecardOutput, SubmitFeedbackInput
from services.clean_provider import (
    ApplicationNotFoundError,
    CleanProvider,
    InvalidInterviewStepError,
    UserNotFoundError,
)


@require_scopes(Perm.FEEDBACK_READ.value)
async def greenhouse_feedback_list(params: ListFeedbackInput) -> ListFeedbackOutput:
    """Return scorecards submitted for an application."""

    provider = CleanProvider()
    try:
        scorecards = await provider.list_feedback(
            application_id=params.application_id,
            page=params.page,
            per_page=params.per_page,
        )
        return ListFeedbackOutput(scorecards=scorecards)
    except ApplicationNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc


@require_scopes(Perm.FEEDBACK_SUBMIT.value)
async def greenhouse_feedback_submit(params: SubmitFeedbackInput) -> ScorecardOutput:
    """Submit structured interview feedback (scorecard) for an application."""
    provider = CleanProvider()

    # Convert Pydantic models to dicts for the provider
    attributes = None
    if params.attributes:
        attributes = [
            {
                "name": attr.name,
                "type": attr.type,
                "rating": attr.rating,
                "note": attr.note,
            }
            for attr in params.attributes
        ]

    questions = None
    if params.questions:
        questions = [
            {
                "id": q.id,
                "question": q.question,
                "answer": q.answer,
            }
            for q in params.questions
        ]

    try:
        return await provider.submit_feedback(
            application_id=params.application_id,
            interviewer_id=params.interviewer_id,
            overall_recommendation=params.overall_recommendation,
            interview_step_id=params.interview_step_id,
            interviewed_at=params.interviewed_at,
            attributes=attributes,
            questions=questions,
        )
    except ApplicationNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    except UserNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    except InvalidInterviewStepError as exc:
        raise ToolError(str(exc)) from exc


def register_feedback_tools(mcp: FastMCP) -> None:
    """Register the feedback tools with the MCP server."""
    mcp.tool()(greenhouse_feedback_list)
    mcp.tool()(greenhouse_feedback_submit)
