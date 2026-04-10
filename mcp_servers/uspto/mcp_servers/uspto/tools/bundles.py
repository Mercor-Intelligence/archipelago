"""Bundle export tools for the USPTO MCP server."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Annotated

from loguru import logger
from pydantic import Field

from mcp_servers.uspto.auth.rate_limiter import rate_limiter
from mcp_servers.uspto.db import get_db
from mcp_servers.uspto.db.models import (
    ApplicationSnapshot,
    DocumentRecord,
    ForeignPriorityRecord,
)
from mcp_servers.uspto.models import (
    AuditEntry,
    BundleApplication,
    BundleExportOptions,
    BundleExportResponse,
    BundleMetadata,
    ExportBundleRequest,
    SavedQueryResponse,
)
from mcp_servers.uspto.repositories.bundles import BundlesRepository
from mcp_servers.uspto.repositories.workspace import WorkspaceRepository
from mcp_servers.uspto.utils.errors import (
    NotFoundError,
    RateLimitError,
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


@handle_errors
async def uspto_bundles_export(
    workspace_id: Annotated[
        str,
        Field(
            pattern=r"^ws_[a-f0-9]{12}$",
            description="Workspace whose assets are exported.",
        ),
    ],
    bundle_name: Annotated[
        str,
        Field(
            min_length=1,
            max_length=200,
            description="Friendly name for the exported bundle.",
        ),
    ],
    include_queries: Annotated[
        list[str] | None,
        Field(description="Optional list of saved query IDs to include."),
    ] = None,
    include_applications: Annotated[
        list[str] | None,
        Field(description="Optional list of application numbers to include."),
    ] = None,
    include_documents: Annotated[
        bool,
        Field(description="Include document metadata in the exported bundle."),
    ] = True,
    include_foreign_priority: Annotated[
        bool,
        Field(description="Include foreign priority claims if they exist."),
    ] = True,
    include_audit_trail: Annotated[
        bool,
        Field(description="Include audit trail entries with the bundle."),
    ] = True,
) -> BundleExportResponse:
    """Export a citation-ready research bundle containing workspace assets with provenance.

    Exports saved queries, application snapshots, documents, foreign priority data,
    and optional audit trail into a single bundle for archival or sharing.

    INCLUSION CONTROL (tri-state logic for include_queries and include_applications):
    - null/omit: Include ALL items in the workspace (default)
    - empty array []: Include NO items
    - array of IDs: Include ONLY those specific items

    OPTIONS:
    - include_documents: Include document metadata (default: true)
    - include_foreign_priority: Include foreign priority claims (default: true)
    - include_audit_trail: Include audit history (default: true)

    COMMON ERRORS:
    - NOT_FOUND: workspace_id does not exist
    - RATE_LIMIT_EXCEEDED: Too many requests (export: 20/min)
    """
    request = ExportBundleRequest(
        workspace_id=workspace_id,
        bundle_name=bundle_name,
        include_queries=include_queries,
        include_applications=include_applications,
        options=BundleExportOptions(
            include_documents=include_documents,
            include_foreign_priority=include_foreign_priority,
            include_audit_trail=include_audit_trail,
        ),
    )
    # 1. Check session-scoped rate limit (export category: 20 req/min)
    rate_limit = rate_limiter.check_rate_limit("export")
    if not rate_limit.allowed:
        raise RateLimitError(
            limit=rate_limit.limit,
            retry_after=rate_limit.retry_after,
            reset_at=rate_limit.reset_at,
        )

    async with get_db() as session:
        workspace_repo = WorkspaceRepository(session)
        bundles_repo = BundlesRepository(session)

        # 3. Validate workspace exists in current session (session-scoped, no user ownership check)
        workspace = await workspace_repo.get_workspace(request.workspace_id)
        if not workspace:
            raise NotFoundError("workspace", request.workspace_id)

        # 4. Fetch queries (all or selected)
        if request.include_queries is not None:
            # Explicit list provided (empty list means include none)
            queries = await bundles_repo.get_queries_by_ids(
                request.workspace_id, request.include_queries
            )
        else:
            # None means use default: include all
            queries = await bundles_repo.get_all_queries(request.workspace_id)

        # Convert queries to SavedQueryResponse
        query_responses = []
        for query in queries:
            query_responses.append(
                SavedQueryResponse(
                    query_id=query.id,
                    workspace_id=query.workspace_id,
                    name=query.name,
                    query=query.query_text,
                    filters=json.loads(query.filters) if query.filters else None,
                    pinned_results=(
                        json.loads(query.pinned_results) if query.pinned_results else []
                    ),
                    notes=query.notes,
                    created_at=_ensure_utc_timestamp(query.created_at) or _current_utc_timestamp(),
                    last_run_at=_ensure_utc_timestamp(query.last_run_at),
                    run_count=query.run_count or 0,
                )
            )

        # 5. Fetch application snapshots (all or selected)
        if request.include_applications is not None:
            # Explicit list provided (empty list means include none)
            snapshots = await bundles_repo.get_snapshots_by_app_numbers(
                request.workspace_id, request.include_applications
            )
        else:
            # None means use default: include all
            snapshots = await bundles_repo.get_all_snapshots(request.workspace_id)

        # Get latest snapshot per application number
        latest_by_app: dict[str, ApplicationSnapshot] = {}
        for snapshot in snapshots:
            if snapshot.application_number_text not in latest_by_app:
                latest_by_app[snapshot.application_number_text] = snapshot

        application_numbers = list(latest_by_app.keys())

        # 6. Fetch documents and foreign priority in batch
        options = request.options or BundleExportOptions()
        applications = []
        total_documents = 0

        documents_by_app: dict[str, list[DocumentRecord]] = {}
        if options.include_documents and application_numbers:
            documents = await bundles_repo.get_documents_for_applications(
                request.workspace_id,
                application_numbers,
            )
            for document in documents:
                documents_by_app.setdefault(document.application_number_text, []).append(document)

        foreign_priority_by_app: dict[str, list[ForeignPriorityRecord]] = {}
        if options.include_foreign_priority and application_numbers:
            foreign_priority_records = await bundles_repo.get_foreign_priority_for_applications(
                request.workspace_id,
                application_numbers,
            )
            for record in foreign_priority_records:
                app_records = foreign_priority_by_app.setdefault(record.application_number_text, [])
                app_records.append(record)

        for app_number, latest_snapshot in latest_by_app.items():
            document_identifiers = []
            if options.include_documents:
                app_documents = documents_by_app.get(app_number, [])
                document_identifiers = [doc.document_identifier for doc in app_documents]
                total_documents += len(document_identifiers)

            foreign_priority_included = False
            if options.include_foreign_priority:
                foreign_priority_included = bool(foreign_priority_by_app.get(app_number))

            applications.append(
                BundleApplication(
                    application_number_text=app_number,
                    snapshot_version=latest_snapshot.version,
                    documents_included=document_identifiers,
                    foreign_priority_included=foreign_priority_included,
                    notes=None,
                )
            )

        # 7. Optionally include audit trail (session-scoped)
        audit_entries = []
        if options.include_audit_trail:
            audit_logs = await bundles_repo.get_audit_log_for_workspace(request.workspace_id)

            for log in audit_logs:
                details = json.loads(log.details) if log.details else {}
                audit_entries.append(
                    AuditEntry(
                        audit_id=f"audit_{log.id}",
                        timestamp=_ensure_utc_timestamp(log.created_at) or _current_utc_timestamp(),
                        action=log.action,
                        resource_type=log.resource_type or "",
                        resource_id=log.resource_id,
                        details=details,
                        description=None,
                    )
                )

        # 8. Assemble bundle
        bundle_id = f"bundle_{uuid.uuid4().hex[:12]}"
        exported_at = _current_utc_timestamp()

        metadata = BundleMetadata(
            exported_at=exported_at,
            application_count=len(applications),
            document_count=total_documents,
            bundle_size_bytes=None,  # Could be calculated if needed
            notes=None,
        )

        logger.info(
            f"Exported bundle: {request.bundle_name}",
            bundle_id=bundle_id,
            workspace_id=request.workspace_id,
            query_count=len(query_responses),
            application_count=len(applications),
            document_count=total_documents,
            audit_entry_count=len(audit_entries),
        )

        return BundleExportResponse(
            bundle_id=bundle_id,
            bundle_name=request.bundle_name,
            workspace_id=request.workspace_id,
            queries=query_responses,
            applications=applications,
            audit_trail=audit_entries,
            metadata=metadata,
        )


__all__ = ["uspto_bundles_export"]
