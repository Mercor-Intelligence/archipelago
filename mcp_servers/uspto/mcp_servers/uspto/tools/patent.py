"""Patent retrieval tool for the USPTO MCP server.

Provides access to full patent text (abstract, description, claims) from the offline database.
"""

from __future__ import annotations

from typing import Annotated

from loguru import logger
from pydantic import Field

from mcp_servers.uspto.api import get_uspto_client
from mcp_servers.uspto.auth.rate_limiter import rate_limiter
from mcp_servers.uspto.models import GetPatentRequest, GetPatentResponse
from mcp_servers.uspto.utils.dates import coerce_iso_date
from mcp_servers.uspto.utils.errors import (
    NotFoundError,
    RateLimitError,
    USPTOError,
    handle_errors,
)


@handle_errors
async def uspto_patent_get(
    application_number: Annotated[
        str,
        Field(
            pattern=r"^(\d{2}/\d{3},\d{3}|\d{6,})$",
            description="Application number in formatted (e.g., 16/123,456) or digits-only form.",
        ),
    ],
) -> GetPatentResponse:
    """Retrieve complete patent details including full text content.

    Returns comprehensive patent data:
    - Bibliographic: title, application type, dates, kind code
    - Full text: abstract, description, claims (THE KEY DATA for patent analysis)
    - Parties: inventors, assignees, applicants, examiner
    - Classifications: CPC, IPC, USPC
    - Citations: patent references, non-patent literature
    - Related: foreign priority, parent/child continuity

    USE CASE: Get full patent text for analysis, summarization, or claim interpretation.
    This is the primary tool for accessing patent content beyond basic search results.

    COMMON ERRORS:
    - NOT_FOUND: Patent does not exist in the database
    - OFFLINE_MODE_ACTIVE: Patent not found in local offline database
    - RATE_LIMIT_EXCEEDED: Too many requests (read: 100/min)
    """
    request = GetPatentRequest(application_number=application_number)
    rate_limit = rate_limiter.check_rate_limit("read")
    if not rate_limit.allowed:
        raise RateLimitError(
            limit=rate_limit.limit,
            retry_after=rate_limit.retry_after,
            reset_at=rate_limit.reset_at,
        )

    client = get_uspto_client()

    try:
        logger.info(
            "Retrieving patent details",
            application_number=request.application_number,
        )

        result = await client.get_application(request.application_number)
    finally:
        await client.aclose()

    if "error" in result:
        error_info = result["error"]
        error_code = error_info.get("code", "UPSTREAM_ERROR")

        if error_code == "APPLICATION_NOT_FOUND":
            raise NotFoundError("patent", request.application_number)

        if error_code == "OFFLINE_MODE_ACTIVE":
            raise USPTOError(
                code="OFFLINE_MODE_ACTIVE",
                message="USPTO API is running in offline mode. Patent not found in local database.",
                details={
                    "suggestion": "Ensure the patent has been ingested into the offline database",
                    "applicationNumber": request.application_number,
                },
                status_code=503,
            )

        raise USPTOError(
            code=error_code,
            message=error_info.get("message", "Failed to retrieve patent"),
            details=error_info.get("details", {}),
            status_code=503,
        )

    # Build response from application data
    return GetPatentResponse(
        application_number=result.get("applicationNumberText") or request.application_number,
        patent_number=result.get("patentNumber"),
        publication_number=result.get("publicationNumber"),
        # Core bibliographic data
        title=result.get("inventionTitle"),
        application_type=result.get("applicationType"),
        document_type=result.get("documentType"),
        kind_code=result.get("kindCode"),
        country=result.get("country", "US"),
        # Dates
        filing_date=coerce_iso_date(result.get("filingDate")),
        publication_date=coerce_iso_date(result.get("publicationDate")),
        issue_date=coerce_iso_date(result.get("patentIssueDate")),
        # Full text content - THE KEY DATA
        abstract=result.get("abstract"),
        description=result.get("description"),
        claims=result.get("claims"),
        # Parties
        inventors=result.get("inventors"),
        assignees=result.get("assignees"),
        applicants=result.get("applicants"),
        first_inventor_name=result.get("firstInventorName"),
        assignee_entity_name=result.get("assigneeEntityName"),
        # Examiner info
        examiner_name=result.get("examinerName"),
        group_art_unit=result.get("groupArtUnitNumber"),
        primary_examiner=result.get("primaryExaminer"),
        assistant_examiner=result.get("assistantExaminer"),
        # Classifications
        cpc_classifications=result.get("cpcClassifications"),
        ipc_codes=result.get("ipcCodes"),
        uspc_class=result.get("uspcClass"),
        uspc_subclass=result.get("uspcSubclass"),
        # Citations
        patent_citations=result.get("patentCitations"),
        npl_citations=result.get("nplCitations"),
        # Related applications
        foreign_priority_claims=result.get("foreignPriorityClaims"),
        parent_continuity=result.get("parentContinuity"),
        child_continuity=result.get("childContinuity"),
        # Grant-specific
        number_of_claims=result.get("numberOfClaims"),
        number_of_figures=result.get("numberOfFigures"),
        # Status (when available)
        application_status_code=result.get("applicationStatusCode"),
        application_status_description=result.get("applicationStatusDescriptionText"),
    )


__all__ = ["uspto_patent_get"]
