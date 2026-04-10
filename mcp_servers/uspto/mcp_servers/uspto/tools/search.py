"""Search tools for the USPTO MCP server."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from loguru import logger
from pydantic import Field

from mcp_servers.uspto.api import get_uspto_client
from mcp_servers.uspto.auth.rate_limiter import rate_limiter
from mcp_servers.uspto.cache.search_cache import (
    cache_search_results,
    get_cached_search,
)
from mcp_servers.uspto.models import (
    ApplicantInfo,
    ApplicationSearchResult,
    PaginationMeta,
    SearchApplicationsRequest,
    SearchMetadata,
    SearchResultsResponse,
)
from mcp_servers.uspto.utils.dates import coerce_iso_date
from mcp_servers.uspto.utils.errors import (
    RateLimitError,
    USPTOError,
    handle_errors,
)


def _ensure_utc_timestamp() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _normalize_cached_timestamp(timestamp: str | None) -> str:
    """Normalize cached timestamps into ISO 8601 format."""
    if not timestamp:
        return _ensure_utc_timestamp()
    result = timestamp.replace(" ", "T").replace("+00:00", "Z")
    if not result.endswith("Z") and "+" not in result and "-" not in result[-6:]:
        result = f"{result}Z"
    return result


def _parse_applicant_info(data: dict | None) -> ApplicantInfo | None:
    """Parse applicant info from USPTO response data."""
    if not data:
        return None
    return ApplicantInfo(
        name=data.get("applicantName") or data.get("name"),
        role=data.get("role"),
        country=data.get("country"),
        organization=data.get("organization"),
    )


def _build_search_results(raw_results: list[dict]) -> list[ApplicationSearchResult]:
    results = []
    for result in raw_results:
        first_named_applicant = _parse_applicant_info(result.get("firstNamedApplicant"))

        # Extract priority claims (both online and offline use foreignPriorityClaims)
        priority_claims = result.get("foreignPriorityClaims")

        # Extract continuity data (parentContinuity and childContinuity)
        parent_continuity = result.get("parentContinuity")
        child_continuity = result.get("childContinuity")

        results.append(
            ApplicationSearchResult(
                application_number_text=result.get("applicationNumberText"),
                invention_title=result.get("inventionTitle"),
                application_type=result.get("applicationType"),
                filing_date=coerce_iso_date(result.get("filingDate")),
                publication_date=coerce_iso_date(result.get("publicationDate")),
                publication_number=result.get("publicationNumber"),
                application_status_code=result.get("applicationStatusCode"),
                application_status_description_text=result.get("statusDescriptionText")
                or result.get("applicationStatusDescriptionText"),
                patent_number=result.get("patentNumber"),
                patent_issue_date=coerce_iso_date(result.get("patentIssueDate")),
                first_named_applicant=first_named_applicant,
                assignee_entity_name=result.get("assigneeEntityName"),
                priority_claims=priority_claims,
                parent_continuity=parent_continuity,
                child_continuity=child_continuity,
                related_application=result.get("relatedApplication"),
            )
        )
    return results


@handle_errors
async def uspto_applications_search(
    query: Annotated[
        str,
        Field(
            min_length=1,
            description="USPTO query syntax executed by the search endpoint.",
        ),
    ],
    filters: Annotated[
        dict[str, Any] | None,
        Field(
            description="Optional JSON object of additional USPTO filters.",
        ),
    ] = None,
    start: Annotated[
        int,
        Field(
            ge=0,
            description="Zero-based offset into the search results.",
        ),
    ] = 0,
    rows: Annotated[
        int,
        Field(
            ge=1,
            le=100,
            description="Page size for search results (1-100).",
        ),
    ] = 25,
    sort: Annotated[
        str | None,
        Field(
            description="Optional sort clause recognized by the USPTO API.",
        ),
    ] = None,
) -> SearchResultsResponse:
    """Search published applications and issued patents using USPTO Solr query syntax.

    DATASET COVERAGE: Applications and patents with filing dates 2001-present.

    QUERY SYNTAX (Solr-based):
    - Field search: fieldName:value or fieldName:"phrase with spaces"
    - Boolean: AND, OR, NOT (UPPERCASE required)
    - Wildcards: * (multiple chars), ? (single char)
    - Ranges: fieldName:[start TO end] (dates as YYYY-MM-DD)

    SEARCHABLE FIELDS: inventionTitle, assigneeEntityName, applicationStatusCode,
    filingDate, publicationDate, patentNumber, applicationNumberText,
    firstNamedApplicant, firstInventorName, groupArtUnitNumber.

    EXAMPLES:
    - 'inventionTitle:"machine learning" AND assigneeEntityName:Google'
    - 'filingDate:[2020-01-01 TO 2024-12-31]'
    - 'patentNumber:US10123456*'

    PAGINATION: Use 'start' and 'rows' parameters. Default rows=25, max rows=100.

    COMMON ERRORS:
    - RATE_LIMIT_EXCEEDED: Too many requests (search: 50/min)
    - OFFLINE_MODE_ACTIVE: Server running in offline mode (no live data)
    """
    request = SearchApplicationsRequest(
        query=query, filters=filters, start=start, rows=rows, sort=sort
    )
    # 1. Check session-scoped rate limit (search category: 50 req/min)
    rate_limit = rate_limiter.check_rate_limit("search")
    if not rate_limit.allowed:
        raise RateLimitError(
            limit=rate_limit.limit,
            retry_after=rate_limit.retry_after,
            reset_at=rate_limit.reset_at,
        )

    # 2. Check session-scoped cache before calling upstream
    cached_result = await get_cached_search(
        request.query,
        request.filters,
        start=request.start,
        rows=request.rows,
        sort=request.sort,
    )
    if cached_result:
        raw_results = cached_result.get("results") or []
        total_results = cached_result.get("totalCount")
        total_results = total_results if total_results is not None else len(raw_results)
        results = _build_search_results(raw_results)

        query_id = f"qry_{uuid.uuid4().hex[:8]}"
        return SearchResultsResponse(
            query_id=query_id,
            results=results,
            pagination=PaginationMeta(
                start=request.start,
                rows=request.rows,
                total_results=total_results,
            ),
            metadata=SearchMetadata(
                query_text=request.query,
                retrieved_at=_normalize_cached_timestamp(cached_result.get("cachedAt")),
                execution_time_ms=None,
                result_count=len(results),
                cursor=None,
                dataset_coverage=(
                    "Published applications and issued patents with filing dates 2001-present"
                ),
                filters_applied=request.filters,
            ),
        )

    # 3. Create USPTO API client (factory handles API key internally)
    client = get_uspto_client()

    try:
        # 5. Execute search
        logger.info(
            "Executing USPTO search",
            query=request.query,
            start=request.start,
            rows=request.rows,
        )

        start_time = time.time()

        upstream_response = await client.search_applications(
            query=request.query,
            filters=request.filters,
            start=request.start,
            rows=request.rows,
            sort=request.sort,
        )

        execution_time_ms = int((time.time() - start_time) * 1000)

        # 6. Check for upstream errors
        if "error" in upstream_response:
            error_info = upstream_response["error"]
            error_code = error_info.get("code", "UPSTREAM_ERROR")

            # Handle offline mode specifically
            if error_code == "OFFLINE_MODE_ACTIVE":
                raise USPTOError(
                    code="OFFLINE_MODE_ACTIVE",
                    message="USPTO API is running in offline mode. No live data available.",
                    details={
                        "suggestion": (
                            "Restart server with --online flag to enable live USPTO API calls"
                        ),
                        "offlineMode": True,
                    },
                    status_code=503,
                )

            # Handle other upstream errors
            raise USPTOError(
                code=error_code,
                message=error_info.get("message", "USPTO API error"),
                details=error_info.get("details", {}),
                status_code=503,
            )

        # 7. Transform results to Pydantic models
        raw_results = upstream_response.get("results") or []
        total = upstream_response.get("total")
        total_results = total if total is not None else len(raw_results)

        results = _build_search_results(raw_results)

        # 8. Cache results for this session
        await cache_search_results(
            request.query,
            request.filters,
            start=request.start,
            rows=request.rows,
            sort=request.sort,
            results=raw_results,
            total_count=total_results,
        )

        # 9. Generate temporary query ID for ad-hoc search
        query_id = f"qry_{uuid.uuid4().hex[:8]}"

        # 10. Build response
        response = SearchResultsResponse(
            query_id=query_id,
            results=results,
            pagination=PaginationMeta(
                start=request.start,
                rows=request.rows,
                total_results=total_results,
            ),
            metadata=SearchMetadata(
                query_text=request.query,
                retrieved_at=_ensure_utc_timestamp(),
                execution_time_ms=execution_time_ms,
                result_count=len(results),
                cursor=None,  # Offset-based pagination, no cursor
                dataset_coverage=(
                    "Published applications and issued patents with filing dates 2001-present"
                ),
                filters_applied=request.filters,
            ),
        )

        logger.info(
            "Search completed",
            total_results=total_results,
            returned_results=len(results),
            execution_time_ms=execution_time_ms,
        )

        return response

    finally:
        await client.aclose()


__all__ = ["uspto_applications_search"]
