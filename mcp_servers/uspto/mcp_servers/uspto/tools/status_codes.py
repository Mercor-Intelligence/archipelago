"""Status codes tool for the USPTO MCP server."""

from __future__ import annotations

from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import delete, select

from mcp_servers.uspto.api.factory import get_uspto_client
from mcp_servers.uspto.auth.rate_limiter import rate_limiter
from mcp_servers.uspto.cache.status_codes_cache import status_codes_cache_cutoff
from mcp_servers.uspto.config import is_online_mode
from mcp_servers.uspto.db import get_db
from mcp_servers.uspto.db.models import StatusCode as StatusCodeRow
from mcp_servers.uspto.models import (
    StatusCode,
    StatusCodesMetadata,
    StatusCodesResponse,
)
from mcp_servers.uspto.utils.errors import (
    RateLimitError,
    USPTOError,
    handle_errors,
)


@handle_errors
async def uspto_status_codes_list() -> StatusCodesResponse:
    """Retrieve and cache the USPTO status codes reference table.

    Returns the complete mapping of raw status codes to human-readable descriptions.
    This data is required for status normalization (uspto_status_normalize) and is
    automatically cached per session.

    CACHING: Results are cached for the session duration. First call fetches from USPTO,
    subsequent calls return cached data (metadata.cache_hit=true).

    WORKFLOW: Call this BEFORE using uspto_status_normalize or auto_normalize_status
    in snapshot creation to ensure the reference table is available.

    VERSIONING: The metadata.version field tracks which reference table version is in use.

    COMMON ERRORS:
    - OFFLINE_MODE_ACTIVE: No cached data and offline mode prevents fetch
    - DATA_FILE_NOT_FOUND: Offline data file missing
    - RATE_LIMIT_EXCEEDED: Too many requests (status_codes: 10/min)
    """

    # 1. Check session cache first (before rate limit to avoid wasting tokens)
    async with get_db() as session:
        cutoff = status_codes_cache_cutoff()
        result = await session.execute(
            select(StatusCodeRow).where(StatusCodeRow.retrieved_at >= cutoff)
        )
        cached_codes = list(result.scalars().all())

        if cached_codes:
            logger.info("Status codes cache hit (session-scoped)")
            await session.execute(
                delete(StatusCodeRow).where(StatusCodeRow.retrieved_at < cutoff),
            )
            return StatusCodesResponse(
                status_codes=[
                    StatusCode(
                        status_code=code.status_code,
                        status_description_text=code.status_description_text,
                    )
                    for code in cached_codes
                ],
                metadata=StatusCodesMetadata(
                    retrieved_at=cached_codes[0].retrieved_at,
                    version=cached_codes[0].version or "unknown",
                    total_codes=len(cached_codes),
                    cache_hit=True,
                ),
            )

    # 2. Check rate limit only when we need to call upstream API
    rate_limit = rate_limiter.check_rate_limit("status_codes")
    if not rate_limit.allowed:
        raise RateLimitError(
            limit=rate_limit.limit,
            retry_after=rate_limit.retry_after,
            reset_at=rate_limit.reset_at,
        )

    # 3. Fetch from USPTO API or offline data (factory handles API key internally)
    logger.info("Fetching status codes", online_mode=is_online_mode())

    client = get_uspto_client()

    try:
        upstream_response = await client.get_status_codes()
    except Exception as e:
        logger.error(f"USPTO API error: {e}")
        raise
    finally:
        await client.aclose()

    # Check for error response
    if "error" in upstream_response:
        error_info = upstream_response["error"]
        error_code = error_info.get("code", "UPSTREAM_ERROR")

        # Map error codes to appropriate HTTP status codes
        status_code_map = {
            "OFFLINE_MODE_ACTIVE": 503,
            "DATA_FILE_NOT_FOUND": 503,
            "INVALID_JSON": 500,
            "DATABASE_ERROR": 500,
        }

        raise USPTOError(
            code=error_code,
            message=error_info.get("message", "USPTO API error"),
            details=error_info.get("details", {}),
            status_code=status_code_map.get(error_code, 502),
        )

    # 5. Response already normalized by the API client
    transformed = upstream_response

    # 6. Store in session cache
    retrieved_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    # Use `or` to treat empty string same as missing (consistent with cache hit path)
    version = upstream_response.get("version") or "unknown"

    # Deduplicate by status_code (primary key) to avoid IntegrityError
    # and ensure consistent response between cache hit and miss
    unique_codes = list({code["statusCode"]: code for code in transformed["statusCodes"]}.values())

    async with get_db() as session:
        # Clear existing and bulk insert status codes
        await session.execute(StatusCodeRow.__table__.delete())
        session.add_all(
            [
                StatusCodeRow(
                    status_code=code["statusCode"],
                    status_description_text=code["statusDescriptionText"],
                    retrieved_at=retrieved_at,
                    version=version,
                )
                for code in unique_codes
            ]
        )

    logger.info(
        "Status codes fetched from USPTO API",
        total_codes=len(unique_codes),
    )

    # 7. Return response (using unique_codes for consistency with cache)
    return StatusCodesResponse(
        status_codes=[
            StatusCode(
                status_code=code["statusCode"],
                status_description_text=code["statusDescriptionText"],
            )
            for code in unique_codes
        ],
        metadata=StatusCodesMetadata(
            retrieved_at=retrieved_at,
            version=version,
            total_codes=len(unique_codes),
            cache_hit=False,
        ),
    )
