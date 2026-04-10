"""Saved queries tools for the USPTO MCP server."""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from loguru import logger
from pydantic import Field
from sqlalchemy.exc import IntegrityError

from mcp_servers.uspto.api import get_uspto_client
from mcp_servers.uspto.auth.rate_limiter import rate_limiter
from mcp_servers.uspto.cache.search_cache import (
    cache_search_results,
    get_cached_search,
)
from mcp_servers.uspto.db import get_db
from mcp_servers.uspto.models import (
    ApplicantInfo,
    ApplicationSearchResult,
    GetQueryRequest,
    PaginationMeta,
    RunQueryRequest,
    SavedQueryResponse,
    SaveQueryRequest,
    SearchMetadata,
    SearchResultsResponse,
)
from mcp_servers.uspto.repositories.queries import QueriesRepository
from mcp_servers.uspto.repositories.workspace import WorkspaceRepository
from mcp_servers.uspto.utils.audit import log_audit_event
from mcp_servers.uspto.utils.errors import (
    NotFoundError,
    QueryConflictError,
    RateLimitError,
    USPTOError,
    handle_errors,
)


def _ensure_utc_timestamp(timestamp: str | None) -> str | None:
    """Convert timestamp to ISO 8601 format (T separator, Z suffix)."""
    if timestamp is None:
        return None
    # Replace space with T for SQLite datetime format
    result = timestamp.replace(" ", "T")
    # Replace +00:00 offset with Z (equivalent)
    result = result.replace("+00:00", "Z")
    # Only append Z if no timezone indicator present
    if not result.endswith("Z") and "+" not in result and "-" not in result[-6:]:
        result = f"{result}Z"
    return result


def _current_utc_timestamp() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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
        results.append(
            ApplicationSearchResult(
                application_number_text=result.get("applicationNumberText"),
                invention_title=result.get("inventionTitle"),
                application_type=result.get("applicationType"),
                filing_date=result.get("filingDate"),
                publication_date=result.get("publicationDate"),
                publication_number=result.get("publicationNumber"),
                application_status_code=result.get("applicationStatusCode"),
                application_status_description_text=result.get("statusDescriptionText")
                or result.get("applicationStatusDescriptionText"),
                patent_number=result.get("patentNumber"),
                patent_issue_date=result.get("patentIssueDate"),
                first_named_applicant=first_named_applicant,
                assignee_entity_name=result.get("assigneeEntityName"),
            )
        )
    return results


@handle_errors
async def uspto_queries_save(
    workspace_id: Annotated[
        str,
        Field(
            pattern=r"^ws_[a-f0-9]{12}$",
            description="Workspace that will own the saved query.",
        ),
    ],
    name: Annotated[
        str,
        Field(
            min_length=1,
            max_length=200,
            description="Label for the saved query (1-200 characters).",
        ),
    ],
    query: Annotated[
        str,
        Field(
            description="USPTO query string saved for repeat execution.",
        ),
    ],
    filters: Annotated[
        dict[str, Any] | None,
        Field(
            description="Optional USPTO filters attached to the saved query.",
        ),
    ] = None,
    pinned_results: Annotated[
        list[str] | None,
        Field(
            description="Optional pinned application numbers for quick access.",
        ),
    ] = None,
    notes: Annotated[
        str | None,
        Field(
            max_length=2000,
            description="Optional workspace notes for the saved query (max 2000 chars).",
        ),
    ] = None,
) -> SavedQueryResponse:
    """Save a search query to a workspace for repeatable execution.

    Persists the query text, filters, pinned results, and notes. Returns a query_id
    (format: 'qry_' + 8 hex chars) for later retrieval and execution.

    REQUIRES: Valid workspace_id from uspto_workspaces_create.

    UNIQUENESS: Query names must be unique within a workspace.

    COMMON ERRORS:
    - NOT_FOUND: workspace_id does not exist
    - QUERY_CONFLICT: Query name already exists in this workspace
    - RATE_LIMIT_EXCEEDED: Too many requests (retrieval: 100/min)
    """
    request = SaveQueryRequest(
        workspace_id=workspace_id,
        name=name,
        query=query,
        filters=filters,
        pinned_results=pinned_results,
        notes=notes,
    )
    # 1. Check session-scoped rate limit
    rate_limit = rate_limiter.check_rate_limit("retrieval")
    if not rate_limit.allowed:
        raise RateLimitError(
            limit=rate_limit.limit,
            retry_after=rate_limit.retry_after,
            reset_at=rate_limit.reset_at,
        )

    async with get_db() as session:
        workspace_repo = WorkspaceRepository(session)
        queries_repo = QueriesRepository(session)

        # 3. Verify workspace exists in session
        workspace = await workspace_repo.get_workspace(request.workspace_id)
        if not workspace:
            raise NotFoundError("workspace", request.workspace_id)

        # 4. Check query name uniqueness within workspace
        existing = await queries_repo.get_query_by_name(request.workspace_id, request.name)
        if existing:
            raise QueryConflictError(
                query_name=request.name,
                workspace_id=request.workspace_id,
                existing_query_id=existing.id,
            )

        # 5. Create saved query (catch race condition on unique constraint)
        query_id = f"qry_{uuid.uuid4().hex[:8]}"
        try:
            saved_query = await queries_repo.create_saved_query(
                id=query_id,
                workspace_id=request.workspace_id,
                name=request.name,
                query_text=request.query,
                filters=json.dumps(request.filters) if request.filters else None,
                pinned_results=(
                    json.dumps(request.pinned_results) if request.pinned_results else None
                ),
                notes=request.notes,
            )
        except IntegrityError as e:
            # Race condition: only catch the specific workspace_name constraint
            # Note: Cannot query session after failed flush (invalid state)
            # Check both constraint name and SQLite's "table.column" format
            err_msg = str(e).lower()
            is_name_conflict = "uq_saved_query_workspace_name" in err_msg or (
                "saved_queries" in err_msg and "name" in err_msg
            )
            if is_name_conflict:
                raise QueryConflictError(
                    query_name=request.name,
                    workspace_id=request.workspace_id,
                ) from e
            raise

        # 6. Log audit event
        await log_audit_event(
            session,
            action="query_saved",
            resource_type="query",
            resource_id=query_id,
            workspace_id=request.workspace_id,
            details={"name": request.name, "query": request.query},
        )

        # 7. Return response
        logger.info(
            f"Saved query: {saved_query.name}",
            query_id=saved_query.id,
            workspace_id=saved_query.workspace_id,
        )

        return SavedQueryResponse(
            query_id=saved_query.id,
            workspace_id=saved_query.workspace_id,
            name=saved_query.name,
            query=saved_query.query_text,
            filters=json.loads(saved_query.filters) if saved_query.filters else None,
            pinned_results=(
                json.loads(saved_query.pinned_results) if saved_query.pinned_results else []
            ),
            notes=saved_query.notes,
            created_at=_ensure_utc_timestamp(saved_query.created_at) or _current_utc_timestamp(),
            last_run_at=None,
            run_count=0,
        )


@handle_errors
async def uspto_queries_get(
    query_id: Annotated[
        str,
        Field(
            pattern=r"^qry_[a-f0-9]{8}$",
            description="Identifier for the saved query (qry_{uuid}).",
        ),
    ],
) -> SavedQueryResponse:
    """Retrieve a saved query definition and execution metadata.

    Returns the query text, filters, pinned results, notes, and execution statistics
    (created_at, last_run_at, run_count).

    PREREQUISITE: Query must exist - use query_id from uspto_queries_save.

    COMMON ERRORS:
    - NOT_FOUND: query_id does not exist
    - RATE_LIMIT_EXCEEDED: Too many requests (retrieval: 100/min)
    """
    request = GetQueryRequest(query_id=query_id)
    # 1. Check session-scoped rate limit
    rate_limit = rate_limiter.check_rate_limit("retrieval")
    if not rate_limit.allowed:
        raise RateLimitError(
            limit=rate_limit.limit,
            retry_after=rate_limit.retry_after,
            reset_at=rate_limit.reset_at,
        )

    async with get_db() as session:
        workspace_repo = WorkspaceRepository(session)
        queries_repo = QueriesRepository(session)

        # 3. Fetch query from session database
        query = await queries_repo.get_saved_query(request.query_id)
        if not query:
            raise NotFoundError("query", request.query_id)

        # 4. Verify workspace exists in session
        workspace = await workspace_repo.get_workspace(query.workspace_id)
        if not workspace:
            raise NotFoundError("workspace", query.workspace_id)

        # 5. Return response
        return SavedQueryResponse(
            query_id=query.id,
            workspace_id=query.workspace_id,
            name=query.name,
            query=query.query_text,
            filters=json.loads(query.filters) if query.filters else None,
            pinned_results=json.loads(query.pinned_results) if query.pinned_results else [],
            notes=query.notes,
            created_at=_ensure_utc_timestamp(query.created_at) or _current_utc_timestamp(),
            last_run_at=_ensure_utc_timestamp(query.last_run_at),
            run_count=query.run_count or 0,
        )


@handle_errors
async def uspto_queries_run(
    query_id: Annotated[
        str,
        Field(
            pattern=r"^qry_[a-f0-9]{8}$",
            description="Identifier of the saved query to execute.",
        ),
    ],
    start: Annotated[
        int,
        Field(
            ge=0,
            description="Zero-based offset into the query results.",
        ),
    ] = 0,
    rows: Annotated[
        int,
        Field(
            ge=1,
            le=100,
            description="Page size for re-running the saved query (1-100).",
        ),
    ] = 25,
) -> SearchResultsResponse:
    """Execute a saved query and return search results with provenance.

    Runs the stored query with its saved filters and returns results in the same
    format as uspto_applications_search. Automatically updates execution metadata
    (last_run_at, run_count) and caches results.

    PREREQUISITE: Query must exist - use query_id from uspto_queries_save.

    PAGINATION: Override start/rows for pagination through results.

    CACHING: Results are cached per session. Cache hits return immediately without
    calling the USPTO API.

    COMMON ERRORS:
    - NOT_FOUND: query_id does not exist
    - OFFLINE_MODE_ACTIVE: Server running in offline mode (no live data)
    - RATE_LIMIT_EXCEEDED: Too many requests (search: 50/min)
    """
    request = RunQueryRequest(query_id=query_id, start=start, rows=rows)
    # 1. Check session-scoped rate limit (search category)
    rate_limit = rate_limiter.check_rate_limit("search")
    if not rate_limit.allowed:
        raise RateLimitError(
            limit=rate_limit.limit,
            retry_after=rate_limit.retry_after,
            reset_at=rate_limit.reset_at,
        )

    async with get_db() as session:
        workspace_repo = WorkspaceRepository(session)
        queries_repo = QueriesRepository(session)

        # 2. Fetch query from session database
        query = await queries_repo.get_saved_query(request.query_id)
        if not query:
            raise NotFoundError("query", request.query_id)

        # 3. Verify workspace exists in session
        workspace = await workspace_repo.get_workspace(query.workspace_id)
        if not workspace:
            raise NotFoundError("workspace", query.workspace_id)

        # 4. Parse stored filters
        filters = json.loads(query.filters) if query.filters else None

        # 5. Check session-scoped cache before calling upstream
        cached_result = await get_cached_search(
            query.query_text,
            filters,
            start=request.start,
            rows=request.rows,
            sort=None,
        )
        if cached_result:
            raw_results = cached_result.get("results") or []
            total_results = cached_result.get("totalCount")
            total_results = total_results if total_results is not None else len(raw_results)
            results = _build_search_results(raw_results)

            await queries_repo.update_query_execution(
                query_id=query.id,
                last_run_at=datetime.now(UTC),
            )

            await log_audit_event(
                session,
                action="query_executed",
                resource_type="query",
                resource_id=query.id,
                workspace_id=query.workspace_id,
                details={"name": query.name, "totalResults": total_results},
            )

            response = SearchResultsResponse(
                query_id=query.id,
                results=results,
                pagination=PaginationMeta(
                    start=request.start,
                    rows=request.rows,
                    total_results=total_results,
                ),
                metadata=SearchMetadata(
                    query_text=query.query_text,
                    retrieved_at=_ensure_utc_timestamp(cached_result.get("cachedAt"))
                    or _current_utc_timestamp(),
                    execution_time_ms=None,
                    result_count=len(results),
                    cursor=None,
                    dataset_coverage=(
                        "Published applications and issued patents with filing dates 2001-present"
                    ),
                    filters_applied=filters,
                ),
            )

            logger.info(
                "Saved query executed (cache hit)",
                query_id=query.id,
                query_name=query.name,
                total_results=total_results,
                returned_results=len(results),
            )

            return response

        # 6. Execute search (factory handles API key internally)
        client = get_uspto_client()

        try:
            logger.info(
                "Executing saved query",
                query_id=query.id,
                query_name=query.name,
                query_text=query.query_text,
            )

            start_time = time.time()

            upstream_response = await client.search_applications(
                query=query.query_text,
                filters=filters,
                start=request.start,
                rows=request.rows,
            )

            execution_time_ms = int((time.time() - start_time) * 1000)

            # 8. Check for upstream errors
            if "error" in upstream_response:
                error_info = upstream_response["error"]
                error_code = error_info.get("code", "UPSTREAM_ERROR")

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

                raise USPTOError(
                    code=error_code,
                    message=error_info.get("message", "USPTO API error"),
                    details=error_info.get("details", {}),
                    status_code=503,
                )

            # 9. Transform results
            raw_results = upstream_response.get("results") or []
            total = upstream_response.get("total")
            total_results = total if total is not None else len(raw_results)

            results = _build_search_results(raw_results)

            # 10. Cache results for this session
            await cache_search_results(
                query.query_text,
                filters,
                start=request.start,
                rows=request.rows,
                sort=None,
                results=raw_results,
                total_count=total_results,
            )

            # 11. Update execution metadata (atomic increment in repository)
            await queries_repo.update_query_execution(
                query_id=query.id,
                last_run_at=datetime.now(UTC),
            )

            # 12. Log audit event
            await log_audit_event(
                session,
                action="query_executed",
                resource_type="query",
                resource_id=query.id,
                workspace_id=query.workspace_id,
                details={"name": query.name, "totalResults": total_results},
            )

            # 13. Build response with provenance
            response = SearchResultsResponse(
                query_id=query.id,
                results=results,
                pagination=PaginationMeta(
                    start=request.start,
                    rows=request.rows,
                    total_results=total_results,
                ),
                metadata=SearchMetadata(
                    query_text=query.query_text,
                    retrieved_at=_current_utc_timestamp(),
                    execution_time_ms=execution_time_ms,
                    result_count=len(results),
                    cursor=None,
                    dataset_coverage=(
                        "Published applications and issued patents with filing dates 2001-present"
                    ),
                    filters_applied=filters,
                ),
            )

            logger.info(
                "Saved query executed",
                query_id=query.id,
                query_name=query.name,
                total_results=total_results,
                returned_results=len(results),
                execution_time_ms=execution_time_ms,
            )

            return response

        finally:
            await client.aclose()


__all__ = ["uspto_queries_save", "uspto_queries_get", "uspto_queries_run"]
