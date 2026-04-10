"""Search Pydantic schemas for BambooHR API.

These schemas match the BambooHR API structure for search operations.
"""

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field

# =============================================================================
# Search Employees Schemas
# =============================================================================


class EmployeeSearchFilters(BaseModel):
    """Filters for employee search."""

    department: str | None = Field(None, description="Filter by department")
    status: str | None = Field(None, description="Filter by status (Active, Inactive, Terminated)")
    location: str | None = Field(None, description="Filter by location")
    job_title: str | None = Field(None, alias="jobTitle", description="Filter by job title")

    model_config = {"populate_by_name": True}


class EmployeeSearchResult(BaseModel):
    """A single employee result in search response."""

    id: str = Field(..., description="Employee ID")
    first_name: str | None = Field(None, alias="firstName")
    last_name: str | None = Field(None, alias="lastName")
    department: str | None = Field(None)
    email: str | None = Field(None)
    job_title: str | None = Field(None, alias="jobTitle")
    match_score: float = Field(..., alias="matchScore", description="Relevance score 0-1")

    model_config = {"populate_by_name": True}


class EmployeeSearchResponse(BaseModel):
    """Response for employee search."""

    employees: list[EmployeeSearchResult] = Field(default_factory=list)
    page: int = Field(1)
    page_size: int = Field(20, alias="pageSize")
    total: int = Field(0)

    model_config = {"populate_by_name": True}


# =============================================================================
# Search Time-Off Schemas
# =============================================================================


class TimeOffSearchFilters(BaseModel):
    """Filters for time-off search."""

    status: list[str] | None = Field(
        None,
        description="Statuses to filter by (approved, denied, requested, canceled, superseded)",
    )
    type: str | None = Field(None, description="Time-off type ID")
    start_date: str | None = Field(
        None, alias="startDate", description="Date range start (YYYY-MM-DD)"
    )
    end_date: str | None = Field(None, alias="endDate", description="Date range end (YYYY-MM-DD)")

    model_config = {"populate_by_name": True}


class TimeOffSearchResult(BaseModel):
    """A single time-off request result in search response."""

    id: str = Field(..., description="Request ID")
    employee_id: str = Field(..., alias="employeeId")
    employee_name: str | None = Field(None, alias="employeeName")
    type: str = Field(..., description="Time-off type name")
    start: str = Field(..., description="Start date (YYYY-MM-DD)")
    end: str = Field(..., description="End date (YYYY-MM-DD)")
    status: str = Field(...)
    amount: str = Field(..., description="Amount with units (e.g., '2 days')")
    match_score: float = Field(..., alias="matchScore", description="Relevance score 0-1")

    model_config = {"populate_by_name": True}


class TimeOffSearchResponse(BaseModel):
    """Response for time-off search."""

    requests: list[TimeOffSearchResult] = Field(default_factory=list)
    page: int = Field(1)
    page_size: int = Field(20, alias="pageSize")
    total: int = Field(0)

    model_config = {"populate_by_name": True}


# =============================================================================
# Search Metadata Schemas
# =============================================================================


class MetadataSearchResult(BaseModel):
    """A single metadata result in search response.

    Can represent a field, list option, or user depending on entityType.
    """

    entity_type: str = Field(
        ..., alias="entityType", description="Type: 'field', 'listOption', or 'user'"
    )
    id: str = Field(..., description="Entity ID")
    name: str | None = Field(None, description="Display name")
    # Field-specific
    type: str | None = Field(None, description="Field type (for field entities)")
    # ListOption-specific
    field_id: str | None = Field(
        None, alias="fieldId", description="Parent field ID (for listOption entities)"
    )
    option_id: str | None = Field(
        None, alias="optionId", description="Option ID (for listOption entities)"
    )
    label: str | None = Field(None, description="Option label (for listOption entities)")
    # User-specific
    email: str | None = Field(None, description="User email (for user entities)")
    first_name: str | None = Field(
        None, alias="firstName", description="First name (for user entities)"
    )
    last_name: str | None = Field(
        None, alias="lastName", description="Last name (for user entities)"
    )
    # Score
    match_score: float = Field(..., alias="matchScore", description="Relevance score 0-1")

    model_config = {"populate_by_name": True}


class MetadataSearchResponse(BaseModel):
    """Response for metadata search."""

    results: list[MetadataSearchResult] = Field(default_factory=list)
    page: int = Field(1)
    page_size: int = Field(20, alias="pageSize")
    total: int = Field(0)

    model_config = {"populate_by_name": True}


# =============================================================================
# Valid Entity Types and Status Values
# =============================================================================

VALID_ENTITY_TYPES = {"fields", "listOptions", "users"}

VALID_TIME_OFF_STATUSES = {"approved", "denied", "requested", "canceled", "superseded"}

# Valid employee fields for search
VALID_EMPLOYEE_SEARCH_FIELDS = {
    "id",
    "first_name",
    "last_name",
    "preferred_name",
    "department",
    "email",
    "work_email",
    "job_title",
    "location",
    "status",
    "hire_date",
}

# Default fields returned in employee search (snake_case; converted to camelCase on output)
DEFAULT_EMPLOYEE_SEARCH_FIELDS = ["first_name", "last_name", "department", "email", "job_title"]
