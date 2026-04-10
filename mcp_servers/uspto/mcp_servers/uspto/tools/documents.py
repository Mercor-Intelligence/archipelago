"""Document retrieval tools for the USPTO MCP server."""

from __future__ import annotations

import json
import time
import uuid
from typing import Annotated, Any

from loguru import logger
from pydantic import Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_servers.uspto.api.factory import get_uspto_client
from mcp_servers.uspto.auth.rate_limiter import rate_limiter
from mcp_servers.uspto.db import get_db
from mcp_servers.uspto.db.models import DocumentRecord, Workspace
from mcp_servers.uspto.models import (
    DocumentRecord as DocumentRecordModel,
)
from mcp_servers.uspto.models import (
    DocumentsListResponse,
    DownloadOption,
    GetDownloadUrlRequest,
    GetDownloadUrlResponse,
    ListDocumentsRequest,
    PaginationMeta,
    RetrievalMetadata,
)
from mcp_servers.uspto.repositories.documents import DocumentsRepository
from mcp_servers.uspto.utils.errors import (
    DocumentsUnavailableError,
    DownloadUnavailableError,
    InvalidRequestError,
    NotFoundError,
    RateLimitError,
    USPTOError,
    ValidationError,
    handle_errors,
)


@handle_errors
async def uspto_documents_list(
    application_number_text: Annotated[
        str,
        Field(
            pattern=r"^\d{2}/\d{3},\d{3}$",
            description="Formatted application number for document discovery.",
        ),
    ],
    start: Annotated[
        int,
        Field(
            ge=0,
            description="Zero-based offset into the document list.",
        ),
    ] = 0,
    rows: Annotated[
        int,
        Field(
            ge=1,
            le=500,
            description="Number of documents to return (1-500).",
        ),
    ] = 100,
) -> DocumentsListResponse:
    """Retrieve prosecution document inventory for a patent application.

    Returns document metadata including document_identifier, document_code,
    official_date, and available download options (MIME types, URLs).

    DATASET COVERAGE: Documents available for applications with filing dates 2001-present.

    WORKFLOW: Call this FIRST to discover available documents and their identifiers,
    then use uspto_documents_get_download_url to get specific download URLs.

    PAGINATION: Use 'start' and 'rows' parameters. Default rows=100, max rows=500.

    COMMON ERRORS:
    - DOCUMENTS_UNAVAILABLE: Application predates document digitization coverage
    - RATE_LIMIT_EXCEEDED: Too many requests (documents: 50/min)
    - OFFLINE_MODE_ACTIVE: Returns empty list in offline mode
    """
    request = ListDocumentsRequest(
        application_number_text=application_number_text, start=start, rows=rows
    )
    # 1. Check session-scoped rate limit (documents category: 50 req/min)
    rate_limit = rate_limiter.check_rate_limit("documents")
    if not rate_limit.allowed:
        raise RateLimitError(
            limit=rate_limit.limit,
            retry_after=rate_limit.retry_after,
            reset_at=rate_limit.reset_at,
        )

    # 2. Validate rows parameter (max 500)
    if request.rows > 500:
        raise InvalidRequestError(
            message="rows parameter cannot exceed 500",
            details={"maxRows": 500, "providedRows": request.rows},
        )

    # 3. Create USPTO API client (factory handles API key internally)
    client = get_uspto_client()

    try:
        # 5. Execute documents list request
        logger.info(
            "Executing USPTO documents list",
            application_number=request.application_number_text,
            start=request.start,
            rows=request.rows,
        )

        start_time = time.time()

        upstream_response = await client.get_documents(
            application_number=request.application_number_text,
            start=request.start,
            rows=request.rows,
        )

        execution_time_ms = int((time.time() - start_time) * 1000)

        # 6. Check for upstream errors
        if "error" in upstream_response:
            error_info = upstream_response["error"]
            error_code = error_info.get("code", "UPSTREAM_ERROR")

            # Handle offline mode specifically - return empty results
            # (like workspace tools work offline)
            if error_code == "OFFLINE_MODE_ACTIVE":
                logger.warning(
                    "Offline mode active - returning empty document list",
                    application_number=request.application_number_text,
                )
                # Return empty response to allow tool to work in offline mode
                # for testing/development
                return DocumentsListResponse(
                    application_number_text=request.application_number_text,
                    documents=[],
                    pagination=PaginationMeta(
                        start=request.start,
                        rows=request.rows,
                        total_results=0,
                    ),
                    metadata=RetrievalMetadata(
                        retrieved_at=DocumentsRepository.ensure_utc_timestamp(),
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

                # Handle 404 as documents unavailable
                if upstream_status == 404 or error_code == "DATASET_COVERAGE_UNAVAILABLE":
                    reason = (
                        error_details.get("reason")
                        or "Application predates document digitization coverage"
                    )
                    raise DocumentsUnavailableError(
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

        # 7. Transform results to Pydantic models
        # The API client already transforms documentBag -> documents
        # and downloadOptionBag -> downloadOptions
        raw_documents = upstream_response.get("documents") or []
        total = upstream_response.get("total")
        if total is None:
            total = upstream_response.get("totalFound")
        total_results = total if total is not None else len(raw_documents)

        documents = []
        for doc_data in raw_documents:
            document = DocumentsRepository.parse_document_from_api(doc_data)
            if document:
                documents.append(document)

        # 8. Build response
        response = DocumentsListResponse(
            application_number_text=request.application_number_text,
            documents=documents,
            pagination=PaginationMeta(
                start=request.start,
                rows=request.rows,
                total_results=total_results,
            ),
            metadata=RetrievalMetadata(
                retrieved_at=DocumentsRepository.ensure_utc_timestamp(),
                execution_time_ms=execution_time_ms,
                dataset_coverage=(
                    "Document metadata available for applications with filing dates 2001-present"
                ),
            ),
        )

        logger.info(
            "Documents list completed",
            application_number=request.application_number_text,
            total_results=total_results,
            returned_documents=len(documents),
            execution_time_ms=execution_time_ms,
            cached=upstream_response.get("cached", False),
        )

        try:
            await _cache_documents(request.application_number_text, documents)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.error(
                "Failed to cache documents",
                application_number=request.application_number_text,
                error=str(exc),
            )

        return response

    finally:
        await client.aclose()


@handle_errors
async def uspto_documents_get_download_url(
    application_number_text: Annotated[
        str,
        Field(
            pattern=r"^\d{2}/\d{3},\d{3}$",
            description="Application number that owns the document.",
        ),
    ],
    document_identifier: Annotated[
        str,
        Field(
            description="USPTO identifier for the specific document.",
        ),
    ],
    preferred_mime_type: Annotated[
        str,
        Field(
            description="Preferred MIME type for the download link.",
        ),
    ] = "application/pdf",
) -> GetDownloadUrlResponse:
    """Get a download URL for a specific prosecution document.

    PREREQUISITE: Must call uspto_documents_list FIRST to cache document metadata.
    Use the document_identifier from that response.

    MIME TYPE SELECTION: Specify preferred_mime_type (default: 'application/pdf').
    If unavailable, returns HTTP 422 with available options.

    URL EXPIRY: Download URLs may expire after several hours. Re-fetch if download fails.

    COMMON ERRORS:
    - NOT_FOUND: Document not in cache (call uspto_documents_list first)
    - PREFERRED_MIME_TYPE_UNAVAILABLE: Requested MIME type not available for this document
    - DOWNLOAD_UNAVAILABLE: Document metadata present but no download URL from USPTO
    - RATE_LIMIT_EXCEEDED: Too many requests (documents_download: 100/min)
    """
    request = GetDownloadUrlRequest(
        application_number_text=application_number_text,
        document_identifier=document_identifier,
        preferred_mime_type=preferred_mime_type,
    )
    # 1. Check session-scoped rate limit (documents_download category: 100 req/min)
    rate_limit = rate_limiter.check_rate_limit("documents_download")
    if not rate_limit.allowed:
        raise RateLimitError(
            limit=rate_limit.limit,
            retry_after=rate_limit.retry_after,
            reset_at=rate_limit.reset_at,
        )

    # 3. Query database for cached document record using repository
    response: GetDownloadUrlResponse | None = None
    async with get_db() as session:
        repo = DocumentsRepository(session)

        # Get document record
        db_record = await repo.get_document(
            application_number_text=request.application_number_text,
            document_identifier=request.document_identifier,
        )

        if db_record is None:
            # Check if any documents exist for this application to provide better error context
            has_any_documents = await repo.has_documents_for_application(
                request.application_number_text
            )

            if has_any_documents:
                # Document exists for app but not this specific identifier
                message = (
                    f"Document '{request.document_identifier}' not found for application "
                    f"{request.application_number_text}. The document may not exist or may have "
                    f"a different identifier. Use 'uspto_documents_list' to see available "
                    f"documents."
                )
                error_details = {
                    "applicationNumber": request.application_number_text,
                    "suggestion": (
                        "Use 'uspto_documents_list' to retrieve the list of available documents "
                        "for this application"
                    ),
                }
            else:
                # No documents cached for this application
                message = (
                    f"Document '{request.document_identifier}' not found for application "
                    f"{request.application_number_text}. No documents are currently cached for "
                    f"this application. Use 'uspto_documents_list' to retrieve and cache "
                    f"documents first."
                )
                error_details = {
                    "applicationNumber": request.application_number_text,
                    "suggestion": (
                        "Use 'uspto_documents_list' to retrieve and cache documents for this "
                        "application first"
                    ),
                }

            # Create a more helpful error with context
            raise NotFoundError(
                resource_type="document",
                resource_id=request.document_identifier,
                message=message,
                details=error_details,
            )

        # 4. Parse download_options JSON using repository
        if not db_record.download_options:
            raise DownloadUnavailableError(
                document_id=request.document_identifier,
                reason="Document metadata present but download URL not provided by USPTO",
            )

        try:
            download_options = repo.parse_download_options(db_record.download_options)
        except ValueError as e:
            logger.error(
                "Failed to parse download_options JSON",
                document_identifier=request.document_identifier,
                error=str(e),
            )
            raise DownloadUnavailableError(
                document_id=request.document_identifier,
                reason="Invalid download options format in cached metadata",
            )

        if not download_options:
            raise DownloadUnavailableError(
                document_id=request.document_identifier,
                reason="No valid download options available for document",
            )

        # 6. Select download option matching preferred MIME type
        selected_option: DownloadOption | None = None
        for option in download_options:
            if option.mime_type_identifier == request.preferred_mime_type:
                selected_option = option
                break

        # 7. If preferred MIME type not available, return 422 error
        if selected_option is None:
            available_mime_types = [opt.mime_type_identifier for opt in download_options]
            message = (
                f"Preferred MIME type '{request.preferred_mime_type}' "
                f"not available for document {request.document_identifier}"
            )
            raise ValidationError(
                code="PREFERRED_MIME_TYPE_UNAVAILABLE",
                message=message,
                details={
                    "documentIdentifier": request.document_identifier,
                    "preferredMimeType": request.preferred_mime_type,
                    "availableMimeTypes": available_mime_types,
                },
            )

        # 8. Build response with metadata
        url_generated_at = DocumentsRepository.ensure_utc_timestamp()
        response = GetDownloadUrlResponse(
            document_identifier=request.document_identifier,
            download_url=selected_option.download_url,
            mime_type_identifier=selected_option.mime_type_identifier,
            page_count=selected_option.page_count,
            file_size_bytes=selected_option.file_size_bytes,
            metadata={
                "urlGeneratedAt": url_generated_at,
                "urlExpiryWarning": (
                    "Download URLs may expire after several hours; re-fetch if needed"
                ),
            },
        )

        logger.info(
            "Download URL retrieved",
            document_identifier=request.document_identifier,
            application_number=request.application_number_text,
            mime_type=selected_option.mime_type_identifier,
            preferred_mime_type=request.preferred_mime_type,
            matched_preferred=(selected_option.mime_type_identifier == request.preferred_mime_type),
        )

    # Return response (outside async context manager for proper serialization)
    return response


async def _cache_documents(
    application_number_text: str,
    documents: list[DocumentRecordModel],
) -> None:
    """Persist the retrieved documents for later download URL lookups."""

    if not documents:
        return

    async with get_db() as session:
        workspace_id = await _get_documents_workspace(session)
        for document in documents:
            await _upsert_document_record(
                session,
                workspace_id,
                application_number_text,
                document,
            )


async def _get_documents_workspace(session: AsyncSession) -> str:
    """Ensure the auto-generated document cache workspace exists."""

    cache_workspace_name = "__documents_cache_workspace__"
    query = select(Workspace).where(Workspace.name == cache_workspace_name).limit(1)
    workspace = (await session.execute(query)).scalar_one_or_none()
    if workspace:
        return workspace.id

    workspace_id = f"ws_{uuid.uuid4().hex[:12]}"
    workspace = Workspace(
        id=workspace_id,
        name=cache_workspace_name,
        description="Auto-created workspace for caching document metadata.",
    )
    session.add(workspace)
    try:
        await session.flush()
        return workspace_id
    except IntegrityError:
        await session.rollback()
        workspace = (await session.execute(query)).scalar_one()
        return workspace.id


async def _upsert_document_record(
    session: AsyncSession,
    workspace_id: str,
    application_number_text: str,
    document: DocumentRecordModel,
) -> None:
    """Insert or replace a document record in the cache workspace."""

    await session.execute(
        delete(DocumentRecord)
        .where(DocumentRecord.workspace_id == workspace_id)
        .where(DocumentRecord.application_number_text == application_number_text)
        .where(DocumentRecord.document_identifier == document.document_identifier)
    )

    download_options_payload = [
        _download_option_to_api_dict(option) for option in (document.download_options or [])
    ]
    record = DocumentRecord(
        id=f"doc_{uuid.uuid4().hex[:12]}",
        workspace_id=workspace_id,
        application_number_text=application_number_text,
        document_identifier=document.document_identifier,
        document_code=document.document_code,
        document_code_description=document.document_code_description_text,
        official_date=document.official_date,
        direction_category=document.direction_category,
        download_options=json.dumps(download_options_payload, ensure_ascii=False),
        retrieved_at=DocumentsRepository.ensure_utc_timestamp(),
    )
    session.add(record)


def _download_option_to_api_dict(option: DownloadOption) -> dict[str, Any]:
    """Convert normalized download option to the API-style payload stored in the DB."""

    payload: dict[str, Any] = {
        "mimeTypeIdentifier": option.mime_type_identifier,
        "downloadUrl": option.download_url,
    }
    if option.page_count is not None:
        payload["pageCount"] = option.page_count
    if option.file_size_bytes is not None:
        payload["fileSizeBytes"] = option.file_size_bytes
    return payload


__all__ = ["uspto_documents_list", "uspto_documents_get_download_url"]
