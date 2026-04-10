"""Lookup list tools for the Greenhouse MCP server.

Simple list endpoints for populating dropdown selections in the UI.
"""

from auth.permissions import Permission as Perm
from fastmcp import FastMCP
from mcp_auth import require_scopes
from schemas.lookups import (
    ListDepartmentsInput,
    ListDepartmentsOutput,
    ListOfficesInput,
    ListOfficesOutput,
    ListRejectionReasonsInput,
    ListRejectionReasonsOutput,
    ListSourcesInput,
    ListSourcesOutput,
    RejectionReasonOutput,
    SourceOutput,
)
from services.clean_provider import CleanProvider


@require_scopes(Perm.USER_READ.value)
async def greenhouse_departments_list(params: ListDepartmentsInput) -> ListDepartmentsOutput:
    """List all departments for dropdown selection."""
    provider = CleanProvider()
    departments = await provider.list_departments()
    return ListDepartmentsOutput(departments=departments)


@require_scopes(Perm.USER_READ.value)
async def greenhouse_offices_list(params: ListOfficesInput) -> ListOfficesOutput:
    """List all offices for dropdown selection."""
    provider = CleanProvider()
    offices = await provider.list_offices()
    return ListOfficesOutput(offices=offices)


@require_scopes(Perm.CANDIDATE_READ.value)
async def greenhouse_sources_list(params: ListSourcesInput) -> ListSourcesOutput:
    """List all candidate sources for dropdown selection."""
    provider = CleanProvider()
    sources = await provider.list_sources()
    return ListSourcesOutput(sources=[SourceOutput(**s) for s in sources])


@require_scopes(Perm.APPLICATION_READ.value)
async def greenhouse_rejection_reasons_list(
    params: ListRejectionReasonsInput,
) -> ListRejectionReasonsOutput:
    """List all rejection reasons for dropdown selection."""
    provider = CleanProvider()
    reasons = await provider.list_rejection_reasons()
    return ListRejectionReasonsOutput(
        rejection_reasons=[RejectionReasonOutput(**r) for r in reasons]
    )


def register_lookup_tools(mcp: FastMCP) -> None:
    """Register the lookup list tools with the MCP server."""
    mcp.tool()(greenhouse_departments_list)
    mcp.tool()(greenhouse_offices_list)
    mcp.tool()(greenhouse_sources_list)
    mcp.tool()(greenhouse_rejection_reasons_list)
