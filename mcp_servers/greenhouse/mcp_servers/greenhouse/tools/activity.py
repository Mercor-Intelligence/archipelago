"""Activity tool wiring for the Greenhouse MCP server."""

from __future__ import annotations

from auth.permissions import Permission as Perm
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError
from mcp_auth import require_scopes
from schemas import ActivityFeedOutput, GetActivityFeedInput
from services.clean_provider import CandidateNotFoundError, CleanProvider


@require_scopes(Perm.ACTIVITY_READ.value)
async def greenhouse_activity_get(params: GetActivityFeedInput) -> ActivityFeedOutput:
    """Retrieve the activity feed for a candidate including notes, emails, and system events."""
    provider = CleanProvider()
    try:
        result = await provider.get_activity_feed(params.candidate_id)
        return ActivityFeedOutput.model_validate(result)
    except CandidateNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc


def register_activity_tools(mcp: FastMCP) -> None:
    """Register the activity feed tool with the MCP server."""
    mcp.tool()(greenhouse_activity_get)
