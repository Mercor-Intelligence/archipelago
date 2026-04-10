"""Status normalization tool for the USPTO MCP server."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from loguru import logger
from pydantic import Field

from mcp_servers.uspto.auth.rate_limiter import rate_limiter
from mcp_servers.uspto.cache.status_codes_cache import (
    fetch_and_cache_status_codes,
    get_cached_status_codes,
)
from mcp_servers.uspto.db import get_db
from mcp_servers.uspto.models import (
    APPLICATION_NUMBER_PATTERN,
    StatusNormalizeEntry,
    StatusNormalizeMetadata,
    StatusNormalizeRequest,
    StatusNormalizeResponse,
)
from mcp_servers.uspto.repositories.snapshots import SnapshotRepository
from mcp_servers.uspto.repositories.workspace import WorkspaceRepository
from mcp_servers.uspto.utils.errors import (
    InvalidRequestError,
    NotFoundError,
    RateLimitError,
    USPTOError,
    handle_errors,
)


def _current_utc_timestamp() -> str:
    """Return current UTC timestamp in ISO 8601 format with Z suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _ensure_utc_timestamp(timestamp: str | None) -> str | None:
    """Convert timestamp to ISO 8601 format (T separator, Z suffix)."""
    if timestamp is None:
        return None
    result = timestamp.replace(" ", "T")
    result = result.replace("+00:00", "Z")
    if not result.endswith("Z") and "+" not in result and "-" not in result[-6:]:
        result = f"{result}Z"
    return result


def _validate_application_numbers(application_numbers: list[str]) -> None:
    """Validate application numbers input before normalization."""
    if not application_numbers:
        raise InvalidRequestError(
            message="applicationNumbers cannot be empty",
            details={"minItems": 1},
        )

    if len(application_numbers) > 50:
        raise InvalidRequestError(
            message="applicationNumbers cannot exceed 50 entries",
            details={"maxBatchSize": 50, "provided": len(application_numbers)},
        )

    invalid_numbers = [
        str(number)
        for number in application_numbers
        if not isinstance(number, str) or not APPLICATION_NUMBER_PATTERN.match(number)
    ]
    if invalid_numbers:
        raise InvalidRequestError(
            message="Invalid application number format",
            details={
                "invalidApplicationNumbers": invalid_numbers,
                "expectedFormat": "16/123,456",
            },
        )


async def _load_status_reference() -> tuple[dict[str, str], str]:
    """Load status code reference table from cache or upstream."""
    status_codes = await get_cached_status_codes()
    if not status_codes:
        refreshed = await fetch_and_cache_status_codes()
        status_codes = await get_cached_status_codes()
        if not status_codes and refreshed is None:
            status_codes = await get_cached_status_codes(allow_stale=True)

    if not status_codes:
        raise USPTOError(
            code="STATUS_CODES_UNAVAILABLE",
            message="Status code reference data is unavailable",
            details={"suggestion": "Call uspto_status_codes_list to refresh the cache"},
            status_code=503,
        )

    status_code_version = next(
        (code.get("version") for code in status_codes if code.get("version")),
        None,
    )
    status_code_version = status_code_version or "unknown"

    status_map: dict[str, str] = {}
    for code in status_codes:
        status_code = code.get("statusCode") or code.get("applicationStatusCode")
        description = code.get("statusDescriptionText") or code.get(
            "applicationStatusDescriptionText"
        )
        if status_code is None or description is None:
            continue
        status_map[str(status_code)] = str(description)

    if not status_map:
        raise USPTOError(
            code="STATUS_CODES_UNAVAILABLE",
            message="Status code reference data is unavailable",
            details={"suggestion": "Call uspto_status_codes_list to refresh the cache"},
            status_code=503,
        )

    return status_map, status_code_version


@handle_errors
async def uspto_status_normalize(
    workspace_id: Annotated[
        str,
        Field(
            pattern=r"^ws_[a-f0-9]{12}$",
            description="Workspace whose snapshots should be normalized.",
        ),
    ],
    application_numbers: Annotated[
        list[str],
        Field(description="Application numbers to normalize (max 50, format: '16/123,456')."),
    ],
) -> StatusNormalizeResponse:
    """Batch normalize raw status codes in existing snapshots using the USPTO reference table.

    PREREQUISITES (call these FIRST):
    1. uspto_status_codes_list - Caches the status code reference table
    2. uspto_snapshots_create - Creates snapshots for the applications to normalize

    WORKFLOW:
    1. Call uspto_status_codes_list to ensure reference data is cached
    2. Call uspto_snapshots_create for each application
    3. Call this tool with the list of application numbers to normalize

    BATCH LIMITS: Process 1-50 application numbers per request.

    RESULT: Each snapshot's status is updated with normalized description and timestamp.
    The status_code_version tracks which reference version was used.

    COMMON ERRORS:
    - NOT_FOUND: workspace_id or one or more snapshots do not exist
    - STATUS_CODES_UNAVAILABLE: Reference table not cached (call uspto_status_codes_list)
    - RATE_LIMIT_EXCEEDED: Too many requests (status_normalize: 100/min)
    """
    request = StatusNormalizeRequest(
        workspace_id=workspace_id, application_numbers=application_numbers
    )
    rate_limit = rate_limiter.check_rate_limit("status_normalize")
    if not rate_limit.allowed:
        raise RateLimitError(
            limit=rate_limit.limit,
            retry_after=rate_limit.retry_after,
            reset_at=rate_limit.reset_at,
        )

    _validate_application_numbers(request.application_numbers)

    status_map, status_code_version = await _load_status_reference()

    async with get_db() as session:
        workspace_repo = WorkspaceRepository(session)
        workspace = await workspace_repo.get_workspace(request.workspace_id)
        if not workspace:
            raise NotFoundError("workspace", request.workspace_id)

        snapshot_repo = SnapshotRepository(session)
        snapshots = await snapshot_repo.get_latest_snapshots_by_app_numbers(
            request.workspace_id,
            request.application_numbers,
        )
        snapshot_by_app = {snapshot.application_number_text: snapshot for snapshot in snapshots}
        missing = [
            application_number
            for application_number in request.application_numbers
            if application_number not in snapshot_by_app
        ]

        if missing:
            if len(missing) == 1:
                raise NotFoundError(
                    "snapshot",
                    missing[0],
                    message=(
                        f"Snapshot '{missing[0]}' not found. "
                        "Create a snapshot first using uspto_snapshots_create."
                    ),
                    details={"missingApplicationNumbers": missing},
                )
            raise NotFoundError(
                "snapshot",
                "multiple",
                message=(
                    "One or more snapshots were not found. "
                    "Create snapshots first using uspto_snapshots_create."
                ),
                details={"missingApplicationNumbers": missing},
            )

        normalized_entries: list[StatusNormalizeEntry] = []
        errors = 0

        for application_number in request.application_numbers:
            snapshot = snapshot_by_app[application_number]
            raw_code = snapshot.application_status_code
            if raw_code is not None:
                raw_code_str = str(raw_code)
                normalized_description = status_map.get(raw_code_str)
                if normalized_description is not None:
                    snapshot.application_status_description = normalized_description
                    snapshot.status_normalized_at = _current_utc_timestamp()
                    snapshot.status_code_version = status_code_version
                else:
                    errors += 1

            normalized_entries.append(
                StatusNormalizeEntry(
                    application_number_text=snapshot.application_number_text,
                    raw_code=raw_code,
                    normalized_description=snapshot.application_status_description,
                    normalized_at=_ensure_utc_timestamp(snapshot.status_normalized_at),
                )
            )

        await session.flush()

    logger.info(
        "Normalized snapshot statuses",
        workspace_id=request.workspace_id,
        total=len(normalized_entries),
        errors=errors,
        status_code_version=status_code_version,
    )

    return StatusNormalizeResponse(
        normalized=normalized_entries,
        status_code_version=status_code_version,
        metadata=StatusNormalizeMetadata(
            total_processed=len(normalized_entries),
            errors=errors,
        ),
    )


__all__ = ["uspto_status_normalize"]
