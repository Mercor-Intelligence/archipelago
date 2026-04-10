"""Request and Response models for the USPTO MCP server.

ALL tool implementations MUST use these models for validation and serialization.
CRITICAL: Session-scoped architecture - NO user_id or User models.
"""

# ruff: noqa: N805

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field, ValidationInfo, field_validator

WORKSPACE_ID_PATTERN = re.compile(r"^ws_[a-f0-9]{12}$")
QUERY_ID_PATTERN = re.compile(r"^qry_[a-f0-9]{8}$")
APPLICATION_NUMBER_PATTERN = re.compile(r"^\d{2}/\d{3},\d{3}$")
APPLICATION_NUMBER_LOOSE_PATTERN = re.compile(r"^(\d{2}/\d{3},\d{3}|\d{6,})$")
ISO8601_HINT = "YYYY-MM-DDTHH:MM:SS.ffffffZ (UTC)"


def _validate_iso8601(value: str | None | list[str], field_name: str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, list | tuple):
        if not value:
            return None
        value = value[0]
    if not isinstance(value, str):
        value = str(value)
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        display_name = field_name or "value"
        raise ValueError(f"{display_name} must be an ISO 8601 timestamp ({ISO8601_HINT})") from exc
    return value


# Workspace Models (session-scoped)
class CreateWorkspaceRequest(BaseModel):
    """Create workspace request payload."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Display name for the workspace (1-200 characters).",
    )
    description: str | None = Field(
        default=None,
        max_length=1000,
        description="Optional workspace description (max 1000 characters).",
    )
    metadata: dict[str, str | int | bool] | None = Field(
        default=None,
        description="Optional key/value metadata scoped to the current session.",
    )


class GetWorkspaceRequest(BaseModel):
    """Retrieve details for a single workspace."""

    workspace_id: str = Field(
        ...,
        pattern=WORKSPACE_ID_PATTERN.pattern,
        description=(
            "Workspace identifier from uspto_workspaces_create response. "
            "Format: 'ws_' prefix followed by exactly 12 lowercase hex characters "
            "(e.g., 'ws_abc123def456'). Do not construct manually - always use "
            "the workspace_id returned from a create or list operation."
        ),
    )


class ListWorkspacesRequest(BaseModel):
    """Page through the current session workspaces."""

    cursor: str | None = Field(
        default=None,
        description=(
            "Pagination cursor for retrieving the next page of results. "
            "IMPORTANT: Do not construct this value manually - always use the exact "
            "'next_cursor' string from a previous response. Pass null or omit to "
            "start from the first page. Invalid cursors return INVALID_CURSOR error."
        ),
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of workspaces to return (1-100).",
    )


class WorkspaceStats(BaseModel):
    """Session workspace counters."""

    saved_queries: int = Field(
        ...,
        ge=0,
        description="Number of saved queries associated with the workspace.",
    )
    snapshots: int = Field(
        ...,
        ge=0,
        description="Count of snapshots created in this workspace.",
    )
    documents_retrieved: int = Field(
        default=0,
        ge=0,
        description="Documents retrieved through the workspace session.",
    )
    foreign_priority_records: int = Field(
        default=0,
        ge=0,
        description="Foreign priority claims captured for the workspace.",
    )


class RecentActivityItem(BaseModel):
    """Single activity entry for workspace recent activity."""

    action: str = Field(..., description="Action that was performed.")
    application_number: str | None = Field(
        default=None,
        description="Application number if the action was on an application.",
    )
    timestamp: str = Field(
        ...,
        description=f"When the action occurred in ISO 8601 ({ISO8601_HINT}).",
    )

    @field_validator("timestamp", mode="before")
    def _validate_activity_timestamp(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _validate_iso8601(value, info.field_name)


class WorkspaceResponse(BaseModel):
    """Workspace details returned by the MCP server."""

    workspace_id: str = Field(
        ...,
        pattern=WORKSPACE_ID_PATTERN.pattern,
        description=(
            "Workspace identifier. Format: 'ws_' prefix followed by exactly 12 "
            "lowercase hex characters (e.g., 'ws_abc123def456')."
        ),
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="User-visible workspace name.",
    )
    description: str | None = Field(
        default=None,
        description="Optional workspace description provided by the user.",
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="Session metadata stored with the workspace.",
    )
    created_at: str = Field(
        ...,
        description=f"Creation timestamp in ISO 8601 ({ISO8601_HINT}).",
    )
    updated_at: str = Field(
        ...,
        description=f"Last update timestamp in ISO 8601 ({ISO8601_HINT}).",
    )
    stats: WorkspaceStats = Field(
        ...,
        description="Session-scoped counters for saved artifacts in the workspace.",
    )
    recent_activity: list[RecentActivityItem] = Field(
        default_factory=list,
        description="Recent actions performed in this workspace (get only).",
    )

    @field_validator("created_at", "updated_at", mode="before")
    def _validate_workspace_timestamps(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _validate_iso8601(value, info.field_name)


class ListWorkspacesResponse(BaseModel):
    """Paginated response listing the session workspaces."""

    workspaces: list[WorkspaceResponse] = Field(
        default_factory=list,
        description="Workspaces returned in the current page.",
    )
    pagination: PaginationResponse = Field(
        ...,
        description="Cursor metadata for fetching the next page.",
    )


# Search Models
class SearchApplicationsRequest(BaseModel):
    """Search USPTO applications with optional filters."""

    query: str = Field(
        ...,
        min_length=1,
        description=(
            "USPTO search query using Solr field-based syntax. "
            'FORMAT: fieldName:value or fieldName:"phrase with spaces". '
            "SEARCHABLE FIELDS: inventionTitle, assigneeEntityName, applicationStatusCode, "
            "filingDate, publicationDate, patentNumber, applicationNumberText, "
            "firstNamedApplicant, firstInventorName, groupArtUnitNumber. "
            "BOOLEAN OPERATORS: AND, OR, NOT (must be UPPERCASE). "
            "RANGES: fieldName:[start TO end] (dates as YYYY-MM-DD). "
            "WILDCARDS: * (zero or more chars), ? (single char). "
            "EXAMPLES: 'inventionTitle:\"machine learning\" AND assigneeEntityName:Google', "
            "'filingDate:[2020-01-01 TO 2024-12-31]', 'applicationStatusCode:150'."
        ),
    )
    filters: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional filter constraints as key-value pairs. "
            "VALID KEYS: 'applicationType' (values: 'utility', 'design', 'plant', 'reissue'), "
            "'applicationStatusCategory' (values: 'pending', 'patented', 'abandoned'), "
            "'entityStatus' (values: 'small', 'micro', 'large'), "
            "'filingDateFrom' (YYYY-MM-DD), 'filingDateTo' (YYYY-MM-DD). "
            "EXAMPLE: {'applicationType': 'utility', 'filingDateFrom': '2020-01-01'}"
        ),
    )
    start: int = Field(
        default=0,
        ge=0,
        description="Zero-based offset into the search results.",
    )
    rows: int = Field(
        default=25,
        ge=1,
        le=100,
        description="Page size for search results (1-100).",
    )
    sort: str | None = Field(
        default=None,
        description="Optional sort clause recognized by the USPTO API.",
    )


class SaveQueryRequest(BaseModel):
    """Persist a saved query inside a workspace."""

    workspace_id: str = Field(
        ...,
        pattern=WORKSPACE_ID_PATTERN.pattern,
        description=(
            "Workspace identifier that will own the saved query. "
            "Format: 'ws_' prefix followed by exactly 12 lowercase hex characters "
            "(e.g., 'ws_abc123def456'). Must be obtained from uspto_workspaces_create "
            "or uspto_workspaces_list."
        ),
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Label for the saved query (1-200 characters).",
    )
    query: str = Field(
        ...,
        description="USPTO query string saved for repeat execution.",
    )
    filters: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional filter constraints as key-value pairs. "
            "VALID KEYS: 'applicationType' (values: 'utility', 'design', 'plant', 'reissue'), "
            "'applicationStatusCategory' (values: 'pending', 'patented', 'abandoned'), "
            "'entityStatus' (values: 'small', 'micro', 'large'), "
            "'filingDateFrom' (YYYY-MM-DD), 'filingDateTo' (YYYY-MM-DD). "
            "EXAMPLE: {'applicationType': 'utility', 'filingDateFrom': '2020-01-01'}"
        ),
    )
    pinned_results: list[str] | None = Field(
        default=None,
        description="Optional pinned application numbers for quick access.",
    )
    notes: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional workspace notes for the saved query (max 2000 chars).",
    )


class GetQueryRequest(BaseModel):
    """Fetch a single saved query definition."""

    query_id: str = Field(
        ...,
        pattern=QUERY_ID_PATTERN.pattern,
        description=(
            "Query identifier from uspto_queries_save response. "
            "Format: 'qry_' prefix followed by exactly 8 lowercase hex characters "
            "(e.g., 'qry_a1b2c3d4'). Do not construct manually."
        ),
    )


class RunQueryRequest(BaseModel):
    """Run an existing saved query by its identifier."""

    query_id: str = Field(
        ...,
        pattern=QUERY_ID_PATTERN.pattern,
        description=(
            "Query identifier from uspto_queries_save response. "
            "Format: 'qry_' prefix followed by exactly 8 lowercase hex characters "
            "(e.g., 'qry_a1b2c3d4'). Do not construct manually."
        ),
    )
    start: int = Field(
        default=0,
        ge=0,
        description="Zero-based offset into the query results.",
    )
    rows: int = Field(
        default=25,
        ge=1,
        le=100,
        description="Page size for re-running the saved query (1-100).",
    )


class ApplicantInfo(BaseModel):
    """Applicant metadata returned with search and snapshot payloads."""

    name: str | None = Field(
        default=None,
        description="Applicant name as returned by the USPTO.",
    )
    role: str | None = Field(
        default=None,
        description="Role or relationship of the applicant.",
    )
    country: str | None = Field(
        default=None,
        description="Country code or name, if provided.",
    )
    organization: str | None = Field(
        default=None,
        description="Organization associated with the applicant.",
    )


class ApplicationSearchResult(BaseModel):
    """USPTO application metadata delivered in search results."""

    application_number_text: str | None = Field(
        default=None,
        description="Application number (format varies by application type).",
    )
    invention_title: str | None = Field(
        default=None,
        description="Title of the invention if available.",
    )
    application_type: str | None = Field(
        default=None,
        description="Type of application returned by the USPTO.",
    )
    filing_date: str | None = Field(
        default=None,
        description=f"Filing date in ISO 8601 ({ISO8601_HINT}).",
    )
    publication_date: str | None = Field(
        default=None,
        description=f"Publication date in ISO 8601 ({ISO8601_HINT}).",
    )
    publication_number: str | None = Field(
        default=None,
        description="Publication number associated with the application.",
    )
    application_status_code: str | int | None = Field(
        default=None,
        description="Raw USPTO status code assigned to the application.",
    )
    application_status_description_text: str | None = Field(
        default=None,
        description="Status description returned by the USPTO search API.",
    )
    patent_number: str | None = Field(
        default=None,
        description="Issued patent number if the application matured.",
    )
    patent_issue_date: str | None = Field(
        default=None,
        description=f"Patent issue date in ISO 8601 ({ISO8601_HINT}).",
    )
    first_named_applicant: ApplicantInfo | None = Field(
        default=None,
        description="Primary applicant metadata when available.",
    )
    assignee_entity_name: str | None = Field(
        default=None,
        description="Assignee entity name provided by the USPTO.",
    )
    priority_claims: list[dict[str, str]] | None = Field(
        default=None,
        description=(
            "Foreign priority claims under the Paris Convention. "
            "Online: filingDate, applicationNumberText, ipOfficeName. "
            "Offline: date, doc_number, country."
        ),
    )
    parent_continuity: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Parent applications and provisional applications. Matches USPTO API "
            "parentContinuityBag structure. Includes continuations, continuation-in-parts, "
            "divisions, reissues, and provisional applications."
        ),
    )
    child_continuity: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Child applications (divisionals, continuations filed from this application). "
            "Matches USPTO API childContinuityBag structure."
        ),
    )
    related_application: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Complete related application details when include_application is true. "
            "Present only for grant results that include the linked application."
        ),
    )

    @field_validator(
        "filing_date",
        "publication_date",
        "patent_issue_date",
        mode="before",
    )
    def _validate_application_dates(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _validate_iso8601(value, info.field_name)


class SearchMetadata(BaseModel):
    """Execution metadata returned with search results."""

    query_text: str = Field(
        ...,
        description="Normalized query text used for the search.",
    )
    retrieved_at: str = Field(
        ...,
        description=f"Timestamp when the search completed ({ISO8601_HINT}).",
    )
    execution_time_ms: int | None = Field(
        default=None,
        ge=0,
        description="Upstream execution time in milliseconds if provided.",
    )
    result_count: int = Field(
        ...,
        ge=0,
        description="Number of results returned in this page.",
    )
    cursor: str | None = Field(
        default=None,
        description="Cursor to retrieve the next page of results.",
    )
    dataset_coverage: str | None = Field(
        default=None,
        description="Dataset coverage notes from the USPTO response.",
    )
    filters_applied: dict[str, Any] | None = Field(
        default=None,
        description="Filters that were honored by the upstream search call.",
    )

    @field_validator("retrieved_at", mode="before")
    def _validate_search_timestamp(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _validate_iso8601(value, info.field_name)


class SearchResultsResponse(BaseModel):
    """Search results plus pagination metadata."""

    query_id: str = Field(
        ...,
        pattern=QUERY_ID_PATTERN.pattern,
        description=(
            "Temporary query identifier for pagination. "
            "Format: 'qry_' prefix followed by exactly 8 lowercase hex characters "
            "(e.g., 'qry_a1b2c3d4')."
        ),
    )
    results: list[ApplicationSearchResult] = Field(
        default_factory=list,
        description="Applications returned by the search request.",
    )
    pagination: PaginationMeta = Field(
        ...,
        description="Offset-based pagination metadata for the search call.",
    )
    metadata: SearchMetadata = Field(
        ...,
        description="Execution metadata for the current search request.",
    )


class SavedQueryResponse(BaseModel):
    """Saved query payload returned from the workspace."""

    query_id: str = Field(
        ...,
        pattern=QUERY_ID_PATTERN.pattern,
        description=(
            "Query identifier. Format: 'qry_' prefix followed by exactly 8 "
            "lowercase hex characters (e.g., 'qry_a1b2c3d4')."
        ),
    )
    workspace_id: str = Field(
        ...,
        pattern=WORKSPACE_ID_PATTERN.pattern,
        description=(
            "Owning workspace identifier (session-scoped). Format: 'ws_' prefix "
            "followed by exactly 12 lowercase hex characters."
        ),
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Human-friendly name of the saved query.",
    )
    query: str = Field(
        ...,
        description="USPTO query text that was persisted.",
    )
    filters: dict[str, Any] | None = Field(
        default=None,
        description="Filters stored with the saved query.",
    )
    pinned_results: list[str] = Field(
        default_factory=list,
        description="Application numbers pinned to the saved query.",
    )
    notes: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional annotations stored with the query.",
    )
    created_at: str = Field(
        ...,
        description=f"Timestamp when the query was saved ({ISO8601_HINT}).",
    )
    last_run_at: str | None = Field(
        default=None,
        description=f"Most recent execution timestamp ({ISO8601_HINT}).",
    )
    run_count: int = Field(
        default=0,
        ge=0,
        description="Number of times this query has been executed.",
    )

    @field_validator("created_at", "last_run_at", mode="before")
    def _validate_saved_query_timestamps(
        cls, value: str | None, info: ValidationInfo
    ) -> str | None:
        return _validate_iso8601(value, info.field_name)


# Snapshot Models
class CreateSnapshotRequest(BaseModel):
    """Capture a snapshot of an application for a workspace."""

    workspace_id: str = Field(
        ...,
        pattern=WORKSPACE_ID_PATTERN.pattern,
        description=(
            "Workspace identifier that will own the snapshot. "
            "Format: 'ws_' prefix followed by exactly 12 lowercase hex characters "
            "(e.g., 'ws_abc123def456'). Must be obtained from uspto_workspaces_create."
        ),
    )
    application_number_text: str = Field(
        ...,
        pattern=APPLICATION_NUMBER_PATTERN.pattern,
        description=(
            "USPTO application number in strict formatted form: NN/NNN,NNN "
            "(2 digits, slash, 3 digits, comma, 3 digits). "
            "Example: '16/123,456'. Digits-only format is NOT accepted."
        ),
    )
    auto_normalize_status: bool = Field(
        default=True,
        description="Normalize status codes automatically when True.",
    )


class GetSnapshotRequest(BaseModel):
    """Retrieve a specific snapshot by application and version."""

    workspace_id: str = Field(
        ...,
        pattern=WORKSPACE_ID_PATTERN.pattern,
        description=(
            "Workspace identifier owning the snapshot. "
            "Format: 'ws_' prefix followed by exactly 12 lowercase hex characters "
            "(e.g., 'ws_abc123def456')."
        ),
    )
    application_number_text: str = Field(
        ...,
        pattern=APPLICATION_NUMBER_PATTERN.pattern,
        description=(
            "USPTO application number in strict formatted form: NN/NNN,NNN "
            "(2 digits, slash, 3 digits, comma, 3 digits). "
            "Example: '16/123,456'. Digits-only format is NOT accepted."
        ),
    )
    version: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Specific snapshot version to retrieve (1-indexed). "
            "When null/omitted, returns the most recent snapshot. "
            "Version numbers start at 1 and increment each time a new snapshot "
            "is created for the same application_number_text in the workspace."
        ),
    )


class ListSnapshotsRequest(BaseModel):
    """List snapshots created within a workspace."""

    workspace_id: str = Field(
        ...,
        pattern=WORKSPACE_ID_PATTERN.pattern,
        description=(
            "Workspace identifier whose snapshots should be listed. "
            "Format: 'ws_' prefix followed by exactly 12 lowercase hex characters "
            "(e.g., 'ws_abc123def456')."
        ),
    )
    cursor: str | None = Field(
        default=None,
        description=(
            "Pagination cursor for retrieving the next page of results. "
            "IMPORTANT: Do not construct this value manually - always use the exact "
            "'next_cursor' string from a previous response. Pass null or omit to "
            "start from the first page. Invalid cursors return INVALID_CURSOR error."
        ),
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Maximum snapshots to return per page (1-200).",
    )


class BibliographicData(BaseModel):
    """Bibliographic metadata captured in a snapshot."""

    invention_title: str | None = Field(
        default=None,
        description="Title of the invention if available.",
    )
    filing_date: str | None = Field(
        default=None,
        description=f"Filing date in ISO 8601 ({ISO8601_HINT}).",
    )
    publication_date: str | None = Field(
        default=None,
        description=f"Publication date in ISO 8601 ({ISO8601_HINT}).",
    )
    publication_number: str | None = Field(
        default=None,
        description="Publication number extracted from USPTO data.",
    )
    patent_number: str | None = Field(
        default=None,
        description="Issued patent number if one exists.",
    )
    patent_issue_date: str | None = Field(
        default=None,
        description=f"Patent issue date in ISO 8601 ({ISO8601_HINT}).",
    )
    first_named_applicant: ApplicantInfo | None = Field(
        default=None,
        description="Applicant metadata captured in the snapshot.",
    )
    assignee_entity_name: str | None = Field(
        default=None,
        description="Assignee entity name if assigned.",
    )
    inventor_name_array_text: list[str] = Field(
        default_factory=list,
        description="Formatted inventor names from the snapshot.",
    )
    priority_claims: list[ForeignPriorityClaim] = Field(
        default_factory=list,
        description="Foreign priority claims associated with the application, if available.",
    )

    @field_validator(
        "filing_date",
        "publication_date",
        "patent_issue_date",
        mode="before",
    )
    def _validate_bibliographic_dates(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _validate_iso8601(value, info.field_name)


class StatusData(BaseModel):
    """Normalized status information for a snapshot."""

    raw_code: str | None = Field(
        default=None,
        description="Source USPTO status code before normalization.",
    )
    normalized_description: str | None = Field(
        default=None,
        description="Normalized status description provided by the MCP.",
    )
    normalized_at: str | None = Field(
        default=None,
        description=f"Timestamp when the status was normalized ({ISO8601_HINT}).",
    )
    status_code_version: str | None = Field(
        default=None,
        description="Version identifier for the status reference data.",
    )

    @field_validator("normalized_at", mode="before")
    def _validate_normalized_at(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _validate_iso8601(value, info.field_name)


class ProsecutionEvent(BaseModel):
    """A prosecution event captured inside a snapshot."""

    event_code: str = Field(
        ...,
        description="USPTO prosecution event code.",
    )
    event_date: str | None = Field(
        default=None,
        description=f"Event date in ISO 8601 ({ISO8601_HINT}).",
    )
    description: str | None = Field(
        default=None,
        description="Human-readable description of the event.",
    )
    document_reference: str | None = Field(
        default=None,
        description="Document identifier tied to the event when available.",
    )

    @field_validator("event_date", mode="before")
    def _validate_event_date(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _validate_iso8601(value, info.field_name)


class ProvenanceData(BaseModel):
    """Provenance information captured for the snapshot."""

    source: str = Field(
        ...,
        description="Source system or API endpoint that provided the data.",
    )
    retrieved_at: str = Field(
        ...,
        description=f"Timestamp when the snapshot was retrieved ({ISO8601_HINT}).",
    )
    retrieved_by: str | None = Field(
        default=None,
        description="Component or process that fetched the snapshot.",
    )

    @field_validator("retrieved_at", mode="before")
    def _validate_provenance_timestamp(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _validate_iso8601(value, info.field_name)


class SnapshotResponse(BaseModel):
    """Snapshot response returned once a snapshot is created or retrieved."""

    snapshot_id: str = Field(
        ...,
        description="Identifier for the immutable snapshot.",
    )
    workspace_id: str = Field(
        ...,
        pattern=WORKSPACE_ID_PATTERN.pattern,
        description=(
            "Owning workspace identifier. Format: 'ws_' prefix followed by "
            "exactly 12 lowercase hex characters (e.g., 'ws_abc123def456')."
        ),
    )
    application_number_text: str = Field(
        ...,
        pattern=APPLICATION_NUMBER_PATTERN.pattern,
        description=("Application number in formatted form: NN/NNN,NNN (e.g., '16/123,456')."),
    )
    version: int = Field(
        ...,
        ge=1,
        description="Version of the snapshot (1-indexed).",
    )
    bibliographic: BibliographicData = Field(
        ...,
        description="Bibliographic metadata captured with this snapshot.",
    )
    status: StatusData = Field(
        ...,
        description="Normalized status details for the capture.",
    )
    events: list[ProsecutionEvent] = Field(
        default_factory=list,
        description="Ordered prosecution events tied to the snapshot.",
    )
    provenance: ProvenanceData = Field(
        ...,
        description="Provenance metadata describing this snapshot.",
    )
    created_at: str = Field(
        ...,
        description=f"Snapshot creation timestamp in ISO 8601 ({ISO8601_HINT}).",
    )

    @field_validator("created_at", mode="before")
    def _validate_snapshot_created_at(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _validate_iso8601(value, info.field_name)


class ListSnapshotsResponse(BaseModel):
    """Paginated response listing application snapshots."""

    snapshots: list[SnapshotResponse] = Field(
        default_factory=list,
        description="Snapshots returned for the current page.",
    )
    pagination: PaginationResponse = Field(
        ...,
        description="Cursor metadata for fetching the next page.",
    )


# Document Models
class ListDocumentsRequest(BaseModel):
    """List prosecution documents for a given application."""

    application_number_text: str = Field(
        ...,
        pattern=APPLICATION_NUMBER_PATTERN.pattern,
        description=(
            "USPTO application number in strict formatted form: NN/NNN,NNN "
            "(2 digits, slash, 3 digits, comma, 3 digits). "
            "Example: '16/123,456'. Digits-only format is NOT accepted."
        ),
    )
    start: int = Field(
        default=0,
        ge=0,
        description="Zero-based offset into the document list.",
    )
    rows: int = Field(
        default=100,
        ge=1,
        le=500,
        description=(
            "Documents per page. Default: 100. Range: 1-500. "
            "Larger values increase response time and payload size."
        ),
    )


class GetDownloadUrlRequest(BaseModel):
    """Request a download URL for a specific prosecution document."""

    application_number_text: str = Field(
        ...,
        pattern=APPLICATION_NUMBER_PATTERN.pattern,
        description=(
            "USPTO application number in strict formatted form: NN/NNN,NNN "
            "(2 digits, slash, 3 digits, comma, 3 digits). "
            "Example: '16/123,456'. Digits-only format is NOT accepted."
        ),
    )
    document_identifier: str = Field(
        ...,
        description=(
            "USPTO identifier for the specific document. Obtain from uspto_documents_list response."
        ),
    )
    preferred_mime_type: str = Field(
        default="application/pdf",
        description=(
            "MIME type for the document download. "
            "OPTIONS: 'application/pdf' (default, most common), 'image/tiff' (legacy/scanned). "
            "If the preferred type is unavailable for a specific document, "
            "returns HTTP 422 with 'availableMimeTypes' listing valid options."
        ),
    )


class GetDownloadUrlResponse(BaseModel):
    """Response containing the preferred download URL for a document."""

    document_identifier: str = Field(
        ...,
        description="USPTO identifier for the document.",
    )
    download_url: str = Field(
        ...,
        description="Pre-signed download URL for the document.",
    )
    mime_type_identifier: str = Field(
        ...,
        description="MIME type identifier for the selected download option.",
    )
    page_count: int | None = Field(
        default=None,
        ge=0,
        description="Page count of the document if reported.",
    )
    file_size_bytes: int | None = Field(
        default=None,
        ge=0,
        description="Byte size of the document if available.",
    )
    metadata: dict[str, str] = Field(
        ...,
        description="Metadata about the URL generation and expiry warnings.",
    )


class DownloadOption(BaseModel):
    """A downloadable option for a document."""

    mime_type_identifier: str = Field(
        ...,
        description="MIME type identifier such as application/pdf.",
    )
    download_url: str = Field(
        ...,
        description="Pre-signed download URL returned by the USPTO.",
    )
    page_count: int | None = Field(
        default=None,
        ge=0,
        description="Page count of the document if reported.",
    )
    file_size_bytes: int | None = Field(
        default=None,
        ge=0,
        description="Byte size of the document if available.",
    )


class DocumentRecord(BaseModel):
    """Metadata for an individual prosecution document."""

    document_identifier: str = Field(
        ...,
        description="Unique identifier assigned by the USPTO.",
    )
    document_code: str | None = Field(
        default=None,
        description="Document code returned by the USPTO.",
    )
    document_code_description_text: str | None = Field(
        default=None,
        description="Description for the document code.",
    )
    official_date: str | None = Field(
        default=None,
        description=f"Official document date in ISO 8601 ({ISO8601_HINT}).",
    )
    direction_category: Literal["INCOMING", "OUTGOING", "INTERNAL"] | None = Field(
        default=None,
        description="Direction category assigned by the USPTO (INCOMING/OUTGOING/INTERNAL).",
    )
    mail_room_date: str | None = Field(
        default=None,
        description=f"Mail room date in ISO 8601 ({ISO8601_HINT}).",
    )
    download_options: list[DownloadOption] = Field(
        default_factory=list,
        description="Available download options for the document.",
    )

    @field_validator("official_date", "mail_room_date", mode="before")
    def _validate_document_dates(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _validate_iso8601(value, info.field_name)


class DocumentsListResponse(BaseModel):
    """Document inventory response for an application."""

    application_number_text: str = Field(
        ...,
        pattern=APPLICATION_NUMBER_PATTERN.pattern,
        description=("Application number in formatted form: NN/NNN,NNN (e.g., '16/123,456')."),
    )
    documents: list[DocumentRecord] = Field(
        default_factory=list,
        description="Documents returned for the application.",
    )
    pagination: PaginationMeta = Field(
        ...,
        description="Pagination metadata for the document list.",
    )
    metadata: RetrievalMetadata = Field(
        ...,
        description="Retrieval metadata describing the document call.",
    )


# Foreign Priority Models
class GetForeignPriorityRequest(BaseModel):
    """Fetch foreign priority claims for an application."""

    application_number_text: str = Field(
        ...,
        pattern=APPLICATION_NUMBER_PATTERN.pattern,
        description=(
            "USPTO application number in strict formatted form: NN/NNN,NNN "
            "(2 digits, slash, 3 digits, comma, 3 digits). "
            "Example: '16/123,456'. Digits-only format is NOT accepted."
        ),
    )


class ForeignPriorityClaim(BaseModel):
    """Single foreign priority claim details."""

    foreign_application_number: str | None = Field(
        default=None,
        description="Foreign application number per the claim.",
    )
    foreign_filing_date: str | None = Field(
        default=None,
        description=f"Foreign filing date in ISO 8601 ({ISO8601_HINT}).",
    )
    ip_office_code: str | None = Field(
        default=None,
        description="Code for the foreign intellectual property office.",
    )
    ip_office_name: str | None = Field(
        default=None,
        description="Full name of the foreign IP office.",
    )
    priority_claim_indicator: str | None = Field(
        default=None,
        description="Indicator describing the priority claim status.",
    )

    @field_validator("foreign_filing_date", mode="before")
    def _validate_foreign_filing_date(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _validate_iso8601(value, info.field_name)


class ForeignPriorityMetadata(BaseModel):
    """Metadata describing a foreign priority retrieval call."""

    retrieved_at: str = Field(
        ...,
        description=f"Timestamp when the data was retrieved ({ISO8601_HINT}).",
    )
    total_claims: int = Field(
        ...,
        ge=0,
        description="Total number of foreign priority claims returned.",
    )
    execution_time_ms: int | None = Field(
        default=None,
        ge=0,
        description="Execution time reported by the upstream call in milliseconds.",
    )
    dataset_coverage: str | None = Field(
        default=None,
        description="Dataset coverage notes reported by the upstream call.",
    )

    @field_validator("retrieved_at", mode="before")
    def _validate_retrieval_timestamp(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _validate_iso8601(value, info.field_name)


class ForeignPriorityResponse(BaseModel):
    """Foreign priority claims returned for an application."""

    application_number_text: str = Field(
        ...,
        pattern=APPLICATION_NUMBER_PATTERN.pattern,
        description=("Application number in formatted form: NN/NNN,NNN (e.g., '16/123,456')."),
    )
    foreign_priority_claims: list[ForeignPriorityClaim] = Field(
        default_factory=list,
        description="Claims returned by the foreign priority lookup.",
    )
    metadata: ForeignPriorityMetadata = Field(
        ...,
        description="Retrieval metadata for the foreign priority call.",
    )


# Patent PDF Models
class GeneratePatentPdfRequest(BaseModel):
    """Generate a text-only patent PDF from offline database content."""

    application_number: str = Field(
        ...,
        pattern=APPLICATION_NUMBER_LOOSE_PATTERN.pattern,
        description=(
            "USPTO application number in either formatted form 'NN/NNN,NNN' "
            "(e.g., '16/123,456') or digits-only form (e.g., '16123456'). "
            "Both formats are equivalent and accepted."
        ),
    )


class GeneratePatentPdfResponse(BaseModel):
    """Generated patent PDF content with metadata."""

    application_number: str = Field(
        ...,
        description="Application number used to generate the PDF.",
    )
    generated_at: str = Field(
        ...,
        description=f"Generation timestamp in ISO 8601 ({ISO8601_HINT}).",
    )
    content_type: str = Field(
        ...,
        description="MIME type for the generated PDF.",
    )
    file_name: str = Field(
        ...,
        description="Suggested filename for the generated PDF.",
    )
    text_only: bool = Field(
        ...,
        description="True when drawings and images are omitted.",
    )
    byte_size: int = Field(
        ...,
        ge=0,
        description="Size of the PDF payload in bytes.",
    )
    note: str | None = Field(
        default=None,
        description="Additional notes about the PDF content.",
    )
    pdf_bytes: str = Field(
        ...,
        description="Base64-encoded PDF bytes.",
    )

    @field_validator("generated_at", mode="before")
    def _validate_pdf_timestamp(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _validate_iso8601(value, info.field_name)


# Patent Retrieval Models (Full Text Access)
class GetPatentRequest(BaseModel):
    """Request to retrieve complete patent details including full text."""

    application_number: str = Field(
        ...,
        pattern=APPLICATION_NUMBER_LOOSE_PATTERN.pattern,
        description="Application number in formatted (e.g., 16/123,456) or digits-only form.",
    )


class InventorInfo(BaseModel):
    """Inventor details for a patent."""

    first_name: str | None = Field(default=None, description="Inventor first name.")
    last_name: str | None = Field(default=None, description="Inventor last name.")
    full_name: str | None = Field(default=None, description="Inventor full name.")
    city: str | None = Field(default=None, description="Inventor city.")
    state: str | None = Field(default=None, description="Inventor state/province.")
    country: str | None = Field(default=None, description="Inventor country.")
    sequence: int | None = Field(default=None, description="Inventor sequence number.")


class AssigneeInfo(BaseModel):
    """Assignee details for a patent."""

    name: str | None = Field(default=None, description="Assignee name.")
    role: str | None = Field(default=None, description="Assignee role.")
    city: str | None = Field(default=None, description="Assignee city.")
    state: str | None = Field(default=None, description="Assignee state/province.")
    country: str | None = Field(default=None, description="Assignee country.")


class CPCClassification(BaseModel):
    """CPC classification code for a patent."""

    section: str | None = Field(default=None, description="CPC section (e.g., A, B, C).")
    class_: str | None = Field(default=None, alias="class", description="CPC class.")
    subclass: str | None = Field(default=None, description="CPC subclass.")
    main_group: str | None = Field(default=None, description="CPC main group.")
    sub_group: str | None = Field(default=None, description="CPC subgroup.")
    is_main: bool | None = Field(default=None, description="Whether this is the main CPC.")


class PatentCitation(BaseModel):
    """Patent citation reference."""

    cited_patent_number: str | None = Field(default=None, description="Cited patent number.")
    cited_country: str | None = Field(default=None, description="Country of cited patent.")
    cited_kind: str | None = Field(default=None, description="Kind code of cited patent.")
    cited_date: str | None = Field(default=None, description="Date of cited patent.")
    category: str | None = Field(default=None, description="Citation category.")


class ExaminerInfo(BaseModel):
    """Patent examiner information."""

    first_name: str | None = Field(default=None, description="Examiner first name.")
    last_name: str | None = Field(default=None, description="Examiner last name.")
    department: str | None = Field(default=None, description="Examiner department/art unit.")


class GetPatentResponse(BaseModel):
    """Complete patent details including full text content.

    This response includes the full patent specification text needed for substantive
    analysis and comparison, unlike search results which only contain metadata.
    """

    # Identifiers
    application_number: str = Field(
        ...,
        description="USPTO application number.",
    )
    patent_number: str | None = Field(
        default=None,
        description="Issued patent number (for granted patents).",
    )
    publication_number: str | None = Field(
        default=None,
        description="Publication number.",
    )

    # Core bibliographic data
    title: str | None = Field(
        default=None,
        description="Invention title.",
    )
    application_type: str | None = Field(
        default=None,
        description="Type of application (utility, design, plant).",
    )
    document_type: str | None = Field(
        default=None,
        description="Document type (application or grant).",
    )
    kind_code: str | None = Field(
        default=None,
        description="Kind code (e.g., A1, B2, S1).",
    )
    country: str = Field(
        default="US",
        description="Country code.",
    )

    # Key dates
    filing_date: str | None = Field(
        default=None,
        description=f"Application filing date ({ISO8601_HINT}).",
    )
    publication_date: str | None = Field(
        default=None,
        description=f"Publication date ({ISO8601_HINT}).",
    )
    issue_date: str | None = Field(
        default=None,
        description=f"Patent issue date for grants ({ISO8601_HINT}).",
    )

    @field_validator(
        "filing_date",
        "publication_date",
        "issue_date",
        mode="before",
    )
    def _validate_patent_dates(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _validate_iso8601(value, info.field_name)

    # FULL TEXT CONTENT - The key data for substantive analysis
    abstract: str | None = Field(
        default=None,
        description="Patent abstract text summarizing the invention.",
    )
    description: str | None = Field(
        default=None,
        description="Full specification/description text of the patent.",
    )
    claims: str | None = Field(
        default=None,
        description="Patent claims text defining the scope of protection.",
    )

    # Parties
    inventors: list[dict[str, Any]] | None = Field(
        default=None,
        description="List of inventors with contact information.",
    )
    assignees: list[dict[str, Any]] | None = Field(
        default=None,
        description="List of assignees (patent owners).",
    )
    applicants: list[dict[str, Any]] | None = Field(
        default=None,
        description="List of applicants.",
    )
    first_inventor_name: str | None = Field(
        default=None,
        description="Name of the first listed inventor.",
    )
    assignee_entity_name: str | None = Field(
        default=None,
        description="Name of the primary assignee.",
    )

    # Examiner information
    examiner_name: str | None = Field(
        default=None,
        description="Primary examiner name.",
    )
    group_art_unit: str | None = Field(
        default=None,
        description="USPTO Art Unit handling the application.",
    )
    primary_examiner: dict[str, Any] | None = Field(
        default=None,
        description="Primary examiner details.",
    )
    assistant_examiner: dict[str, Any] | None = Field(
        default=None,
        description="Assistant examiner details.",
    )

    # Classifications
    cpc_classifications: list[dict[str, Any]] | None = Field(
        default=None,
        description="CPC (Cooperative Patent Classification) codes.",
    )
    ipc_codes: list[dict[str, Any]] | None = Field(
        default=None,
        description="IPC (International Patent Classification) codes.",
    )
    uspc_class: str | None = Field(
        default=None,
        description="US Patent Classification class.",
    )
    uspc_subclass: str | None = Field(
        default=None,
        description="US Patent Classification subclass.",
    )

    # Citations
    patent_citations: list[dict[str, Any]] | None = Field(
        default=None,
        description="Patents cited by this patent.",
    )
    npl_citations: list[dict[str, Any]] | None = Field(
        default=None,
        description="Non-patent literature citations.",
    )

    # Related applications
    foreign_priority_claims: list[dict[str, Any]] | None = Field(
        default=None,
        description="Foreign priority claims under Paris Convention.",
    )
    parent_continuity: list[dict[str, Any]] | None = Field(
        default=None,
        description="Parent applications (continuations, divisionals, etc.).",
    )
    child_continuity: list[dict[str, Any]] | None = Field(
        default=None,
        description="Child applications claiming priority to this application.",
    )

    # Grant-specific metadata
    number_of_claims: int | None = Field(
        default=None,
        description="Total number of claims.",
    )
    number_of_figures: int | None = Field(
        default=None,
        description="Number of figures in the patent.",
    )

    # Status (when available, primarily online mode)
    application_status_code: str | int | None = Field(
        default=None,
        description="USPTO application status code.",
    )
    application_status_description: str | None = Field(
        default=None,
        description="Human-readable status description.",
    )


# Bundle & Audit Models
class BundleExportOptions(BaseModel):
    """Optional toggles controlling bundle exports."""

    include_documents: bool = Field(
        default=True,
        description="Include document metadata in the exported bundle.",
    )
    include_foreign_priority: bool = Field(
        default=True,
        description="Include foreign priority claims if they exist.",
    )
    include_audit_trail: bool = Field(
        default=True,
        description="Include audit trail entries with the bundle.",
    )


class ExportBundleRequest(BaseModel):
    """Export a research bundle for a workspace."""

    workspace_id: str = Field(
        ...,
        pattern=WORKSPACE_ID_PATTERN.pattern,
        description=(
            "Workspace identifier whose assets are exported. "
            "Format: 'ws_' prefix followed by exactly 12 lowercase hex characters "
            "(e.g., 'ws_abc123def456')."
        ),
    )
    bundle_name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Friendly name for the exported bundle.",
    )
    include_queries: list[str] | None = Field(
        default=None,
        description=(
            "Controls which saved queries to include in the bundle. "
            "THREE MODES: "
            "(1) null/omit = include ALL queries in workspace; "
            "(2) empty array [] = include NO queries; "
            "(3) array of IDs ['qry_xxx', 'qry_yyy'] = include ONLY those specific queries. "
            "TIP: Use mode 2 or 3 to reduce bundle size when queries aren't needed."
        ),
    )
    include_applications: list[str] | None = Field(
        default=None,
        description=(
            "Controls which application snapshots to include in the bundle. "
            "THREE MODES: "
            "(1) null/omit = include ALL applications in workspace; "
            "(2) empty array [] = include NO applications; "
            "(3) array of numbers ['16/123,456', '17/234,567'] = include ONLY those applications. "
            "TIP: Use mode 2 or 3 to reduce bundle size when not all applications are needed."
        ),
    )
    options: BundleExportOptions | None = Field(
        default=None,
        description="Optional export feature flags.",
    )


class GetAuditHistoryRequest(BaseModel):
    """Retrieve audit entries for a workspace."""

    workspace_id: str = Field(
        ...,
        pattern=WORKSPACE_ID_PATTERN.pattern,
        description=(
            "Workspace identifier whose audit history is requested. "
            "Format: 'ws_' prefix followed by exactly 12 lowercase hex characters "
            "(e.g., 'ws_abc123def456')."
        ),
    )
    start_date: str | None = Field(
        default=None,
        description=(
            f"ISO 8601 start timestamp filter ({ISO8601_HINT}). "
            "Date-only values like '2024-01-15' are also accepted."
        ),
    )
    end_date: str | None = Field(
        default=None,
        description=(
            f"ISO 8601 end timestamp filter ({ISO8601_HINT}). "
            "Date-only values like '2024-01-15' are also accepted."
        ),
    )
    cursor: str | None = Field(
        default=None,
        description=(
            "Pagination cursor for retrieving the next page of results. "
            "IMPORTANT: Do not construct this value manually - always use the exact "
            "'next_cursor' string from a previous response. Pass null or omit to "
            "start from the first page. Invalid cursors return INVALID_CURSOR error."
        ),
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=500,
        description="Maximum audit entries per page (1-500).",
    )

    @field_validator("start_date", "end_date", mode="before")
    def _validate_audit_timestamps(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _validate_iso8601(value, info.field_name)


class BundleApplication(BaseModel):
    """Application-level metadata included in an export bundle."""

    application_number_text: str = Field(
        ...,
        pattern=APPLICATION_NUMBER_PATTERN.pattern,
        description=("Application number in formatted form: NN/NNN,NNN (e.g., '16/123,456')."),
    )
    snapshot_version: int = Field(
        ...,
        ge=1,
        description="Snapshot version that was exported for this application.",
    )
    documents_included: list[str] = Field(
        default_factory=list,
        description="Document identifiers included for this application.",
    )
    foreign_priority_included: bool = Field(
        ...,
        description="Indicates whether foreign priority data was included.",
    )
    notes: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional notes for the exported application.",
    )


class AuditEntry(BaseModel):
    """Single audit action recorded within a session."""

    audit_id: str | None = Field(
        default=None,
        description="Unique identifier for the audit entry.",
    )
    timestamp: str = Field(
        ...,
        description=f"Timestamp of the action in ISO 8601 ({ISO8601_HINT}).",
    )
    action: str = Field(
        ...,
        description="Action label describing the audited event.",
    )
    resource_type: str = Field(
        ...,
        description="Type of resource that was affected.",
    )
    resource_id: str | None = Field(
        default=None,
        description="Identifier of the affected resource, if available.",
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional diagnostic data captured for the audit entry.",
    )
    description: str | None = Field(
        default=None,
        description="Optional narrative description of the action.",
    )

    @field_validator("timestamp", mode="before")
    def _validate_audit_timestamp(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _validate_iso8601(value, info.field_name)


class BundleMetadata(BaseModel):
    """Metadata summarizing a bundle export."""

    exported_at: str = Field(
        ...,
        description=f"Export timestamp in ISO 8601 ({ISO8601_HINT}).",
    )
    application_count: int = Field(
        ...,
        ge=0,
        description="Count of applications included in the bundle.",
    )
    document_count: int = Field(
        ...,
        ge=0,
        description="Count of documents included in the bundle.",
    )
    bundle_size_bytes: int | None = Field(
        default=None,
        ge=0,
        description="Optional size estimate for the exported bundle in bytes.",
    )
    notes: str | None = Field(
        default=None,
        description="Optional notes describing the bundle export.",
    )

    @field_validator("exported_at", mode="before")
    def _validate_exported_at(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _validate_iso8601(value, info.field_name)


class BundleExportResponse(BaseModel):
    """Response describing the completed bundle export."""

    bundle_id: str = Field(
        ...,
        description="Identifier assigned to the exported bundle.",
    )
    bundle_name: str = Field(
        ...,
        description="Name supplied for the bundle export.",
    )
    workspace_id: str = Field(
        ...,
        pattern=WORKSPACE_ID_PATTERN.pattern,
        description=(
            "Workspace identifier that produced the bundle. "
            "Format: 'ws_' prefix followed by exactly 12 lowercase hex characters."
        ),
    )
    queries: list[SavedQueryResponse] = Field(
        default_factory=list,
        description="Saved queries included inside the bundle.",
    )
    applications: list[BundleApplication] = Field(
        default_factory=list,
        description="Applications packaged in the bundle export.",
    )
    audit_trail: list[AuditEntry] = Field(
        default_factory=list,
        description="Audit entries captured with the bundle export.",
    )
    metadata: BundleMetadata = Field(
        ...,
        description="Metadata describing the bundle export operation.",
    )


class AuditHistoryResponse(BaseModel):
    """Paginated audit history results for a workspace."""

    workspace_id: str = Field(
        ...,
        pattern=WORKSPACE_ID_PATTERN.pattern,
        description=(
            "Workspace identifier whose audit trail is being returned. "
            "Format: 'ws_' prefix followed by exactly 12 lowercase hex characters."
        ),
    )
    audit_entries: list[AuditEntry] = Field(
        default_factory=list,
        description="Audit entries recorded for the workspace.",
    )
    pagination: PaginationResponse = Field(
        ...,
        description="Cursor metadata for the audit history pagination.",
    )


# Status Codes Models
class StatusCodesRequest(BaseModel):
    """Request model for fetching USPTO status codes (no required parameters)."""

    pass


class StatusCode(BaseModel):
    """Individual USPTO application status code."""

    status_code: str = Field(
        ...,
        description="USPTO status code (e.g., '150').",
    )
    status_description_text: str = Field(
        ...,
        description="Human-readable description of the status code.",
    )


class StatusCodesMetadata(BaseModel):
    """Metadata for the status codes retrieval."""

    retrieved_at: str = Field(
        ...,
        description=f"Timestamp when the status codes were retrieved ({ISO8601_HINT}).",
    )
    version: str = Field(
        ...,
        description="Version or date of the status code data.",
    )
    total_codes: int = Field(
        ...,
        ge=0,
        description="Total number of status codes returned.",
    )
    cache_hit: bool = Field(
        ...,
        description="True if the response came from session cache.",
    )

    @field_validator("retrieved_at", mode="before")
    def _validate_retrieved_at(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _validate_iso8601(value, info.field_name)


class StatusCodesResponse(BaseModel):
    """Response model for fetching USPTO status codes."""

    status_codes: list[StatusCode] = Field(
        ...,
        description="List of USPTO application status codes.",
    )
    metadata: StatusCodesMetadata = Field(
        ...,
        description="Metadata about the status codes retrieval.",
    )


class StatusNormalizeRequest(BaseModel):
    """Request model for normalizing snapshot status codes."""

    workspace_id: str = Field(
        ...,
        pattern=WORKSPACE_ID_PATTERN.pattern,
        description=(
            "Workspace identifier whose snapshots should be normalized. "
            "Format: 'ws_' prefix followed by exactly 12 lowercase hex characters "
            "(e.g., 'ws_abc123def456')."
        ),
    )
    application_numbers: list[str] = Field(
        ...,
        description=(
            "List of application numbers to normalize. "
            "Minimum: 1, Maximum: 50 per request (validated at runtime). "
            "Each must be in formatted form 'NN/NNN,NNN' (e.g., '16/123,456'). "
            "All applications must have existing snapshots in the workspace."
        ),
    )


class StatusNormalizeEntry(BaseModel):
    """Normalized status details for a snapshot."""

    application_number_text: str = Field(
        ...,
        pattern=APPLICATION_NUMBER_PATTERN.pattern,
        description=("Application number in formatted form: NN/NNN,NNN (e.g., '16/123,456')."),
    )
    raw_code: str | None = Field(
        default=None,
        description="Raw USPTO status code captured in the snapshot.",
    )
    normalized_description: str | None = Field(
        default=None,
        description="Normalized status description derived from the reference table.",
    )
    normalized_at: str | None = Field(
        default=None,
        description=f"Timestamp when normalization occurred ({ISO8601_HINT}).",
    )

    @field_validator("normalized_at", mode="before")
    def _validate_normalized_at(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _validate_iso8601(value, info.field_name)


class StatusNormalizeMetadata(BaseModel):
    """Metadata describing the status normalization run."""

    total_processed: int = Field(
        ...,
        ge=0,
        description="Total number of snapshots normalized.",
    )
    errors: int = Field(
        ...,
        ge=0,
        description="Number of snapshots that could not be normalized.",
    )


class StatusNormalizeResponse(BaseModel):
    """Response model for status normalization."""

    normalized: list[StatusNormalizeEntry] = Field(
        default_factory=list,
        description="Normalized status entries for each application number.",
    )
    status_code_version: str | None = Field(
        default=None,
        description="Version identifier for the status code reference table used.",
    )
    metadata: StatusNormalizeMetadata = Field(
        ...,
        description="Metadata about the normalization batch.",
    )


# Shared Models
class PaginationResponse(BaseModel):
    """Cursor-based pagination metadata."""

    next_cursor: str | None = Field(
        default=None,
        description="Cursor for the next page when more entries exist.",
    )
    has_more: bool = Field(
        ...,
        description="Indicates whether additional pages are available.",
    )


class PaginationMeta(BaseModel):
    """Offset-based pagination metadata."""

    start: int = Field(
        ...,
        ge=0,
        description="Zero-based offset of the current page.",
    )
    rows: int = Field(
        ...,
        ge=0,
        description="Number of rows returned for this page.",
    )
    total_results: int = Field(
        ...,
        ge=0,
        description="Total number of available results.",
    )


class RetrievalMetadata(BaseModel):
    """Metadata describing an upstream retrieval call."""

    retrieved_at: str = Field(
        ...,
        description=f"Timestamp when the data was retrieved ({ISO8601_HINT}).",
    )
    execution_time_ms: int | None = Field(
        default=None,
        ge=0,
        description="Execution time reported by the upstream call in milliseconds.",
    )
    dataset_coverage: str | None = Field(
        default=None,
        description="Dataset coverage notes reported by the upstream call.",
    )

    @field_validator("retrieved_at", mode="before")
    def _validate_retrieval_timestamp(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _validate_iso8601(value, info.field_name)


class ErrorDetail(BaseModel):
    """Structured error information."""

    code: str = Field(..., description="Machine-readable error code.")
    message: str = Field(..., description="Human-readable error message.")
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional details useful for diagnostics.",
    )


class ErrorResponse(BaseModel):
    """Standardized error response."""

    error: ErrorDetail = Field(..., description="Structured error payload.")


class DatabaseStatus(BaseModel):
    """Database connectivity status."""

    connected: bool = Field(
        ...,
        description="True if database connection is active.",
    )
    path: str | None = Field(
        default=None,
        description="Database file path or ':memory:' for in-memory databases.",
    )


class UpstreamAPIStatus(BaseModel):
    """USPTO API availability status."""

    available: bool = Field(
        ...,
        description="True if upstream USPTO API is reachable.",
    )
    reason: str | None = Field(
        default=None,
        description="Reason for unavailability or offline mode status.",
    )


class HealthCheckInput(BaseModel):
    """Request parameters for health check endpoint."""

    pass


class HealthCheckResponse(BaseModel):
    """Server health status including database and upstream API connectivity."""

    status: Literal["healthy", "degraded", "unhealthy"] = Field(
        ...,
        description=(
            "Overall health status: healthy (all systems operational), "
            "degraded (partial functionality), unhealthy (critical failure)."
        ),
    )
    version: str = Field(
        ...,
        description="MCP server version identifier.",
    )
    mode: Literal["online", "offline"] = Field(
        ...,
        description="Server mode: online (live USPTO API calls) or offline (cached data only).",
    )
    database: DatabaseStatus = Field(
        ...,
        description="Database connectivity status.",
    )
    upstream_api: UpstreamAPIStatus = Field(
        ...,
        description="USPTO API availability status.",
    )
    timestamp: str = Field(
        ...,
        description=f"Health check execution timestamp in ISO 8601 ({ISO8601_HINT}).",
    )

    @field_validator("timestamp", mode="before")
    def _validate_health_timestamp(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _validate_iso8601(value, info.field_name)
