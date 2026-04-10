"""Foreign priority retrieval tools for the USPTO MCP server."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Annotated

from loguru import logger
from pydantic import Field

from mcp_servers.uspto.api.factory import get_uspto_client
from mcp_servers.uspto.auth.rate_limiter import rate_limiter
from mcp_servers.uspto.db import get_db
from mcp_servers.uspto.models import (
    ForeignPriorityMetadata,
    ForeignPriorityResponse,
    GetForeignPriorityRequest,
)
from mcp_servers.uspto.repositories.foreign_priority import ForeignPriorityRepository
from mcp_servers.uspto.utils.errors import (
    ForeignPriorityUnavailableError,
    InvalidRequestError,
    RateLimitError,
    USPTOError,
    handle_errors,
)


def _ensure_utc_timestamp() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@handle_errors
async def uspto_foreign_priority_get(
    application_number_text: Annotated[
        str,
        Field(
            pattern=r"^\d{2}/\d{3},\d{3}$",
            description="Formatted application number for foreign priority lookup.",
        ),
    ],
) -> ForeignPriorityResponse:
    """Retrieve foreign priority claims for a patent application.

    Returns details of any foreign applications cited as priority under the Paris Convention,
    including country code, application number, filing date, and priority status.

    DATASET COVERAGE: Foreign priority data available for applications that have filed
    priority claims. Not all applications have foreign priority.

    COMMON ERRORS:
    - FOREIGN_PRIORITY_UNAVAILABLE: Application has no foreign priority claims or
      data is outside dataset coverage
    - OFFLINE_MODE_ACTIVE: Returns empty list in offline mode
    - RATE_LIMIT_EXCEEDED: Too many requests (foreign_priority: 50/min)
    """
    request = GetForeignPriorityRequest(application_number_text=application_number_text)
    # 1. Check session-scoped rate limit (foreign priority category: 50 req/min)
    rate_limit = rate_limiter.check_rate_limit("foreign_priority")
    if not rate_limit.allowed:
        raise RateLimitError(
            limit=rate_limit.limit,
            retry_after=rate_limit.retry_after,
            reset_at=rate_limit.reset_at,
        )

    # 2. Create USPTO API client (factory handles API key internally)
    client = get_uspto_client()

    try:
        # 4. Execute foreign priority request
        logger.info(
            "Executing USPTO foreign priority get",
            application_number=request.application_number_text,
        )

        start_time = time.time()

        upstream_response = await client.get_foreign_priority(
            application_number=request.application_number_text,
        )

        execution_time_ms = int((time.time() - start_time) * 1000)

        # 5. Check for upstream errors
        if "error" in upstream_response:
            error_info = upstream_response["error"]
            error_code = error_info.get("code", "UPSTREAM_ERROR")

            # Handle offline mode specifically - return empty results
            if error_code == "OFFLINE_MODE_ACTIVE":
                logger.warning(
                    "Offline mode active - returning empty foreign priority list",
                    application_number=request.application_number_text,
                )
                # Return empty response to allow tool to work in offline mode
                # for testing/development
                return ForeignPriorityResponse(
                    application_number_text=request.application_number_text,
                    foreign_priority_claims=[],
                    metadata=ForeignPriorityMetadata(
                        retrieved_at=_ensure_utc_timestamp(),
                        total_claims=0,
                        execution_time_ms=execution_time_ms,
                        dataset_coverage=(
                            "Offline mode: No live data available. "
                            "Restart server with --online flag to enable live USPTO API calls."
                        ),
                    ),
                )

            # Handle coverage errors (404 or DATASET_COVERAGE_UNAVAILABLE)
            if error_code in ("DATASET_COVERAGE_UNAVAILABLE", "UPSTREAM_CLIENT_ERROR"):
                error_details = error_info.get("details", {})
                # statusCode is directly in error_details from API client
                upstream_status = error_details.get("statusCode")
                upstream_error = error_details.get("upstreamError", {})

                # Handle 404 as foreign priority unavailable
                if upstream_status == 404 or error_code == "DATASET_COVERAGE_UNAVAILABLE":
                    reason = error_details.get("reason") or (
                        "Application may not claim foreign priority or data is "
                        "outside dataset coverage"
                    )
                    raise ForeignPriorityUnavailableError(
                        application_number=request.application_number_text,
                        reason=reason,
                    )

                # Handle other 4xx client errors (400, 401, 403, etc.)
                # UPSTREAM_CLIENT_ERROR is always a 4xx error from the API client
                if error_code == "UPSTREAM_CLIENT_ERROR" and upstream_status:
                    # Extract message from upstream error response or use default
                    if isinstance(upstream_error, dict):
                        upstream_message = upstream_error.get("message") or error_info.get(
                            "message", "USPTO rejected the request"
                        )
                    else:
                        upstream_message = error_info.get("message", "USPTO rejected the request")

                    logger.error(
                        "USPTO API client error",
                        error_code=error_code,
                        upstream_status=upstream_status,
                        upstream_error=upstream_error,
                        application_number=request.application_number_text,
                    )
                    raise InvalidRequestError(
                        message=f"USPTO API rejected the request: {upstream_message}",
                        details={
                            "applicationNumber": request.application_number_text,
                            "upstreamStatusCode": upstream_status,
                            "upstreamError": upstream_error,
                        },
                    )

            # Handle other upstream errors
            logger.error(
                "Unhandled upstream error",
                error_code=error_code,
                error_message=error_info.get("message"),
                error_details=error_info.get("details", {}),
            )
            raise USPTOError(
                code=error_code,
                message=error_info.get("message", "USPTO API error"),
                details=error_info.get("details", {}),
                status_code=503,
            )

        # 6. Transform results to Pydantic models using repository
        # The API client already transforms foreignPriorityBag -> foreignPriorityClaims
        raw_claims = upstream_response.get("foreignPriorityClaims") or []
        raw_claims_count = len(raw_claims)

        # 7. Process claims using repository (within database session context)
        async with get_db() as session:
            repo = ForeignPriorityRepository(session)

            # Parse claims using repository
            claims = repo.parse_foreign_priority_claims(raw_claims)

            # Store data in session database (if workspace context available)
            # Note: This is optional and only stores if workspace_id is available in context
            # For now, we skip storage as the request doesn't include workspace_id
            # This can be enhanced later to support optional workspace storage
            # Example: if workspace_id:
            #     await repo.store_foreign_priority_claims(
            #         workspace_id=workspace_id,
            #         application_number_text=request.application_number_text,
            #         claims=raw_claims,
            #     )

        # Calculate total_claims from parsed claims to match actual returned count
        total_claims = len(claims)

        # 8. Build response
        response = ForeignPriorityResponse(
            application_number_text=request.application_number_text,
            foreign_priority_claims=claims,
            metadata=ForeignPriorityMetadata(
                retrieved_at=_ensure_utc_timestamp(),
                total_claims=total_claims,
                execution_time_ms=execution_time_ms,
                dataset_coverage=(
                    "Foreign priority data available for applications with priority "
                    "claims in supported datasets"
                ),
            ),
        )

        logger.info(
            "Foreign priority get completed",
            application_number=request.application_number_text,
            raw_claims_count=raw_claims_count,
            total_claims=total_claims,
            execution_time_ms=execution_time_ms,
            cached=upstream_response.get("cached", False),
        )

        return response

    finally:
        await client.aclose()


__all__ = ["uspto_foreign_priority_get"]
