"""Snapshot tools for the USPTO MCP server."""

from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from loguru import logger
from pydantic import Field
from sqlalchemy.exc import IntegrityError

from mcp_servers.uspto.api.client import USPTOAPIClient
from mcp_servers.uspto.api.factory import get_uspto_client
from mcp_servers.uspto.auth.rate_limiter import rate_limiter
from mcp_servers.uspto.db import get_db
from mcp_servers.uspto.models import (
    ApplicantInfo,
    BibliographicData,
    CreateSnapshotRequest,
    ForeignPriorityClaim,
    GetSnapshotRequest,
    ListSnapshotsRequest,
    ListSnapshotsResponse,
    PaginationResponse,
    ProsecutionEvent,
    ProvenanceData,
    SnapshotResponse,
    StatusData,
)
from mcp_servers.uspto.repositories.foreign_priority import ForeignPriorityRepository
from mcp_servers.uspto.repositories.snapshots import SnapshotRepository
from mcp_servers.uspto.repositories.workspace import WorkspaceRepository
from mcp_servers.uspto.utils.audit import log_audit_event
from mcp_servers.uspto.utils.dates import coerce_iso_date
from mcp_servers.uspto.utils.errors import (
    NotFoundError,
    RateLimitError,
    SnapshotConflictError,
    USPTOError,
    ValidationError,
    handle_errors,
)
from mcp_servers.uspto.utils.transform import extract_prosecution_events_from_payload


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


def _normalize_event_date(value: str | None) -> str | None:
    """Return ISO 8601 event date or None if invalid."""
    if not value or not isinstance(value, str):
        return None
    try:
        datetime.fromisoformat(value)
        return value
    except ValueError:
        return None


def _normalize_application_number_text(value: str) -> str:
    """Normalize application number for comparison by stripping non-digits."""
    return "".join(ch for ch in value if ch.isdigit())


async def _fetch_foreign_priority_claims(
    client: USPTOAPIClient,
    application_number_text: str,
) -> tuple[list[ForeignPriorityClaim], str | None, list[dict[str, Any]]]:
    """Fetch foreign priority claims for a snapshot if the upstream API returns them."""
    try:
        response = await client.get_foreign_priority(application_number_text)
    except Exception as exc:
        logger.warning(
            "Foreign priority lookup failed",
            application_number=application_number_text,
            error=str(exc),
        )
        return [], None, []

    if not isinstance(response, dict):
        return [], None, []

    if "error" in response:
        logger.debug(
            "Foreign priority lookup upstream error",
            application_number=application_number_text,
            error=response.get("error"),
        )
        return [], None, []

    raw_claims = response.get("foreignPriorityClaims") or []
    if not isinstance(raw_claims, list):
        raw_claims = []

    parsed_claims = ForeignPriorityRepository.parse_foreign_priority_claims(raw_claims)
    json_payload = json.dumps(raw_claims) if raw_claims else None
    return parsed_claims, json_payload, raw_claims


def _select_search_match(results: list[dict], application_number_text: str) -> dict | None:
    """Pick the matching search result by application number."""
    normalized_target = _normalize_application_number_text(application_number_text)
    for result in results:
        candidate = result.get("applicationNumberText")
        if not candidate:
            continue
        if _normalize_application_number_text(str(candidate)) == normalized_target:
            return result
    return None


async def _fallback_application_lookup(
    client: USPTOAPIClient,
    application_number_text: str,
) -> dict | None:
    """Fallback to search endpoint when application lookup fails."""
    search_response = await client.search_applications(
        query=application_number_text,
        start=0,
        rows=25,
    )
    if not isinstance(search_response, dict):
        return None
    if "error" in search_response:
        return None
    results = search_response.get("results")
    if not isinstance(results, list) or not results:
        return None
    match = _select_search_match(results, application_number_text)
    return match


async def _get_cached_status_codes(session) -> dict | None:
    """Retrieve cached USPTO status codes for the current session."""
    from sqlalchemy import select

    from mcp_servers.uspto.db.models import StatusCode as StatusCodeRow

    result = await session.execute(select(StatusCodeRow))
    cached_codes = list(result.scalars().all())

    if not cached_codes:
        return None

    return {
        "statusCodes": [
            {
                "statusCode": code.status_code,
                "statusDescriptionText": code.status_description_text,
            }
            for code in cached_codes
        ],
        "version": cached_codes[0].version or "unknown",
    }


async def _normalize_status(
    session, raw_code: str | int | None
) -> tuple[str | None, str | None, str | None]:
    """Normalize USPTO status codes using the cached reference table."""
    if raw_code is None:
        return None, None, None

    # Convert to string for comparison (USPTO API may return int or str)
    raw_code_str = str(raw_code)

    status_codes_data = await _get_cached_status_codes(session)
    if not status_codes_data:
        logger.warning("Status codes cache miss during normalization")
        return None, None, None

    for code_entry in status_codes_data.get("statusCodes", []):
        if code_entry.get("statusCode") == raw_code_str:
            return (
                code_entry.get("statusDescriptionText"),
                _current_utc_timestamp(),
                status_codes_data.get("version"),
            )

    logger.warning(f"Status code {raw_code_str} not found in reference table")
    return None, None, None


def _parse_priority_claims_json(value: str | None) -> list[ForeignPriorityClaim]:
    """Parse stored priority claims JSON for snapshots."""
    if not value:
        return []
    if not isinstance(value, str):
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        logger.warning("Invalid priority claims JSON stored with snapshot", payload=value)
        return []
    if not isinstance(payload, list):
        return []
    return ForeignPriorityRepository.parse_foreign_priority_claims(payload)


def _deserialize_raw_uspto_response(
    raw_response: str | dict[str, Any] | None,
    snapshot_id: str | None,
) -> dict[str, Any] | None:
    """Return the stored raw USPTO response as a dictionary."""
    if not raw_response:
        return None
    if isinstance(raw_response, str):
        try:
            raw_response = json.loads(raw_response)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Invalid raw USPTO response stored with snapshot",
                snapshot_id=snapshot_id,
                error=str(exc),
            )
            return None
    if isinstance(raw_response, dict):
        return raw_response
    return None


def _snapshot_prosecution_events(snapshot) -> list[ProsecutionEvent]:
    """Extract normalized prosecution events from a stored snapshot payload."""
    payload = _deserialize_raw_uspto_response(snapshot.raw_uspto_response, snapshot.id)
    if not payload:
        return []
    events_data = extract_prosecution_events_from_payload(payload)
    events: list[ProsecutionEvent] = []
    for event in events_data:
        event_code = event.get("eventCode")
        if not event_code:
            continue
        event_date = _normalize_event_date(event.get("eventDate"))
        if event.get("eventDate") and event_date is None:
            logger.warning(
                "Skipping prosecution event with malformed date",
                snapshot_id=snapshot.id,
                event_code=event_code,
                raw_date=event.get("eventDate"),
            )
            continue
        events.append(
            ProsecutionEvent(
                event_code=str(event_code),
                event_date=event_date,
                description=event.get("description"),
                document_reference=event.get("documentReference"),
            )
        )
    return events


def _transform_snapshot_to_response(snapshot) -> SnapshotResponse:
    """Transform database ApplicationSnapshot model to SnapshotResponse."""
    # Note: CPC classifications are stored in DB but not currently included in response
    # The BibliographicData model doesn't have a field for CPC classifications yet

    # Build ApplicantInfo if first_applicant_name exists
    first_named_applicant = None
    if snapshot.first_applicant_name:
        first_named_applicant = ApplicantInfo(name=snapshot.first_applicant_name)

    # Build inventor array
    inventor_names = []
    if snapshot.first_inventor_name:
        inventor_names = [snapshot.first_inventor_name]

    return SnapshotResponse(
        snapshot_id=snapshot.id,
        workspace_id=snapshot.workspace_id,
        application_number_text=snapshot.application_number_text,
        version=snapshot.version,
        bibliographic=BibliographicData(
            invention_title=snapshot.invention_title,
            filing_date=_ensure_utc_timestamp(snapshot.filing_date),
            publication_date=_ensure_utc_timestamp(snapshot.publication_date),
            publication_number=snapshot.publication_number,
            patent_number=snapshot.patent_number,
            patent_issue_date=_ensure_utc_timestamp(snapshot.patent_issue_date),
            first_named_applicant=first_named_applicant,
            assignee_entity_name=snapshot.assignee_entity_name,
            inventor_name_array_text=inventor_names,
            priority_claims=_parse_priority_claims_json(snapshot.priority_claims_json),
        ),
        status=StatusData(
            raw_code=snapshot.application_status_code,
            normalized_description=snapshot.application_status_description,
            normalized_at=_ensure_utc_timestamp(snapshot.status_normalized_at),
            status_code_version=snapshot.status_code_version,
        ),
        events=_snapshot_prosecution_events(snapshot),
        provenance=ProvenanceData(
            source="USPTO Patent Examination Data System API",
            retrieved_at=_ensure_utc_timestamp(snapshot.retrieved_at) or _current_utc_timestamp(),
            retrieved_by="USPTO MCP Server",
        ),
        created_at=_ensure_utc_timestamp(snapshot.created_at) or _current_utc_timestamp(),
    )


@handle_errors
async def uspto_snapshots_create(
    workspace_id: Annotated[
        str,
        Field(pattern=r"^ws_[a-f0-9]{12}$", description="Workspace that will own the snapshot."),
    ],
    application_number_text: Annotated[
        str,
        Field(
            pattern=r"^\d{2}/\d{3},\d{3}$",
            description="Formatted application number to snapshot.",
        ),
    ],
    auto_normalize_status: Annotated[
        bool,
        Field(description="Normalize status codes automatically when True."),
    ] = True,
) -> SnapshotResponse:
    """Capture a point-in-time snapshot of a patent application for tracking changes.

    Creates a versioned record of the application's current state including bibliographic
    data, status, foreign priority claims, and prosecution events. Multiple snapshots
    of the same application are version-numbered (1, 2, 3...) for historical comparison.

    REQUIRES: Valid workspace_id and application_number_text in formatted form (NN/NNN,NNN).

    AUTO-NORMALIZE: Set auto_normalize_status=true to automatically translate raw status
    codes using the cached reference table (requires prior call to uspto_status_codes_list).

    COMMON ERRORS:
    - NOT_FOUND: workspace_id does not exist
    - SNAPSHOT_CONFLICT: Race condition creating same version (retry will succeed)
    - OFFLINE_MODE_ACTIVE: Requires online mode to fetch USPTO data
    - DATASET_COVERAGE_UNAVAILABLE: Application outside data coverage
    - RATE_LIMIT_EXCEEDED: Too many requests (retrieval: 100/min)
    """
    request = CreateSnapshotRequest(
        workspace_id=workspace_id,
        application_number_text=application_number_text,
        auto_normalize_status=auto_normalize_status,
    )
    # 1. Check rate limit
    rate_limit = rate_limiter.check_rate_limit("retrieval")
    if not rate_limit.allowed:
        raise RateLimitError(
            limit=rate_limit.limit,
            retry_after=rate_limit.retry_after,
            reset_at=rate_limit.reset_at,
        )

    async with get_db() as session:
        # 2. Validate workspace exists
        workspace_repo = WorkspaceRepository(session)
        workspace = await workspace_repo.get_workspace(request.workspace_id)
        if not workspace:
            raise NotFoundError("workspace", request.workspace_id)

        # 3. Call USPTO API to fetch application data (factory handles API key internally)
        client = get_uspto_client()

        logger.info(f"Fetching application {request.application_number_text} for snapshot creation")
        start_time = time.time()

        try:
            upstream_response = await client.get_application(request.application_number_text)
        except Exception as e:
            logger.error(f"USPTO API error: {e}")
            raise

        execution_time_ms = int((time.time() - start_time) * 1000)
        logger.info(f"USPTO API call completed in {execution_time_ms}ms")

        # 4. Handle errors from upstream
        if "error" in upstream_response:
            error_info = upstream_response["error"]
            error_code = error_info.get("code", "UNKNOWN_ERROR")
            error_message = error_info.get("message", "Unknown error")
            error_details = error_info.get("details", {})

            if error_code == "OFFLINE_MODE_ACTIVE":
                raise USPTOError(
                    code="OFFLINE_MODE_ACTIVE",
                    message="Snapshot creation requires online mode to fetch USPTO data",
                    status_code=503,
                )

            fallback_response = None
            if error_code in [
                "DATASET_COVERAGE_UNAVAILABLE",
                "APPLICATION_NOT_FOUND",
                "UPSTREAM_CLIENT_ERROR",
            ]:
                fallback_response = await _fallback_application_lookup(
                    client,
                    request.application_number_text,
                )
                if fallback_response:
                    logger.warning(
                        "Falling back to search results for snapshot creation",
                        application_number=request.application_number_text,
                        upstream_error=error_code,
                    )
                    upstream_response = fallback_response
                else:
                    status_code = (
                        error_details.get("statusCode", 400)
                        if error_code == "UPSTREAM_CLIENT_ERROR"
                        else 422
                    )
                    raise USPTOError(
                        code=error_code,
                        message=error_message,
                        details=error_details,
                        status_code=status_code,
                    )

            if "error" in upstream_response:
                # Generic upstream error
                raise USPTOError(
                    code=error_code,
                    message=error_message,
                    details=error_details,
                    status_code=error_details.get("statusCode", 500),
                )

        # 5. Transform upstream response - extract bibliographic fields
        invention_title = upstream_response.get("inventionTitle")
        filing_date = coerce_iso_date(upstream_response.get("filingDate"))
        publication_date = coerce_iso_date(upstream_response.get("publicationDate"))
        publication_number = upstream_response.get("publicationNumber")
        patent_number = upstream_response.get("patentNumber")
        patent_issue_date = coerce_iso_date(upstream_response.get("patentIssueDate"))

        (
            priority_claims,
            priority_claims_json,
            raw_priority_claims,
        ) = await _fetch_foreign_priority_claims(
            client,
            request.application_number_text,
        )

        # Extract status
        raw_status_code = upstream_response.get("applicationStatusCode")
        raw_status_description = upstream_response.get(
            "applicationStatusDescriptionText"
        ) or upstream_response.get("statusDescriptionText")

        # Extract parties
        first_inventor_name = upstream_response.get("firstInventorName")
        first_applicant_name = upstream_response.get("firstApplicantName")
        if not first_applicant_name:
            first_named = upstream_response.get("firstNamedApplicant")
            if isinstance(first_named, dict):
                first_applicant_name = first_named.get("applicantName") or first_named.get("name")
        assignee_entity_name = upstream_response.get("assigneeEntityName")
        examiner_name = upstream_response.get("examinerName")
        group_art_unit_number = upstream_response.get("groupArtUnitNumber")

        # Extract classifications
        uspc_class = upstream_response.get("uspcClass")
        uspc_subclass = upstream_response.get("uspcSubclass")
        cpc_classifications = (
            json.dumps(upstream_response.get("cpcClassifications"))
            if upstream_response.get("cpcClassifications")
            else None
        )

        # Extract metadata
        entity_status = upstream_response.get("entityStatus")
        application_type = upstream_response.get("applicationType")
        confidential = upstream_response.get("confidential", False)

        # 6. Auto-normalize status if enabled
        status_normalized_at = None
        status_code_version = None
        normalized_status_description = raw_status_description

        if request.auto_normalize_status and raw_status_code is not None:
            (
                norm_desc,
                norm_at,
                norm_version,
            ) = await _normalize_status(session, raw_status_code)
            if norm_desc:
                normalized_status_description = norm_desc
                status_normalized_at = norm_at
                status_code_version = norm_version
                logger.info(
                    f"Auto-normalized status {raw_status_code} to '{norm_desc}' "
                    f"(version {norm_version})"
                )

        # 7. Get next version number
        foreign_priority_repo = ForeignPriorityRepository(session)
        snapshot_repo = SnapshotRepository(session)
        next_version = await snapshot_repo.get_next_version_number(
            request.workspace_id,
            request.application_number_text,
        )

        if raw_priority_claims:
            await foreign_priority_repo.store_foreign_priority_claims(
                request.workspace_id, request.application_number_text, raw_priority_claims
            )

        # 8. Create snapshot record
        snapshot_id = f"snap_{uuid.uuid4().hex[:12]}"
        retrieved_at = _current_utc_timestamp()

        try:
            snapshot = await snapshot_repo.create_snapshot(
                id=snapshot_id,
                workspace_id=request.workspace_id,
                application_number_text=request.application_number_text,
                version=next_version,
                invention_title=invention_title,
                filing_date=filing_date,
                publication_date=publication_date,
                publication_number=publication_number,
                patent_number=patent_number,
                patent_issue_date=patent_issue_date,
                application_status_code=raw_status_code,
                application_status_description=normalized_status_description,
                status_normalized_at=status_normalized_at,
                status_code_version=status_code_version,
                first_inventor_name=first_inventor_name,
                first_applicant_name=first_applicant_name,
                assignee_entity_name=assignee_entity_name,
                examiner_name=examiner_name,
                group_art_unit_number=group_art_unit_number,
                uspc_class=uspc_class,
                uspc_subclass=uspc_subclass,
                cpc_classifications=cpc_classifications,
                entity_status=entity_status,
                application_type=application_type,
                confidential=confidential,
                raw_uspto_response=json.dumps(upstream_response),
                priority_claims_json=priority_claims_json,
                retrieved_at=retrieved_at,
            )
        except IntegrityError as e:
            # Race condition: only catch the specific version constraint
            # Note: Cannot query session after failed flush (invalid state)
            # Check both constraint name and SQLite's "table.column" format
            err_msg = str(e).lower()
            is_version_conflict = "uq_workspace_app_number_version" in err_msg or (
                "application_snapshots" in err_msg and "version" in err_msg
            )
            if is_version_conflict:
                raise SnapshotConflictError(
                    application_number=request.application_number_text,
                    version=next_version,
                    workspace_id=request.workspace_id,
                ) from e
            raise

        # 9. Log audit event
        await log_audit_event(
            session,
            action="snapshot_created",
            resource_type="snapshot",
            resource_id=snapshot_id,
            workspace_id=request.workspace_id,
            details={
                "applicationNumberText": request.application_number_text,
                "version": next_version,
                "autoNormalized": (
                    request.auto_normalize_status and status_normalized_at is not None
                ),
                "statusCode": raw_status_code,
                "priorityClaims": len(priority_claims),
            },
        )

        # 10. Build and return response
        logger.info(
            f"Created snapshot {snapshot_id} v{next_version} for {request.application_number_text}"
        )
        return _transform_snapshot_to_response(snapshot)


@handle_errors
async def uspto_snapshots_get(
    workspace_id: Annotated[
        str,
        Field(pattern=r"^ws_[a-f0-9]{12}$", description="Workspace owning the snapshot."),
    ],
    application_number_text: Annotated[
        str,
        Field(
            pattern=r"^\d{2}/\d{3},\d{3}$",
            description="Formatted application number identifying the snapshot.",
        ),
    ],
    version: Annotated[
        int | None,
        Field(ge=1, description="Optional snapshot version; latest is returned when omitted."),
    ] = None,
) -> SnapshotResponse:
    """Retrieve a specific snapshot by application number and optional version.

    PREREQUISITE: Snapshot must exist - created via uspto_snapshots_create.

    VERSION SELECTION:
    - Omit 'version' or pass null: Returns the most recent snapshot
    - Specify version number (1, 2, 3...): Returns that exact version

    Returns full snapshot data including bibliographic info, status, prosecution events,
    foreign priority claims, and provenance metadata.

    COMMON ERRORS:
    - NOT_FOUND: workspace_id or snapshot does not exist
    - RATE_LIMIT_EXCEEDED: Too many requests (retrieval: 100/min)
    """
    request = GetSnapshotRequest(
        workspace_id=workspace_id,
        application_number_text=application_number_text,
        version=version,
    )
    # 1. Check rate limit
    rate_limit = rate_limiter.check_rate_limit("retrieval")
    if not rate_limit.allowed:
        raise RateLimitError(
            limit=rate_limit.limit,
            retry_after=rate_limit.retry_after,
            reset_at=rate_limit.reset_at,
        )

    async with get_db() as session:
        # 2. Validate workspace exists
        workspace_repo = WorkspaceRepository(session)
        workspace = await workspace_repo.get_workspace(request.workspace_id)
        if not workspace:
            raise NotFoundError("workspace", request.workspace_id)

        # 3. Fetch snapshot from repository
        snapshot_repo = SnapshotRepository(session)
        snapshot = await snapshot_repo.get_snapshot_by_app_and_version(
            workspace_id=request.workspace_id,
            application_number_text=request.application_number_text,
            version=request.version,  # None means latest
        )

        if not snapshot:
            if request.version:
                raise NotFoundError(
                    "snapshot",
                    f"{request.application_number_text} v{request.version}",
                )
            else:
                raise NotFoundError("snapshot", request.application_number_text)

        # 4. Build and return response
        version_msg = f"v{snapshot.version}" if request.version else f"v{snapshot.version} (latest)"
        logger.info(
            f"Retrieved snapshot {snapshot.id} {version_msg} for {request.application_number_text}"
        )
        return _transform_snapshot_to_response(snapshot)


@handle_errors
async def uspto_snapshots_list(
    workspace_id: Annotated[
        str,
        Field(
            pattern=r"^ws_[a-f0-9]{12}$",
            description="Workspace whose snapshots should be listed.",
        ),
    ],
    cursor: Annotated[
        str | None,
        Field(description="Cursor returned by prior snapshot list calls."),
    ] = None,
    limit: Annotated[
        int,
        Field(ge=1, le=200, description="Maximum snapshots to return per page (1-200)."),
    ] = 50,
) -> ListSnapshotsResponse:
    """List all snapshots in a workspace with cursor-based pagination.

    Returns snapshot summaries ordered by creation time (newest first).

    PAGINATION: Default limit is 100. When has_more=true, use next_cursor to continue.
    Do NOT construct cursor values manually - always use the exact string returned.

    COMMON ERRORS:
    - NOT_FOUND: workspace_id does not exist
    - INVALID_CURSOR: Malformed or expired pagination cursor
    - RATE_LIMIT_EXCEEDED: Too many requests (retrieval: 100/min)
    """
    request = ListSnapshotsRequest(workspace_id=workspace_id, cursor=cursor, limit=limit)
    # 1. Check rate limit
    rate_limit = rate_limiter.check_rate_limit("retrieval")
    if not rate_limit.allowed:
        raise RateLimitError(
            limit=rate_limit.limit,
            retry_after=rate_limit.retry_after,
            reset_at=rate_limit.reset_at,
        )

    async with get_db() as session:
        # 2. Validate workspace exists
        workspace_repo = WorkspaceRepository(session)
        workspace = await workspace_repo.get_workspace(request.workspace_id)
        if not workspace:
            raise NotFoundError("workspace", request.workspace_id)

        # 3. Parse cursor for offset
        offset = 0
        if request.cursor:
            try:
                offset = int(base64.b64decode(request.cursor).decode())
            except Exception:
                raise ValidationError(
                    code="INVALID_CURSOR",
                    message=(
                        "Invalid pagination cursor. Use the next_cursor from a previous response."
                    ),
                )
            if offset < 0:
                raise ValidationError(
                    code="INVALID_CURSOR",
                    message=(
                        "Invalid pagination cursor: offset cannot be negative. "
                        "Use the next_cursor from a previous response."
                    ),
                )

        # 4. Fetch snapshots with pagination (fetch one extra to check for more)
        snapshot_repo = SnapshotRepository(session)
        snapshots = await snapshot_repo.list_snapshots(
            workspace_id=request.workspace_id,
            offset=offset,
            limit=request.limit + 1,
        )

        # 5. Check if more results exist
        has_more = len(snapshots) > request.limit
        if has_more:
            snapshots = snapshots[: request.limit]

        # 6. Generate next cursor if more results exist
        next_cursor = None
        if has_more:
            next_offset = offset + request.limit
            next_cursor = base64.b64encode(str(next_offset).encode()).decode()

        # 7. Transform to response models
        snapshot_responses = [_transform_snapshot_to_response(snap) for snap in snapshots]

        # 8. Return response with cursor-based pagination
        logger.info(
            f"Listed {len(snapshots)} snapshots for workspace {request.workspace_id} "
            f"(offset={offset}, has_more={has_more})"
        )

        return ListSnapshotsResponse(
            snapshots=snapshot_responses,
            pagination=PaginationResponse(
                has_more=has_more,
                next_cursor=next_cursor,
            ),
        )


__all__ = [
    "uspto_snapshots_create",
    "uspto_snapshots_get",
    "uspto_snapshots_list",
]
