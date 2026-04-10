"""User-related Pydantic models for Greenhouse MCP Server.

Defines input and output schemas for user tools:
- greenhouse_users_list
- greenhouse_users_get
- greenhouse_users_create
"""

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import EmailStr, Field, field_validator
from schemas.common import PaginationMeta


class DepartmentOutput(BaseModel):
    """Department info returned in user responses."""

    id: int
    name: str
    parent_id: int | None
    child_ids: list[int]
    external_id: str | None


class OfficeLocationOutput(BaseModel):
    """Simplified location info for office responses."""

    name: str | None


class OfficeOutput(BaseModel):
    """Office info returned in user responses."""

    id: int
    name: str
    location: OfficeLocationOutput
    primary_contact_user_id: int | None
    parent_id: int | None
    child_ids: list[int]
    external_id: str | None


class UserOutput(BaseModel):
    """Harvest-style user representation."""

    id: int
    name: str
    first_name: str | None
    last_name: str | None
    primary_email_address: str | None
    emails: list[str]
    employee_id: str | None
    disabled: bool
    site_admin: bool
    created_at: str | None
    updated_at: str | None
    linked_candidate_ids: list[int]
    departments: list[DepartmentOutput]
    offices: list[OfficeOutput]


class ListUsersOutput(BaseModel):
    """Response payload for greenhouse_users_list."""

    users: list[UserOutput]
    meta: PaginationMeta


class ListUsersInput(BaseModel):
    """Input for listing users with filters.

    Tool: greenhouse_users_list
    API: GET /users
    """

    per_page: int = Field(
        default=100, ge=1, le=500, description="Number of results per page (max 500)"
    )
    page: int = Field(default=1, ge=1, description="Page number (1-indexed)")
    skip_count: bool = Field(
        default=False, description="Skip total count calculation for performance"
    )
    email: str | None = Field(default=None, description="Filter by email address")
    employee_id: str | None = Field(default=None, description="Filter by employee ID")
    created_before: str | None = Field(default=None, description="ISO 8601 timestamp filter")
    created_after: str | None = Field(default=None, description="ISO 8601 timestamp filter")
    updated_before: str | None = Field(default=None, description="ISO 8601 timestamp filter")
    updated_after: str | None = Field(default=None, description="ISO 8601 timestamp filter")


class GetUserInput(BaseModel):
    """Input for retrieving a single user.

    Tool: greenhouse_users_get
    API: GET /users/{id}
    """

    user_id: int = Field(
        ...,
        description="User ID to retrieve",
        json_schema_extra={
            "x-populate-from": "greenhouse_users_list",
            "x-populate-field": "users",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )


class CreateUserInput(BaseModel):
    """Input for creating a new user.

    Tool: greenhouse_users_create
    API: POST /users

    Creates a new user with Basic permissions.
    """

    first_name: str = Field(..., description="The user's first name")
    last_name: str = Field(..., description="The user's last name")
    email: EmailStr = Field(..., description="The user's email address. Must be valid.")
    employee_id: str | None = Field(default=None, description="The user's external employee ID")
    office_ids: list[int] | None = Field(
        default=None,
        description="Office ID(s) to associate with the user.",
        json_schema_extra={
            "x-populate-from": "greenhouse_offices_list",
            "x-populate-field": "offices",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )
    department_ids: list[int] | None = Field(
        default=None,
        description="Department ID(s) to associate with the user.",
        json_schema_extra={
            "x-populate-from": "greenhouse_departments_list",
            "x-populate-field": "departments",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )

    @field_validator("office_ids", "department_ids", mode="before")
    @classmethod
    def filter_empty_strings(cls, v):
        """Filter out empty strings from list fields and convert empty lists to None."""
        if v is None:
            return None
        if isinstance(v, list):
            # Filter out empty strings and None values
            filtered = [x for x in v if x != "" and x is not None]
            return filtered if filtered else None
        return v
