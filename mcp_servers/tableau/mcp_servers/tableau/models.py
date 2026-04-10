"""Pydantic models for tableau.

Define your API specification here using Pydantic models.
These models will:
1. Validate inputs/outputs automatically
2. Generate type hints for IDE support
3. Serve as documentation
4. Enable test generation
"""

import re
from enum import Enum
from typing import Any

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import EmailStr, Field, ValidationInfo, field_validator

# UUID v4 regex pattern (case-insensitive)
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# ============================================================================
# BASE MODELS WITH VALIDATORS
# ============================================================================


class SiteAwareModel(BaseModel):
    """Base model for all models with UUID validation for ID fields."""

    @field_validator(
        "site_id",
        "owner_id",
        "project_id",
        "parent_project_id",
        "workbook_id",
        "group_id",
        "user_id",
        "datasource_id",
        "connection_id",
        "resource_id",
        "grantee_id",
        "idp_configuration_id",
        check_fields=False,
    )
    @classmethod
    def validate_uuid_fields(cls, v: str | None, info: ValidationInfo) -> str | None:
        if v is not None and not UUID_PATTERN.match(v):
            raise ValueError(f"{info.field_name} must be a valid UUID v4 (36 characters), got: {v}")
        return v


# ============================================================================
# CONSTANTS - Tableau API v3.x Site Roles and Permissions
# ============================================================================

# Valid site roles as per Tableau REST API v3.0+
# Reference: https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api_concepts_new_site_roles.htm
VALID_SITE_ROLES = [
    "Creator",  # Full creator license - can create and publish workbooks, datasources
    "Explorer",  # Explorer license - can view and interact but cannot publish
    "ExplorerCanPublish",  # Explorer license with publish capability
    "SiteAdministratorExplorer",  # Site admin with Explorer license
    "SiteAdministratorCreator",  # Site admin with Creator license - full admin rights
    "Unlicensed",  # No license - view-only access to owned content only
    "ReadOnly",  # Read-only access to all permitted content
    "Viewer",  # Viewer license - basic viewing rights
]

# Valid resource types for permissions
VALID_RESOURCE_TYPES = [
    "project",  # Project resource
    "workbook",  # Workbook resource
    "datasource",  # Datasource resource
]

# Valid grantee types for permissions
VALID_GRANTEE_TYPES = [
    "user",  # Individual user
    "group",  # User group
]

# Valid permission capabilities
# Reference: https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api_concepts_permissions.htm
VALID_CAPABILITIES = [
    "Read",  # View the resource
    "Write",  # Edit/modify the resource
    "ChangePermissions",  # Modify permissions on the resource
]

# Valid permission modes
VALID_MODES = [
    "Allow",  # Grant permission
    "Deny",  # Explicitly deny permission
]

# ============================================================================
# SITE MODELS
# ============================================================================


class TableauListSitesInput(BaseModel):
    """Input specification for listing sites."""

    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number for pagination - integer starting at 1 (first page). Must be >= 1.",
    )
    page_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Number of items per page - integer between 1 and 1000. Defaults to 100 if not specified.",
    )


class TableauSiteOutput(BaseModel):
    """Output specification for a single site."""

    id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string with hyphens, e.g., '550e8400-e29b-41d4-a716-446655440000')",
    )
    name: str = Field(
        ...,
        description="Site name - human-readable identifier for the site",
    )
    content_url: str = Field(
        ...,
        description="Site content URL identifier - URL-friendly path component for the site",
    )
    created_at: str = Field(
        ...,
        description="Site creation timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )
    updated_at: str = Field(
        ...,
        description="Last update timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )


class TableauListSitesOutput(BaseModel):
    """Output specification for listing sites."""

    sites: list[TableauSiteOutput] = Field(
        ...,
        description="List of site objects. Each object contains full site details. Empty list if no sites found.",
    )
    total_count: int = Field(
        ...,
        description="Total number of sites across all pages - integer >= 0. Use for pagination calculation.",
    )
    page_number: int = Field(
        ...,
        description="Current page number being returned - integer >= 1. Echo of the input page_number.",
    )
    page_size: int = Field(
        ...,
        description="Page size used for this response - integer between 1 and 1000. Echo of the input page_size.",
    )


# ============================================================================
# PROJECT MODELS
# ============================================================================


class TableauCreateProjectInput(SiteAwareModel):
    """Input specification for creating a project."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string with hyphens, e.g., '550e8400-e29b-41d4-a716-446655440000'). Must exist in sites table.",
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Project name - 1-255 characters, human-readable identifier for the project",
    )
    description: str = Field(
        default="",
        description="Optional project description - free-text field to describe the project's purpose or content. Defaults to empty string if not provided.",
    )
    parent_project_id: str | None = Field(
        None,
        description="Optional parent project identifier - UUID v4 format (36-character string). If provided, creates nested project hierarchy. Must reference an existing project. Set to null for top-level projects.",
    )
    owner_id: str = Field(
        ...,
        description="Project owner identifier - UUID v4 format (36-character string). Must reference an existing user in the users table. Owner has full control over the project.",
    )


class TableauCreateProjectOutput(BaseModel):
    """Output specification for project creation."""

    id: str = Field(
        ...,
        description="Newly generated project identifier - UUID v4 format (36-character string), automatically generated on creation",
    )
    name: str = Field(..., description="Project name - 1-255 characters as provided in input")
    description: str = Field(
        ..., description="Project description - as provided in input or empty string"
    )
    parent_project_id: str | None = Field(
        ...,
        description="Parent project identifier if nested, null if top-level - UUID v4 format or null",
    )
    owner_id: str = Field(
        ..., description="Project owner identifier - UUID v4 format (36-character string)"
    )
    created_at: str = Field(
        ...,
        description="Creation timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )
    updated_at: str = Field(
        ...,
        description="Last update timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z'). Initially same as created_at.",
    )


class TableauListProjectsInput(SiteAwareModel):
    """Input specification for listing projects."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Filters projects to those within this site.",
    )
    parent_project_id: str | None = Field(
        None,
        description="Optional filter by parent project - UUID v4 format (36-character string). If provided, returns only direct children of this project. If null, returns all projects regardless of hierarchy.",
    )
    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number for pagination - integer starting at 1 (first page). Must be >= 1.",
    )
    page_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Number of items per page - integer between 1 and 1000. Defaults to 100 if not specified.",
    )


class TableauListProjectsOutput(BaseModel):
    """Output specification for listing projects."""

    projects: list[TableauCreateProjectOutput] = Field(
        ...,
        description="List of project objects matching the query filters. Each object contains full project details. Empty list if no projects found.",
    )
    total_count: int = Field(
        ...,
        description="Total number of projects matching the filters across all pages - integer >= 0. Use for pagination calculation.",
    )
    page_number: int = Field(
        ...,
        description="Current page number being returned - integer >= 1. Echo of the input page_number.",
    )
    page_size: int = Field(
        ...,
        description="Page size used for this response - integer between 1 and 1000. Echo of the input page_size.",
    )


class TableauGetProjectInput(SiteAwareModel):
    """Input specification for getting a project."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Used to validate project belongs to this site.",
    )
    project_id: str = Field(
        ...,
        description="Project identifier to retrieve - UUID v4 format (36-character string). Must exist in the projects table.",
    )


class TableauGetProjectOutput(BaseModel):
    """Output specification for getting a project (same as create output)."""

    id: str = Field(..., description="Project identifier - UUID v4 format (36-character string)")
    name: str = Field(..., description="Project name - 1-255 characters")
    description: str = Field(
        ..., description="Project description - text field, may be empty string"
    )
    parent_project_id: str | None = Field(
        ...,
        description="Parent project identifier if nested, null if top-level - UUID v4 format or null",
    )
    owner_id: str = Field(
        ..., description="Project owner identifier - UUID v4 format (36-character string)"
    )
    created_at: str = Field(
        ...,
        description="Creation timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )
    updated_at: str = Field(
        ...,
        description="Last update timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )


class TableauUpdateProjectInput(SiteAwareModel):
    """Input specification for updating a project."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Used to validate project belongs to this site.",
    )
    project_id: str = Field(
        ...,
        description="Project identifier to update - UUID v4 format (36-character string). Must exist in the projects table.",
    )
    name: str | None = Field(
        None,
        min_length=1,
        max_length=255,
        description="Optional new project name - 1-255 characters. If null, name remains unchanged. If provided, must not be empty.",
    )
    description: str | None = Field(
        None,
        description="Optional new project description - free-text field. If null, description remains unchanged. Can be set to empty string to clear description.",
    )


class TableauUpdateProjectOutput(BaseModel):
    """Output specification for updating a project (same as create output)."""

    id: str = Field(
        ...,
        description="Project identifier - UUID v4 format (36-character string), unchanged from input",
    )
    name: str = Field(
        ..., description="Project name - 1-255 characters, updated if new value was provided"
    )
    description: str = Field(
        ..., description="Project description - text field, updated if new value was provided"
    )
    parent_project_id: str | None = Field(
        ...,
        description="Parent project identifier if nested, null if top-level - UUID v4 format or null. Cannot be changed via update.",
    )
    owner_id: str = Field(
        ...,
        description="Project owner identifier - UUID v4 format (36-character string). Cannot be changed via update.",
    )
    created_at: str = Field(
        ...,
        description="Creation timestamp - ISO 8601 format with UTC timezone, unchanged from original",
    )
    updated_at: str = Field(
        ...,
        description="Last update timestamp - ISO 8601 format with UTC timezone. Automatically updated to current time on modification.",
    )


class TableauDeleteProjectInput(SiteAwareModel):
    """Input specification for deleting a project."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Used to validate project belongs to this site.",
    )
    project_id: str = Field(
        ...,
        description="Project identifier to delete - UUID v4 format (36-character string). Must exist. Deletion cascades to child projects, workbooks, and datasources in this project.",
    )


class TableauDeleteProjectOutput(BaseModel):
    """Output specification for deleting a project."""

    success: bool = Field(
        ...,
        description="Deletion status - true if project was successfully deleted, false if operation failed",
    )
    message: str = Field(
        ...,
        description="Human-readable status message describing the result of the deletion operation",
    )


# ============================================================================
# WORKBOOK MODELS
# ============================================================================


class TableauCreateWorkbookInput(SiteAwareModel):
    """Input specification for creating a workbook."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Validates that project and owner belong to this site.",
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Workbook name - 1-255 characters, human-readable identifier for the workbook",
    )
    project_id: str = Field(
        ...,
        description="Project identifier where workbook will be published - UUID v4 format (36-character string). Must reference an existing project.",
    )
    owner_id: str = Field(
        ...,
        description="Workbook owner identifier - UUID v4 format (36-character string). Must reference an existing user. Owner has full control over the workbook.",
    )
    description: str = Field(
        default="",
        description="Optional workbook description - free-text field to describe the workbook's purpose or content. Defaults to empty string if not provided.",
    )
    file_reference: str | None = Field(
        None,
        description="Optional file path or reference to the workbook file - string up to 500 characters (e.g., '/path/to/workbook.twbx' or 's3://bucket/workbook.twbx'). Null if no file reference is provided.",
    )


class TableauCreateWorkbookOutput(BaseModel):
    """Output specification for workbook creation."""

    id: str = Field(
        ...,
        description="Newly generated workbook identifier - UUID v4 format (36-character string), automatically generated on creation",
    )
    name: str = Field(..., description="Workbook name - 1-255 characters as provided in input")
    project_id: str = Field(
        ...,
        description="Project identifier where workbook is published - UUID v4 format (36-character string)",
    )
    owner_id: str = Field(
        ..., description="Workbook owner identifier - UUID v4 format (36-character string)"
    )
    file_reference: str | None = Field(
        ...,
        description="File path or reference to the workbook file - string up to 500 characters, or null if not provided",
    )
    description: str = Field(
        ..., description="Workbook description - as provided in input or empty string"
    )
    created_at: str = Field(
        ...,
        description="Creation timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )
    updated_at: str = Field(
        ...,
        description="Last update timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z'). Initially same as created_at.",
    )


class TableauListWorkbooksInput(SiteAwareModel):
    """Input specification for listing workbooks."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Filters workbooks to those within this site.",
    )
    project_id: str | None = Field(
        None,
        description="Optional filter by project - UUID v4 format (36-character string). If provided, returns only workbooks in this project. If null, returns workbooks from all projects.",
    )
    owner_id: str | None = Field(
        None,
        description="Optional filter by owner - UUID v4 format (36-character string). If provided, returns only workbooks owned by this user. If null, returns workbooks owned by any user.",
    )
    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number for pagination - integer starting at 1 (first page). Must be >= 1.",
    )
    page_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Number of items per page - integer between 1 and 1000. Defaults to 100 if not specified.",
    )


class TableauListWorkbooksOutput(BaseModel):
    """Output specification for listing workbooks."""

    workbooks: list[TableauCreateWorkbookOutput] = Field(
        ...,
        description="List of workbook objects matching the query filters. Each object contains full workbook details. Empty list if no workbooks found.",
    )
    total_count: int = Field(
        ...,
        description="Total number of workbooks matching the filters across all pages - integer >= 0. Use for pagination calculation.",
    )
    page_number: int = Field(
        ...,
        description="Current page number being returned - integer >= 1. Echo of the input page_number.",
    )
    page_size: int = Field(
        ...,
        description="Page size used for this response - integer between 1 and 1000. Echo of the input page_size.",
    )


class TableauGetWorkbookInput(SiteAwareModel):
    """Input specification for getting a workbook."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Used to validate workbook belongs to this site.",
    )
    workbook_id: str = Field(
        ...,
        description="Workbook identifier to retrieve - UUID v4 format (36-character string). Must exist in the workbooks table.",
    )


class TableauGetWorkbookOutput(BaseModel):
    """Output specification for getting a workbook (same as create output)."""

    id: str = Field(..., description="Workbook identifier - UUID v4 format (36-character string)")
    name: str = Field(..., description="Workbook name - 1-255 characters")
    project_id: str = Field(
        ...,
        description="Project identifier where workbook is published - UUID v4 format (36-character string)",
    )
    owner_id: str = Field(
        ..., description="Workbook owner identifier - UUID v4 format (36-character string)"
    )
    file_reference: str | None = Field(
        ...,
        description="File path or reference to the workbook file - string up to 500 characters, or null",
    )
    description: str = Field(
        ..., description="Workbook description - text field, may be empty string"
    )
    created_at: str = Field(
        ...,
        description="Creation timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )
    updated_at: str = Field(
        ...,
        description="Last update timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )


class TableauUpdateWorkbookInput(SiteAwareModel):
    """Input specification for updating a workbook."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Used to validate workbook belongs to this site.",
    )
    workbook_id: str = Field(
        ...,
        description="Workbook identifier to update - UUID v4 format (36-character string). Must exist in the workbooks table.",
    )
    name: str | None = Field(
        None,
        min_length=1,
        description="Optional new workbook name - 1-255 characters. If null, name remains unchanged. If provided, must not be empty.",
    )
    description: str | None = Field(
        None,
        description="Optional new workbook description - free-text field. If null, description remains unchanged. Can be set to empty string to clear description.",
    )


class TableauUpdateWorkbookOutput(BaseModel):
    """Output specification for updating a workbook (same as create output)."""

    id: str = Field(
        ...,
        description="Workbook identifier - UUID v4 format (36-character string), unchanged from input",
    )
    name: str = Field(
        ..., description="Workbook name - 1-255 characters, updated if new value was provided"
    )
    project_id: str = Field(
        ...,
        description="Project identifier - UUID v4 format (36-character string). Cannot be changed via update.",
    )
    owner_id: str = Field(
        ...,
        description="Workbook owner identifier - UUID v4 format (36-character string). Cannot be changed via update.",
    )
    file_reference: str | None = Field(
        ...,
        description="File path or reference - string up to 500 characters or null. Cannot be changed via update.",
    )
    description: str = Field(
        ..., description="Workbook description - text field, updated if new value was provided"
    )
    created_at: str = Field(
        ...,
        description="Creation timestamp - ISO 8601 format with UTC timezone, unchanged from original",
    )
    updated_at: str = Field(
        ...,
        description="Last update timestamp - ISO 8601 format with UTC timezone. Automatically updated to current time on modification.",
    )


class TableauDeleteWorkbookInput(SiteAwareModel):
    """Input specification for deleting a workbook."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Used to validate workbook belongs to this site.",
    )
    workbook_id: str = Field(
        ...,
        description="Workbook identifier to delete - UUID v4 format (36-character string). Must exist. Deletion cascades to: all views (worksheets, dashboards, stories) within the workbook, workbook-datasource connections, and associated permissions. Does NOT delete referenced datasources.",
    )


class TableauDeleteWorkbookOutput(BaseModel):
    """Output specification for deleting a workbook."""

    success: bool = Field(
        ...,
        description="Deletion status - true if workbook was successfully deleted, false if operation failed",
    )
    message: str = Field(
        ...,
        description="Human-readable status message describing the result of the deletion operation",
    )


# ============================================================================
# DATASOURCE MODELS
# ============================================================================


class TableauCreateDatasourceInput(SiteAwareModel):
    """Input specification for creating a datasource."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Validates that project and owner belong to this site.",
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Datasource name - 1-255 characters, human-readable identifier for the datasource",
    )
    project_id: str = Field(
        ...,
        description="Project identifier where datasource will be published - UUID v4 format (36-character string). Must reference an existing project.",
    )
    owner_id: str = Field(
        ...,
        description="Datasource owner identifier - UUID v4 format (36-character string). Must reference an existing user. Owner has full control over the datasource.",
    )
    connection_type: str = Field(
        ...,
        min_length=1,
        description="Database or file connection type - string up to 50 characters. Examples: 'postgres', 'mysql', 'sqlserver', 'oracle', 'excel', 'csv', 'snowflake', 'redshift', 'bigquery'. Must be non-empty.",
    )
    description: str = Field(
        default="",
        description="Optional datasource description - free-text field to describe the datasource's purpose or content. Defaults to empty string if not provided.",
    )


class TableauCreateDatasourceOutput(BaseModel):
    """Output specification for datasource creation."""

    id: str = Field(
        ...,
        description="Newly generated datasource identifier - UUID v4 format (36-character string), automatically generated on creation",
    )
    name: str = Field(..., description="Datasource name - 1-255 characters as provided in input")
    project_id: str = Field(
        ...,
        description="Project identifier where datasource is published - UUID v4 format (36-character string)",
    )
    owner_id: str = Field(
        ..., description="Datasource owner identifier - UUID v4 format (36-character string)"
    )
    connection_type: str = Field(
        ...,
        description="Database or file connection type - string up to 50 characters (e.g., 'postgres', 'mysql', 'excel')",
    )
    description: str = Field(
        ..., description="Datasource description - as provided in input or empty string"
    )
    created_at: str = Field(
        ...,
        description="Creation timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )
    updated_at: str = Field(
        ...,
        description="Last update timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z'). Initially same as created_at.",
    )


class TableauListDatasourcesInput(SiteAwareModel):
    """Input specification for listing datasources."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Filters datasources to those within this site.",
    )
    project_id: str | None = Field(
        None,
        description="Optional filter by project - UUID v4 format (36-character string). If provided, returns only datasources in this project. If null, returns datasources from all projects.",
    )
    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number for pagination - integer starting at 1 (first page). Must be >= 1.",
    )
    page_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Number of items per page - integer between 1 and 1000. Defaults to 100 if not specified.",
    )


class TableauListDatasourcesOutput(BaseModel):
    """Output specification for listing datasources."""

    datasources: list[TableauCreateDatasourceOutput] = Field(
        ...,
        description="List of datasource objects matching the query filters. Each object contains full datasource details. Empty list if no datasources found.",
    )
    total_count: int = Field(
        ...,
        description="Total number of datasources matching the filters across all pages - integer >= 0. Use for pagination calculation.",
    )
    page_number: int = Field(
        ...,
        description="Current page number being returned - integer >= 1. Echo of the input page_number.",
    )
    page_size: int = Field(
        ...,
        description="Page size used for this response - integer between 1 and 1000. Echo of the input page_size.",
    )


class TableauGetDatasourceInput(SiteAwareModel):
    """Input specification for getting a datasource."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Used to validate datasource belongs to this site.",
    )
    datasource_id: str = Field(
        ...,
        description="Datasource identifier to retrieve - UUID v4 format (36-character string). Must exist in the datasources table.",
    )


class TableauGetDatasourceOutput(BaseModel):
    """Output specification for getting a datasource (same as create output)."""

    id: str = Field(..., description="Datasource identifier - UUID v4 format (36-character string)")
    name: str = Field(..., description="Datasource name - 1-255 characters")
    project_id: str = Field(
        ...,
        description="Project identifier where datasource is published - UUID v4 format (36-character string)",
    )
    owner_id: str = Field(
        ..., description="Datasource owner identifier - UUID v4 format (36-character string)"
    )
    connection_type: str = Field(
        ...,
        description="Database or file connection type - string up to 50 characters (e.g., 'postgres', 'mysql', 'excel')",
    )
    description: str = Field(
        ..., description="Datasource description - text field, may be empty string"
    )
    created_at: str = Field(
        ...,
        description="Creation timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )
    updated_at: str = Field(
        ...,
        description="Last update timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )


class TableauUpdateDatasourceInput(SiteAwareModel):
    """Input specification for updating a datasource."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Used to validate datasource belongs to this site.",
    )
    datasource_id: str = Field(
        ...,
        description="Datasource identifier to update - UUID v4 format (36-character string). Must exist in the datasources table.",
    )
    name: str | None = Field(
        None,
        min_length=1,
        max_length=255,
        description="Optional new datasource name - 1-255 characters. If null, name remains unchanged. If provided, must not be empty.",
    )
    description: str | None = Field(
        None,
        description="Optional new datasource description - free-text field. If null, description remains unchanged. Can be set to empty string to clear description.",
    )
    connection_type: str | None = Field(
        None,
        min_length=1,
        description="Optional new connection type - string up to 50 characters (e.g., 'postgres', 'mysql', 'excel'). If null, connection_type remains unchanged. If provided, must not be empty.",
    )


class TableauUpdateDatasourceOutput(BaseModel):
    """Output specification for updating a datasource (same as create output)."""

    id: str = Field(
        ...,
        description="Datasource identifier - UUID v4 format (36-character string), unchanged from input",
    )
    name: str = Field(
        ..., description="Datasource name - 1-255 characters, updated if new value was provided"
    )
    project_id: str = Field(
        ...,
        description="Project identifier - UUID v4 format (36-character string). Cannot be changed via update.",
    )
    owner_id: str = Field(
        ...,
        description="Datasource owner identifier - UUID v4 format (36-character string). Cannot be changed via update.",
    )
    connection_type: str = Field(
        ...,
        description="Database or file connection type - string up to 50 characters, updated if new value was provided",
    )
    description: str = Field(
        ..., description="Datasource description - text field, updated if new value was provided"
    )
    created_at: str = Field(
        ...,
        description="Creation timestamp - ISO 8601 format with UTC timezone, unchanged from original",
    )
    updated_at: str = Field(
        ...,
        description="Last update timestamp - ISO 8601 format with UTC timezone. Automatically updated to current time on modification.",
    )


class TableauDeleteDatasourceInput(SiteAwareModel):
    """Input specification for deleting a datasource."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Used to validate datasource belongs to this site.",
    )
    datasource_id: str = Field(
        ...,
        description="Datasource identifier to delete - UUID v4 format (36-character string). Must exist. Deletion cascades to workbook-datasource connections.",
    )


class TableauDeleteDatasourceOutput(BaseModel):
    """Output specification for deleting a datasource."""

    success: bool = Field(
        ...,
        description="Deletion status - true if datasource was successfully deleted, false if operation failed",
    )
    message: str = Field(
        ...,
        description="Human-readable status message describing the result of the deletion operation",
    )


# ============================================================================
# WORKBOOK-DATASOURCE CONNECTION MODELS
# ============================================================================


class TableauCreateWorkbookConnectionInput(SiteAwareModel):
    """Input specification for linking a workbook to a datasource."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Validates that both workbook and datasource belong to this site.",
    )
    workbook_id: str = Field(
        ...,
        description="Workbook identifier - UUID v4 format (36-character string). Must reference an existing workbook in the site.",
    )
    datasource_id: str = Field(
        ...,
        description="Datasource identifier - UUID v4 format (36-character string). Must reference an existing datasource in the site. Creates a many-to-many relationship.",
    )


class TableauCreateWorkbookConnectionOutput(BaseModel):
    """Output specification for workbook-datasource connection."""

    success: bool = Field(
        ...,
        description="Creation status - true if connection was created, false if operation was not supported",
    )
    id: str | None = Field(
        None,
        description="Connection identifier - UUID v4 format (36-character string). None if creation failed or not supported.",
    )
    workbook_id: str = Field(
        ...,
        description="Workbook identifier - UUID v4 format (36-character string), as provided in input",
    )
    datasource_id: str = Field(
        ...,
        description="Datasource identifier - UUID v4 format (36-character string), as provided in input",
    )
    created_at: str | None = Field(
        None,
        description="Connection creation timestamp - ISO 8601 format with UTC timezone. None if creation failed.",
    )
    message: str | None = Field(
        None,
        description="Optional message with additional context (e.g., API limitations)",
    )


class TableauListWorkbookConnectionsInput(SiteAwareModel):
    """Input specification for listing workbook connections."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Used to validate workbook belongs to this site.",
    )
    workbook_id: str = Field(
        ...,
        description="Workbook identifier - UUID v4 format (36-character string). Returns all datasource connections for this workbook.",
    )


class TableauListWorkbookConnectionsOutput(BaseModel):
    """Output specification for listing workbook connections."""

    connections: list[TableauCreateWorkbookConnectionOutput] = Field(
        ...,
        description="List of all datasource connections for the workbook. Each object contains connection ID, workbook ID, datasource ID, and creation timestamp. Empty list if workbook has no datasource connections.",
    )


class TableauDeleteWorkbookConnectionInput(SiteAwareModel):
    """Input specification for deleting a workbook connection."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Used to validate workbook belongs to this site.",
    )
    workbook_id: str = Field(
        ...,
        description="Workbook identifier - UUID v4 format (36-character string). Used to verify the connection belongs to this workbook.",
    )
    connection_id: str = Field(
        ...,
        description="Connection identifier to delete - UUID v4 format (36-character string). Must be an existing connection belonging to the specified workbook.",
    )


class TableauDeleteWorkbookConnectionOutput(BaseModel):
    """Output specification for deleting a workbook connection."""

    success: bool = Field(
        ...,
        description="Deletion status - true if connection was successfully deleted, false if operation failed",
    )
    message: str | None = Field(
        None,
        description="Optional message with additional context (e.g., API limitations)",
    )


# ============================================================================
# USER MODELS
# ============================================================================


class TableauCreateUserInput(SiteAwareModel):
    """Input specification for creating a user."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). User will be created within this site. Must exist in sites table.",
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Username - 1-255 characters, must be unique within the site. Used for login and identification. Cannot contain only whitespace.",
    )
    email: EmailStr | None = Field(
        None,
        description="Optional email address for notifications - valid email format, up to 255 characters. Only supported in Tableau Cloud API 3.26+. Set to null for on-premise deployments or if no email is needed.",
    )
    site_role: str = Field(
        ...,
        description="Site role - must be exactly one of: 'Creator' (full creator license with publish rights), 'Explorer' (view and interact, cannot publish), 'ExplorerCanPublish' (Explorer with publish capability), 'SiteAdministratorExplorer' (site admin with Explorer license), 'SiteAdministratorCreator' (site admin with Creator license, full admin rights), 'Unlicensed' (no license, view-only access to owned content), 'ReadOnly' (read-only access to permitted content), 'Viewer' (basic viewing license). Reference: VALID_SITE_ROLES constant.",
    )


class TableauCreateUserOutput(BaseModel):
    """Output specification for user creation."""

    id: str = Field(
        ...,
        description="Newly generated user identifier - UUID v4 format (36-character string), automatically generated on creation",
    )
    name: str = Field(
        ..., description="Username - 1-255 characters as provided in input, unique within the site"
    )
    email: str | None = Field(
        ...,
        description="Email address if provided - valid email format up to 255 characters, or null if not provided",
    )
    site_role: str = Field(
        ...,
        description="Site role - one of the 8 valid VALID_SITE_ROLES values (e.g., 'Creator', 'Explorer', 'Viewer')",
    )
    created_at: str = Field(
        ...,
        description="User creation timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )
    updated_at: str = Field(
        ...,
        description="Last update timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z'). Initially same as created_at.",
    )


class TableauListUsersInput(SiteAwareModel):
    """Input specification for listing users."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Returns only users within this site.",
    )
    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number for pagination - integer starting at 1 (first page). Must be >= 1.",
    )
    page_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Number of items per page - integer between 1 and 1000. Defaults to 100 if not specified.",
    )


class TableauListUsersOutput(BaseModel):
    """Output specification for listing users."""

    users: list[TableauCreateUserOutput] = Field(
        ...,
        description="List of user objects within the site. Each object contains full user details (id, name, email, site_role, timestamps). Empty list if no users found.",
    )
    total_count: int = Field(
        ...,
        description="Total number of users in the site across all pages - integer >= 0. Use for pagination calculation.",
    )
    page_number: int = Field(
        ...,
        description="Current page number being returned - integer >= 1. Echo of the input page_number.",
    )
    page_size: int = Field(
        ...,
        description="Page size used for this response - integer between 1 and 1000. Echo of the input page_size.",
    )


class TableauGetUserInput(SiteAwareModel):
    """Input specification for getting a user."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Used to validate user belongs to this site.",
    )
    user_id: str = Field(
        ...,
        description="User identifier to retrieve - UUID v4 format (36-character string). Must exist in the users table for the specified site.",
    )


class TableauGetUserOutput(BaseModel):
    """Output specification for getting a user (same as create output)."""

    id: str = Field(..., description="User identifier - UUID v4 format (36-character string)")
    name: str = Field(..., description="Username - 1-255 characters, unique within the site")
    email: str | None = Field(
        ..., description="Email address if set - valid email format up to 255 characters, or null"
    )
    site_role: str = Field(
        ...,
        description="Site role - one of the 8 valid VALID_SITE_ROLES values (e.g., 'Creator', 'Explorer', 'Viewer')",
    )
    created_at: str = Field(
        ...,
        description="User creation timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )
    updated_at: str = Field(
        ...,
        description="Last update timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )


class TableauUpdateUserInput(SiteAwareModel):
    """Input specification for updating a user.

    Only the following fields can be updated:
    - name: Username (must be unique per site)
    - email: Email address for notifications
    - site_role: User's site role (must be one of VALID_SITE_ROLES)
    """

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Used to validate user belongs to this site.",
    )
    user_id: str = Field(
        ...,
        description="User identifier to update - UUID v4 format (36-character string). Must exist in the users table for the specified site.",
    )
    name: str | None = Field(
        None,
        min_length=1,
        max_length=255,
        description="Optional new username - 1-255 characters, must be unique within the site. If null, username remains unchanged. Cannot be empty or whitespace-only.",
    )
    full_name: str | None = Field(
        None,
        description="Optional full display name - string for user's complete name (e.g., 'John Smith'). This is a display field and does not affect authentication. If null, full_name remains unchanged.",
    )
    email: EmailStr | None = Field(
        None,
        description="Optional notification email address - valid email format up to 255 characters. Only supported in Tableau Cloud API 3.26+. If null, email remains unchanged.",
    )
    password: str | None = Field(
        None,
        description="Optional new password for user authentication - string meeting password requirements. If null, password remains unchanged. Used for local authentication only.",
    )
    site_role: str | None = Field(
        None,
        description="Optional new site role - must be one of the 8 valid VALID_SITE_ROLES values: 'Creator', 'Explorer', 'ExplorerCanPublish', 'SiteAdministratorExplorer', 'SiteAdministratorCreator', 'Unlicensed', 'ReadOnly', 'Viewer'. If null, site_role remains unchanged.",
    )
    auth_setting: str | None = Field(
        None,
        description="Optional authentication method - string specifying how user authenticates (e.g., 'ServerDefault', 'SAML', 'OpenID'). If null, auth_setting remains unchanged.",
    )
    identity_pool_name: str | None = Field(
        None,
        description="Optional identity pool name - string for Tableau Server identity pools (on-premise only). Not supported in Tableau Cloud. If null, identity_pool_name remains unchanged.",
    )
    idp_configuration_id: str | None = Field(
        None,
        description="Optional Identity Provider configuration identifier - UUID v4 format for SAML/OpenID configuration (Tableau Cloud only). Not supported in Tableau Server. If null, idp_configuration_id remains unchanged.",
    )


class TableauUpdateUserOutput(BaseModel):
    """Output specification for updating a user (same as create output)."""

    id: str = Field(
        ...,
        description="User identifier - UUID v4 format (36-character string), unchanged from input",
    )
    name: str = Field(
        ..., description="Username - 1-255 characters, updated if new value was provided"
    )
    email: str | None = Field(
        ...,
        description="Email address - valid email format up to 255 characters or null, updated if new value was provided",
    )
    site_role: str = Field(
        ...,
        description="Site role - one of the 8 valid VALID_SITE_ROLES values, updated if new value was provided",
    )
    created_at: str = Field(
        ...,
        description="User creation timestamp - ISO 8601 format with UTC timezone, unchanged from original",
    )
    updated_at: str = Field(
        ...,
        description="Last update timestamp - ISO 8601 format with UTC timezone. Automatically updated to current time on modification.",
    )


class TableauDeleteUserInput(SiteAwareModel):
    """Input specification for deleting a user.

    Note: Tableau blocks deletion if user owns content unless map_assets_to is provided.
    """

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Used to validate user belongs to this site.",
    )
    user_id: str = Field(
        ...,
        description="User identifier to delete - UUID v4 format (36-character string). Must exist. If user owns content and map_assets_to is not provided, deletion is blocked and user role is changed to 'Unlicensed' instead.",
    )
    map_assets_to: str | None = Field(
        None,
        description="Optional target user identifier for content transfer - UUID v4 format (36-character string). Required if user owns any content (projects, workbooks, datasources, views). All owned content will be transferred to this user before deletion. Supported in Tableau API 3.9+. If null and user owns content, deletion is blocked.",
    )


class TableauDeleteUserOutput(BaseModel):
    """Output specification for deleting a user.

    If user owns content and map_assets_to is not provided:
    - success: False
    - role_changed_to: "Unlicensed"
    - message: explains user still exists with Unlicensed role

    If user deleted successfully:
    - success: True
    - role_changed_to: None
    - content_transferred_to: user_id if map_assets_to was used
    """

    success: bool = Field(
        ...,
        description="Deletion status - true if user was successfully deleted, false if deletion was blocked (typically because user owns content and no transfer target was provided)",
    )
    message: str = Field(
        ...,
        description="Human-readable status message - explains the result. If deletion blocked, explains why (e.g., 'User owns content and no map_assets_to provided. User role changed to Unlicensed.')",
    )
    role_changed_to: str | None = Field(
        None,
        description="New role if deletion was blocked - typically 'Unlicensed' when user owns content and cannot be deleted. Null if user was successfully deleted.",
    )
    content_transferred_to: str | None = Field(
        None,
        description="Target user identifier that received transferred content - UUID v4 format (36-character string). Only set if map_assets_to was used and content was successfully transferred. Null if no transfer occurred or if deletion failed.",
    )


# ============================================================================
# GROUP MODELS
# ============================================================================


class TableauCreateGroupInput(SiteAwareModel):
    """Input specification for creating a group."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Group will be created within this site context. Must exist in sites table.",
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Group name - 1-255 characters, must be unique across all sites (not just within site). Used for identifying and managing collections of users.",
    )
    description: str = Field(
        default="",
        description="Optional group description - free-text field to describe the group's purpose or members. Defaults to empty string if not provided.",
    )


class TableauCreateGroupOutput(BaseModel):
    """Output specification for group creation."""

    id: str = Field(
        ...,
        description="Newly generated group identifier - UUID v4 format (36-character string), automatically generated on creation",
    )
    name: str = Field(
        ...,
        description="Group name - 1-255 characters as provided in input, unique across all sites",
    )
    description: str = Field(
        ..., description="Group description - as provided in input or empty string"
    )
    created_at: str = Field(
        ...,
        description="Group creation timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )
    updated_at: str = Field(
        ...,
        description="Last update timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z'). Initially same as created_at.",
    )


class TableauListGroupsInput(SiteAwareModel):
    """Input specification for listing groups."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Returns groups accessible within this site context.",
    )
    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number for pagination - integer starting at 1 (first page). Must be >= 1.",
    )
    page_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Number of items per page - integer between 1 and 1000. Defaults to 100 if not specified.",
    )


class TableauListGroupsOutput(BaseModel):
    """Output specification for listing groups."""

    groups: list[TableauCreateGroupOutput] = Field(
        ...,
        description="List of group objects. Each object contains full group details (id, name, description, timestamps). Empty list if no groups found.",
    )
    total_count: int = Field(
        ...,
        description="Total number of groups across all pages - integer >= 0. Use for pagination calculation.",
    )
    page_number: int = Field(
        ...,
        description="Current page number being returned - integer >= 1. Echo of the input page_number.",
    )
    page_size: int = Field(
        ...,
        description="Page size used for this response - integer between 1 and 1000. Echo of the input page_size.",
    )


class TableauAddUserToGroupInput(SiteAwareModel):
    """Input specification for adding a user to a group."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Used to validate both group and user belong to this site context.",
    )
    group_id: str = Field(
        ...,
        description="Group identifier - UUID v4 format (36-character string). Must reference an existing group. User will be added to this group.",
    )
    user_id: str = Field(
        ...,
        description="User identifier - UUID v4 format (36-character string). Must reference an existing user in the site. Creates a many-to-many relationship between user and group.",
    )


class TableauAddUserToGroupOutput(BaseModel):
    """Output specification for adding a user to a group."""

    id: str = Field(
        ...,
        description="Newly generated group membership identifier - UUID v4 format (36-character string), automatically generated on creation",
    )
    group_id: str = Field(
        ...,
        description="Group identifier - UUID v4 format (36-character string), as provided in input",
    )
    user_id: str = Field(
        ...,
        description="User identifier - UUID v4 format (36-character string), as provided in input",
    )
    created_at: str = Field(
        ...,
        description="Membership creation timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )


class TableauRemoveUserFromGroupInput(SiteAwareModel):
    """Input specification for removing a user from a group."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Used to validate both group and user belong to this site context.",
    )
    group_id: str = Field(
        ...,
        description="Group identifier - UUID v4 format (36-character string). Must reference an existing group. User will be removed from this group.",
    )
    user_id: str = Field(
        ...,
        description="User identifier - UUID v4 format (36-character string). Must reference an existing user who is currently a member of the group.",
    )


class TableauRemoveUserFromGroupOutput(BaseModel):
    """Output specification for removing a user from a group."""

    success: bool = Field(
        ...,
        description="Removal status - true if user was successfully removed from the group, false if operation failed",
    )


# ============================================================================
# PERMISSION MODELS
# ============================================================================


class TableauGrantPermissionInput(SiteAwareModel):
    """Input specification for granting a permission."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Used to validate resource and grantee belong to this site.",
    )
    resource_type: str = Field(
        ...,
        description="Resource type - must be exactly one of: 'project' (project resource), 'workbook' (workbook resource), or 'datasource' (datasource resource). Reference: VALID_RESOURCE_TYPES constant.",
    )
    resource_id: str = Field(
        ...,
        description="Resource identifier - UUID v4 format (36-character string). Must reference an existing resource of the specified resource_type (project, workbook, or datasource).",
    )
    grantee_type: str = Field(
        ...,
        description="Grantee type - must be exactly one of: 'user' (individual user) or 'group' (user group). Reference: VALID_GRANTEE_TYPES constant.",
    )
    grantee_id: str = Field(
        ...,
        description="Grantee identifier - UUID v4 format (36-character string). Must reference an existing user (if grantee_type='user') or group (if grantee_type='group').",
    )
    capability: str = Field(
        ...,
        description="Permission capability - must be exactly one of: 'Read' (view the resource), 'Write' (edit/modify the resource), or 'ChangePermissions' (modify permissions on the resource). Reference: VALID_CAPABILITIES constant. See Tableau REST API documentation for complete capability list.",
    )
    mode: str = Field(
        ...,
        description="Permission mode - must be exactly one of: 'Allow' (grant permission) or 'Deny' (explicitly deny permission, overrides Allow). Reference: VALID_MODES constant.",
    )


class TableauGrantPermissionOutput(BaseModel):
    """Output specification for permission granting."""

    id: str = Field(
        ...,
        description="Newly generated permission identifier - UUID v4 format (36-character string), automatically generated on creation. May return existing permission ID if permission already exists (idempotent operation).",
    )
    resource_type: str = Field(
        ...,
        description="Resource type - 'project', 'workbook', or 'datasource', as provided in input",
    )
    resource_id: str = Field(
        ...,
        description="Resource identifier - UUID v4 format (36-character string), as provided in input",
    )
    grantee_type: str = Field(
        ..., description="Grantee type - 'user' or 'group', as provided in input"
    )
    grantee_id: str = Field(
        ...,
        description="Grantee identifier - UUID v4 format (36-character string), as provided in input",
    )
    capability: str = Field(
        ...,
        description="Permission capability - 'Read', 'Write', or 'ChangePermissions', as provided in input",
    )
    mode: str = Field(..., description="Permission mode - 'Allow' or 'Deny', as provided in input")
    created_at: str = Field(
        ...,
        description="Permission creation timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z'). If permission already existed, returns original creation timestamp.",
    )


class TableauListPermissionsInput(SiteAwareModel):
    """Input specification for listing permissions."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Used to validate resource belongs to this site context.",
    )
    resource_type: str = Field(
        ...,
        description="Resource type - must be exactly one of: 'project', 'workbook', or 'datasource'. Reference: VALID_RESOURCE_TYPES constant.",
    )
    resource_id: str = Field(
        ...,
        description="Resource identifier - UUID v4 format (36-character string). Returns all permissions (both Allow and Deny) for this specific resource.",
    )


class TableauListPermissionsOutput(BaseModel):
    """Output specification for listing permissions."""

    permissions: list[TableauGrantPermissionOutput] = Field(
        ...,
        description="List of all permission objects for the specified resource. Each object contains full permission details (id, resource_type, resource_id, grantee_type, grantee_id, capability, mode, created_at). Includes both 'Allow' and 'Deny' permissions. Empty list if no permissions are set on the resource.",
    )


class TableauRevokePermissionInput(SiteAwareModel):
    """Input specification for revoking a permission."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Used to validate resource belongs to this site context.",
    )
    resource_type: str = Field(
        ...,
        description="Resource type - must be exactly one of: 'project', 'workbook', or 'datasource'. Reference: VALID_RESOURCE_TYPES constant.",
    )
    resource_id: str = Field(
        ...,
        description="Resource identifier - UUID v4 format (36-character string). Must reference an existing resource of the specified type.",
    )
    grantee_type: str = Field(
        ...,
        description="Grantee type - must be exactly 'user' or 'group'. Required for HTTP mode to construct the correct API endpoint.",
    )
    grantee_id: str = Field(
        ...,
        description="Grantee identifier - UUID v4 format (36-character string). Can be either a user ID or group ID.",
    )
    capability: str = Field(
        ...,
        description="Permission capability to revoke - must be exactly one of: 'Read', 'Write', or 'ChangePermissions'. Reference: VALID_CAPABILITIES constant.",
    )
    mode: str = Field(
        ...,
        description="Permission mode - must be exactly 'Allow' or 'Deny'. Specifies which permission rule to revoke.",
    )


class TableauRevokePermissionOutput(BaseModel):
    """Output specification for revoking a permission."""

    success: bool = Field(
        ...,
        description="Revocation status - true if permission was successfully revoked, false if operation failed (e.g., permission did not exist)",
    )


# ============================================================================
# VIEW MODELS (Read-only - Views are created when workbooks are published)
# ============================================================================

# Valid sheet types for views
VALID_SHEET_TYPES = ["worksheet", "dashboard", "story"]


class TableauViewOutput(BaseModel):
    """Output specification for view data."""

    id: str = Field(
        ...,
        description="View identifier - UUID v4 format (36-character string with hyphens, e.g., '550e8400-e29b-41d4-a716-446655440000')",
    )
    workbook_id: str = Field(
        ...,
        description="Parent workbook identifier - UUID v4 format (36-character string). The workbook that contains this view.",
    )
    name: str = Field(
        ...,
        description="View name - 1-255 characters, human-readable identifier for the view/sheet",
    )
    content_url: str | None = Field(
        ...,
        description="URL-friendly path to the view (e.g., 'sheets/SalesOverview'). May be null for views without a content URL.",
    )
    sheet_type: str = Field(
        ...,
        description="Type of sheet - must be one of: 'worksheet' (single visualization), 'dashboard' (collection of views), or 'story' (narrative sequence). Reference: VALID_SHEET_TYPES constant.",
    )
    created_at: str = Field(
        ...,
        description="View creation timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )
    updated_at: str = Field(
        ...,
        description="Last update timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )


class TableauListViewsInput(SiteAwareModel):
    """Input specification for listing views."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string with hyphens, e.g., '550e8400-e29b-41d4-a716-446655440000'). Filters views to those within this site.",
    )
    workbook_id: str | None = Field(
        None,
        description="Optional filter by workbook - UUID v4 format (36-character string). If provided, returns only views belonging to this workbook. If null, returns views from all workbooks in the site.",
    )
    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number for pagination - integer starting at 1 (first page). Must be >= 1.",
    )
    page_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Number of items per page - integer between 1 and 1000. Defaults to 100 if not specified.",
    )


class TableauListViewsOutput(BaseModel):
    """Output specification for listing views."""

    views: list[TableauViewOutput] = Field(
        ...,
        description="List of view objects matching the query filters. Each object contains full view details. Empty list if no views found.",
    )
    total_count: int = Field(
        ...,
        description="Total number of views matching the filters across all pages - integer >= 0. Use for pagination calculation.",
    )
    page_number: int = Field(
        ...,
        description="Current page number being returned - integer >= 1. Echo of the input page_number.",
    )
    page_size: int = Field(
        ...,
        description="Page size used for this response - integer between 1 and 1000. Echo of the input page_size.",
    )


class TableauGetViewInput(SiteAwareModel):
    """Input specification for getting a view."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string with hyphens, e.g., '550e8400-e29b-41d4-a716-446655440000'). Used to validate view belongs to this site.",
    )
    view_id: str = Field(
        ...,
        description="View identifier to retrieve - UUID v4 format (36-character string). Must exist in the views table.",
    )


class TableauGetViewOutput(BaseModel):
    """Output specification for getting a view."""

    id: str = Field(
        ...,
        description="View identifier - UUID v4 format (36-character string)",
    )
    workbook_id: str = Field(
        ...,
        description="Parent workbook identifier - UUID v4 format (36-character string)",
    )
    name: str = Field(
        ...,
        description="View name - 1-255 characters",
    )
    content_url: str | None = Field(
        ...,
        description="URL-friendly path to the view, or null if not set",
    )
    sheet_type: str = Field(
        ...,
        description="Type of sheet - 'worksheet', 'dashboard', or 'story'. Reference: VALID_SHEET_TYPES constant.",
    )
    created_at: str = Field(
        ...,
        description="Creation timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )
    updated_at: str = Field(
        ...,
        description="Last update timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )


# ============================================================================
# VIEW DATA EXPORT MODELS
# ============================================================================


class TableauQueryViewDataInput(SiteAwareModel):
    """Input specification for querying view data as CSV."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Used to validate view belongs to this site.",
    )
    view_id: str = Field(
        ...,
        description="View identifier to query - UUID v4 format (36-character string). Must be a worksheet type view (dashboards and stories cannot be exported as CSV).",
    )
    max_age: int | None = Field(
        None,
        ge=1,
        description="Maximum age of cached data in minutes - integer >= 1. If the cached data is older than this value, fresh data will be fetched. If null, uses server default cache policy.",
    )
    # Filters are passed as key-value pairs (vf_<fieldname>=value in Tableau API)
    filters: dict[str, str] | None = Field(
        None,
        description="View filters as field_name: value pairs - dictionary where keys are exact field names from the view (case-sensitive) and values are filter values as strings. Example: {'Region': 'West', 'Year': '2024', 'Category': 'Electronics'}. Multiple values for same field use comma separation: {'Region': 'West,East'}. If null or empty dict, no filters applied.",
    )


class TableauQueryViewDataOutput(BaseModel):
    """Output specification for view data query."""

    view_id: str = Field(
        ...,
        description="View identifier - UUID v4 format (36-character string)",
    )
    csv_data: str = Field(
        ...,
        description="CSV formatted data - string containing comma-separated values with header row followed by data rows",
    )
    row_count: int = Field(
        ...,
        description="Number of data rows (excluding header) - integer >= 0",
    )


class TableauQueryViewDataToFileOutput(BaseModel):
    """Output specification for view data query written to file."""

    view_id: str = Field(
        ...,
        description="View identifier - UUID v4 format (36-character string)",
    )
    file_path: str = Field(
        ...,
        description="Absolute path to the CSV file - full filesystem path where the exported CSV data was written",
    )
    row_count: int = Field(
        ...,
        description="Number of data rows (excluding header) - integer >= 0",
    )


class TableauQueryViewImageInput(SiteAwareModel):
    """Input specification for querying view image."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string). Used to validate view belongs to this site.",
    )
    view_id: str = Field(
        ...,
        description="View identifier to capture - UUID v4 format (36-character string). Works with worksheet, dashboard, and story type views.",
    )
    resolution: str = Field(
        default="standard",
        description="Image resolution - must be 'standard' (default, ~800px width) or 'high' (~1600px width, 2x pixel density). Higher resolution produces larger file size.",
    )
    max_age: int | None = Field(
        None,
        ge=1,
        description="Maximum age of cached image in minutes - integer >= 1. If the cached image is older than this value, a fresh image will be rendered. If null, uses server default cache policy.",
    )
    filters: dict[str, str] | None = Field(
        None,
        description="View filters as field_name: value pairs - dictionary where keys are exact field names from the view (case-sensitive) and values are filter values as strings. Example: {'Region': 'West', 'Year': '2024'}. If null or empty dict, no filters applied.",
    )


class TableauQueryViewImageOutput(BaseModel):
    """Output specification for view image query."""

    view_id: str = Field(
        ...,
        description="View identifier - UUID v4 format (36-character string)",
    )
    image_data_base64: str = Field(
        ...,
        description="Base64 encoded PNG image data - string that can be decoded and saved as a PNG file",
    )
    content_type: str = Field(
        default="image/png",
        description="MIME type of the image - typically 'image/png'",
    )


# ============================================================================
# VIEW METADATA MODELS
# Mimics Tableau Metadata API: https://help.tableau.com/current/api/metadata_api/en-us/reference/
# ============================================================================


class FieldDataType(str, Enum):
    """Tableau field data types.

    Reference: https://help.tableau.com/current/api/metadata_api/en-us/reference/fielddatatype.doc.html
    """

    INTEGER = "INTEGER"
    REAL = "REAL"
    STRING = "STRING"
    DATETIME = "DATETIME"
    DATE = "DATE"
    BOOLEAN = "BOOLEAN"
    UNKNOWN = "UNKNOWN"


class FieldRole(str, Enum):
    """Tableau field roles (dimension vs measure).

    Reference: https://help.tableau.com/current/api/metadata_api/en-us/reference/fieldrole.doc.html
    """

    DIMENSION = "DIMENSION"
    MEASURE = "MEASURE"
    UNKNOWN = "UNKNOWN"


class TableauFieldMetadata(BaseModel):
    """Metadata for a single field in a view."""

    name: str = Field(
        ...,
        description="Field/column name - the exact name as it appears in the data source",
    )
    data_type: FieldDataType = Field(
        ...,
        description="Inferred data type - one of: INTEGER, REAL, STRING, DATETIME, DATE, BOOLEAN, or UNKNOWN. Reference: FieldDataType enum.",
    )
    role: FieldRole = Field(
        ...,
        description="Field role - one of: 'DIMENSION' (categorical/discrete values for grouping), 'MEASURE' (numeric values for aggregation), or 'UNKNOWN'. Reference: FieldRole enum.",
    )
    nullable: bool = Field(
        ...,
        description="Whether field contains null values - true if any row has null for this field",
    )
    sample_values: list[Any] = Field(
        default_factory=list,
        description="Sample unique values from the field - list of example values for reference. Number of samples controlled by sample_value_limit parameter.",
    )


class TableauGetViewMetadataInput(SiteAwareModel):
    """Input specification for getting view metadata."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string with hyphens, e.g., '550e8400-e29b-41d4-a716-446655440000'). Used to validate view belongs to this site.",
    )
    view_id: str = Field(
        ...,
        description="View identifier - UUID v4 format (36-character string). Must exist in the views table.",
    )
    include_sample_values: bool = Field(
        default=True,
        description="Include sample values for each field - boolean. If true, populates sample_values list in field metadata. If false, sample_values will be empty.",
    )
    sample_value_limit: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum number of sample values per field - integer between 1 and 20. Only used if include_sample_values is true.",
    )


class TableauGetViewMetadataOutput(BaseModel):
    """Output specification for view metadata.

    Returns field-level metadata including column names, inferred data types,
    and roles (dimension vs measure), mimicking Tableau's Metadata API behavior.
    """

    view_id: str = Field(
        ...,
        description="View identifier - UUID v4 format (36-character string)",
    )
    view_name: str = Field(
        ...,
        description="View name - human-readable name of the view",
    )
    workbook_id: str = Field(
        ...,
        description="Parent workbook identifier - UUID v4 format (36-character string)",
    )
    sheet_type: str = Field(
        ...,
        description="Type of sheet - 'worksheet', 'dashboard', or 'story'. Reference: VALID_SHEET_TYPES constant.",
    )
    row_count: int = Field(
        ...,
        description="Total number of data rows in the view - integer >= 0",
    )
    fields: list[TableauFieldMetadata] = Field(
        ...,
        description="List of field metadata for all columns - each entry contains name, data_type, role, nullable, and sample_values",
    )


# ============================================================================
# WORKBOOK PUBLISH MODELS
# ============================================================================


class TableauPublishWorkbookInput(SiteAwareModel):
    """Input specification for publishing a workbook file (.twb or .twbx).

    This tool uploads a complete workbook file and automatically creates
    all views (worksheets, dashboards, stories) contained in the workbook.

    File Types:
        - .twb: Unpackaged workbook (XML only, requires external data connections)
        - .twbx: Packaged workbook (includes embedded data extracts and visualizations)
            Supports modern Hyper (.hyper) data extracts, legacy Excel (.xlsx, .xls),
            and CSV files bundled within the package.

    Two ways to provide the file:
        1. file_path: Local filesystem path to the .twb/.twbx file (recommended)
        2. file_content_base64: Base64-encoded file content (for API uploads)

    If both are provided, file_path takes precedence.
    """

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format. Must exist in sites table.",
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Workbook name - 1-255 characters, human-readable identifier.",
    )
    project_id: str = Field(
        ...,
        description="Project identifier where workbook will be published - UUID v4 format.",
    )
    file_path: str | None = Field(
        None,
        description="Filename of the task input file to publish (e.g., 'Mart_Sales.twbx'). IMPORTANT: Use ONLY the filename exactly as provided in the task - do NOT add path prefixes like '/data/' or '/'. The system automatically locates files in the correct directory. Takes precedence over file_content_base64.",
    )
    file_content_base64: str | None = Field(
        None,
        description="Upload a .twb or .twbx file from your computer. The file will be base64-encoded automatically. Use this to upload local workbook files via the UI.",
    )
    file_name: str | None = Field(
        None,
        description="Original filename with extension (e.g., 'dashboard.twbx'). Auto-populated when uploading a file, or auto-detected from file_path.",
    )
    description: str = Field(
        default="",
        description="Optional workbook description.",
    )
    show_tabs: bool = Field(
        default=True,
        description="Show worksheet tabs in published workbook.",
    )
    overwrite: bool = Field(
        default=False,
        description="Overwrite if workbook with same name exists in project.",
    )
    owner_id: str | None = Field(
        None,
        description="Owner UUID. If not provided, uses the first user in the site.",
    )


class TableauPublishWorkbookOutput(BaseModel):
    """Output specification for workbook publishing.

    Returns the created workbook details along with the list of view IDs
    that were automatically created from the workbook file.
    """

    id: str = Field(
        ...,
        description="Created workbook identifier - UUID v4 format (36-character string)",
    )
    name: str = Field(
        ...,
        description="Workbook name - as provided in input",
    )
    project_id: str = Field(
        ...,
        description="Project identifier - UUID v4 format (36-character string)",
    )
    owner_id: str = Field(
        ...,
        description="Owner user identifier - UUID v4 format (36-character string)",
    )
    description: str = Field(
        default="",
        description="Workbook description - as provided in input or empty string",
    )
    content_url: str | None = Field(
        None,
        description="URL path to workbook (e.g., 'workbooks/{id}') - may be null if not set",
    )
    created_at: str = Field(
        ...,
        description="Creation timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )
    updated_at: str = Field(
        ...,
        description="Last update timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )
    views: list[str] = Field(
        default_factory=list,
        description="List of view identifiers (UUID v4 format strings) that were automatically created from the workbook file",
    )


# ============================================================================
# VISUALIZATION TOOLS - Drag-and-Drop Query Builder
# ============================================================================


class ShelfMeasure(BaseModel):
    """A measure field with its aggregation function."""

    field: str = Field(
        ...,
        description="Column name from the datasource table - must match an existing column name exactly (case-sensitive)",
    )
    aggregation: str = Field(
        default="SUM",
        description="Aggregation function for the measure - must be one of: 'SUM' (total, default), 'AVG' (arithmetic mean), 'COUNT' (row count, works with any field), 'MIN' (minimum value), 'MAX' (maximum value), or 'COUNT_DISTINCT' (unique value count). For numeric fields use SUM/AVG/MIN/MAX. For counting records use COUNT. For counting unique values use COUNT_DISTINCT.",
    )


class ShelfFilter(BaseModel):
    """A filter condition for the visualization query."""

    field: str = Field(
        ...,
        description="Column name to filter on - must match an existing column name exactly (case-sensitive)",
    )
    op: str = Field(
        default="=",
        description="Comparison operator - must be one of: '=' (equals), '!=' (not equals), '>' (greater than), '<' (less than), '>=' (greater than or equal), '<=' (less than or equal), 'IN' (value in list), 'NOT IN' (value not in list), 'LIKE' (pattern match with % wildcard). Default is '='.",
    )
    value: Any = Field(
        ...,
        description="Filter value - string, number, or list (for IN/NOT IN operators). Examples: 'West', 2024, ['East', 'West'], '%sales%' (for LIKE).",
    )


class ShelfConfig(BaseModel):
    """Configuration for visualization shelves (drag-and-drop state)."""

    datasource_id: str = Field(
        ...,
        description="Datasource identifier - UUID v4 format (36-character string). References the datasource whose table will be queried.",
    )
    rows: list[str] = Field(
        default_factory=list,
        description="Dimension fields on the Rows shelf - list of column names (strings) that define row groupings. These create the vertical axis categories.",
    )
    columns: list[str] = Field(
        default_factory=list,
        description="Dimension fields on the Columns shelf - list of column names (strings) that define column groupings. These create the horizontal axis categories.",
    )
    measures: list[ShelfMeasure] = Field(
        default_factory=list,
        description="Measure fields with aggregations - list of ShelfMeasure objects, each specifying a field and its aggregation function (SUM, AVG, COUNT, etc.).",
    )
    filters: list[ShelfFilter] = Field(
        default_factory=list,
        description="Filter conditions - list of ShelfFilter objects to restrict the data before visualization.",
    )
    mark_type: str = Field(
        default="bar",
        description="Chart visualization type - must be one of: 'bar' (vertical bar chart, default), 'line' (line chart for trends over time), 'table' (tabular text display), 'area' (filled area chart), 'scatter' (scatter plot for correlation), or 'pie' (pie chart for proportions, requires exactly one measure). Choice should match the data being visualized.",
    )
    color: str | None = Field(
        None,
        description="Optional field name for color encoding - column name whose values determine mark colors. Creates a color legend. If null, uses default single color.",
    )
    size: str | None = Field(
        None,
        description="Optional field name for size encoding - column name whose values determine mark sizes. Typically a measure field. If null, uses uniform size.",
    )
    label: str | None = Field(
        None,
        description="Optional field name for label encoding - column name whose values are displayed as text labels on marks. If null, no labels shown.",
    )
    sort_field: str | None = Field(
        None,
        description="Optional field to sort results by - column name for ordering. If null, results are unsorted or use database default order.",
    )
    sort_order: str = Field(
        default="ASC",
        description="Sort order - must be 'ASC' (ascending, A-Z, 0-9) or 'DESC' (descending, Z-A, 9-0). Only applies if sort_field is specified.",
    )
    limit: int | None = Field(
        None,
        ge=1,
        le=10000,
        description="Maximum rows to return - integer between 1 and 10000. If null, returns all rows (may be large for big datasets).",
    )


# --- tableau_upload_csv ---


class TableauUploadCsvInput(SiteAwareModel):
    """Upload CSV data and create a queryable datasource."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string with hyphens, e.g., '550e8400-e29b-41d4-a716-446655440000')",
    )
    project_id: str = Field(
        ...,
        description="Project identifier - UUID v4 format (36-character string). Must reference an existing project.",
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Datasource name - 1-255 characters, human-readable identifier for the created datasource",
    )
    csv_content: str | None = Field(
        None,
        description="CSV content as plain text or base64-encoded string. Provide either csv_content or file_content_base64, not both.",
    )
    file_content_base64: str | None = Field(
        None,
        description="Base64-encoded CSV file content - for UI file upload. Provide either csv_content or file_content_base64, not both.",
    )
    owner_id: str | None = Field(
        None,
        description="Owner user identifier - UUID v4 format. Optional; if not provided, uses the first user in the site.",
    )


class TableauUploadCsvFieldInfo(BaseModel):
    """Information about a field in the uploaded CSV."""

    name: str = Field(
        ...,
        description="Column name (sanitized) - cleaned version of the CSV header",
    )
    data_type: str = Field(
        ...,
        description="Inferred data type - one of: 'STRING', 'INTEGER', 'REAL', 'DATE', 'DATETIME'",
    )
    role: str = Field(
        ...,
        description="Field role - 'DIMENSION' (categorical/discrete) or 'MEASURE' (numeric for aggregation)",
    )


class TableauUploadCsvOutput(BaseModel):
    """Result of CSV upload."""

    datasource_id: str = Field(
        ...,
        description="Created datasource identifier - UUID v4 format (36-character string)",
    )
    table_name: str = Field(
        ...,
        description="SQLite table name for the imported data - internal table identifier",
    )
    name: str = Field(
        ...,
        description="Datasource display name - as provided in input",
    )
    fields: list[TableauUploadCsvFieldInfo] = Field(
        ...,
        description="List of fields with types and roles - one entry per CSV column",
    )
    row_count: int = Field(
        ...,
        description="Number of rows imported - integer >= 0 (excluding header row)",
    )
    message: str = Field(
        ...,
        description="Success message - human-readable confirmation of the upload operation",
    )


# --- tableau_get_sheets ---


class TableauGetSheetsInput(SiteAwareModel):
    """Get sheets (views) with their shelf configurations."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string with hyphens, e.g., '550e8400-e29b-41d4-a716-446655440000')",
    )
    workbook_id: str | None = Field(
        None,
        description="Optional workbook filter - UUID v4 format. If provided, returns only sheets from this workbook. If null, returns sheets from all workbooks.",
    )


class TableauSheetInfo(BaseModel):
    """Information about a sheet including its shelf configuration."""

    id: str = Field(
        ...,
        description="View identifier - UUID v4 format (36-character string)",
    )
    workbook_id: str = Field(
        ...,
        description="Parent workbook identifier - UUID v4 format (36-character string)",
    )
    name: str = Field(
        ...,
        description="Sheet name - human-readable identifier",
    )
    sheet_type: str = Field(
        ...,
        description="Type of sheet - 'worksheet', 'dashboard', or 'story'. Reference: VALID_SHEET_TYPES constant.",
    )
    datasource_id: str | None = Field(
        None,
        description="Associated datasource identifier - UUID v4 format, or null if no datasource linked",
    )
    shelf_config: ShelfConfig | None = Field(
        None,
        description="Current shelf configuration - contains rows, columns, measures, filters, mark_type, etc. Null if not configured.",
    )
    created_at: str = Field(
        ...,
        description="Creation timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )
    updated_at: str = Field(
        ...,
        description="Last update timestamp - ISO 8601 format with UTC timezone (e.g., '2024-01-15T10:30:45.123Z')",
    )


class TableauGetSheetsOutput(BaseModel):
    """Result of getting sheets."""

    sheets: list[TableauSheetInfo] = Field(
        ...,
        description="List of sheet objects matching the query. Each contains full sheet details including shelf configuration.",
    )
    total_count: int = Field(
        ...,
        description="Total number of sheets returned - integer >= 0",
    )


# --- tableau_list_fields ---


class TableauListFieldsInput(SiteAwareModel):
    """List fields from a datasource's underlying data table."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string with hyphens, e.g., '550e8400-e29b-41d4-a716-446655440000')",
    )
    datasource_id: str = Field(
        ...,
        description="Datasource identifier - UUID v4 format (36-character string). Must reference an existing datasource.",
    )


class TableauFieldInfo(BaseModel):
    """Detailed information about a field in the datasource."""

    name: str = Field(
        ...,
        description="Column name - exact name as it appears in the data table",
    )
    data_type: str = Field(
        ...,
        description="Data type - one of: 'STRING', 'INTEGER', 'REAL', 'DATE', 'DATETIME', 'BOOLEAN'",
    )
    role: str = Field(
        ...,
        description="Field role - 'DIMENSION' (categorical/discrete for grouping) or 'MEASURE' (numeric for aggregation)",
    )
    nullable: bool = Field(
        ...,
        description="Whether field contains null values - true if any row has null for this field",
    )
    distinct_count: int = Field(
        ...,
        description="Number of distinct/unique values in this field - integer >= 0",
    )
    sample_values: list[Any] = Field(
        default_factory=list,
        description="Sample unique values (up to 10) - list of example values for reference",
    )


class TableauListFieldsOutput(BaseModel):
    """Result of listing fields."""

    datasource_id: str = Field(
        ...,
        description="Datasource identifier - UUID v4 format (36-character string)",
    )
    table_name: str = Field(
        ...,
        description="SQLite table name - internal table identifier for the datasource data",
    )
    fields: list[TableauFieldInfo] = Field(
        ...,
        description="List of field metadata - one entry per column in the datasource",
    )
    row_count: int = Field(
        ...,
        description="Total rows in the table - integer >= 0",
    )


# --- tableau_configure_shelf ---


class TableauConfigureShelfInput(SiteAwareModel):
    """Configure the shelf layout for a view (drag-and-drop state)."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string with hyphens, e.g., '550e8400-e29b-41d4-a716-446655440000')",
    )
    view_id: str = Field(
        ...,
        description="View identifier to configure - UUID v4 format (36-character string). Must exist in the views table.",
    )
    shelf_config: ShelfConfig = Field(
        ...,
        description="Shelf configuration to apply - object with datasource_id, rows, columns, measures, filters, mark_type, and optional color/size/label/sort settings",
    )


class TableauConfigureShelfOutput(BaseModel):
    """Result of configuring shelves."""

    view_id: str = Field(
        ...,
        description="View identifier - UUID v4 format (36-character string)",
    )
    shelf_config: ShelfConfig = Field(
        ...,
        description="Applied shelf configuration - the configuration that was saved to the view",
    )
    generated_sql: str = Field(
        ...,
        description="SQL query that would be generated - preview of the query that will execute when creating visualization",
    )
    message: str = Field(
        ...,
        description="Status message - human-readable confirmation of the operation",
    )


# --- tableau_create_visualization ---


class TableauCreateVisualizationInput(SiteAwareModel):
    """Generate a visualization from the view's shelf configuration."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string with hyphens, e.g., '550e8400-e29b-41d4-a716-446655440000')",
    )
    view_id: str = Field(
        ...,
        description="View identifier with configured shelves - UUID v4 format (36-character string). Must have a shelf_config already set via configure_shelf.",
    )
    width: int = Field(
        default=800,
        ge=200,
        le=2000,
        description="Image width in pixels - integer between 200 and 2000. Default is 800.",
    )
    height: int = Field(
        default=500,
        ge=200,
        le=2000,
        description="Image height in pixels - integer between 200 and 2000. Default is 500.",
    )
    format: str = Field(
        default="png",
        description="Output image format - must be 'png' (raster image, default) or 'svg' (vector image, scalable).",
    )


class TableauVisualizationData(BaseModel):
    """Structured query result data."""

    headers: list[str] = Field(
        ...,
        description="Column headers - list of column names from the query result",
    )
    rows: list[list[Any]] = Field(
        ...,
        description="Data rows - list of lists, each inner list contains values for one row in column order",
    )
    row_count: int = Field(
        ...,
        description="Number of result rows - integer >= 0",
    )


class TableauCreateVisualizationOutput(BaseModel):
    """Result of creating a visualization."""

    view_id: str = Field(
        ...,
        description="View identifier - UUID v4 format (36-character string)",
    )
    chart_type: str = Field(
        ...,
        description="Chart type rendered - one of: 'bar', 'line', 'table', 'area', 'scatter', 'pie'. Matches the mark_type from shelf_config.",
    )
    generated_sql: str = Field(
        ...,
        description="SQL query that was executed - the actual query run against the datasource",
    )
    data: TableauVisualizationData = Field(
        ...,
        description="Query result data - structured data with headers and rows",
    )
    image_base64: str | None = Field(
        None,
        description="Base64-encoded chart image (PNG or SVG) - can be decoded and displayed as an image. Null in LLM mode to reduce token usage.",
    )
    content_type: str = Field(
        default="image/png",
        description="Image MIME type - 'image/png' or 'image/svg+xml' depending on format requested",
    )
    message: str = Field(
        ...,
        description="Status message - human-readable confirmation of the operation",
    )


# --- tableau_create_sheet ---


class TableauCreateSheetInput(SiteAwareModel):
    """Create a new sheet (View + Workbook) linked to a datasource."""

    site_id: str = Field(
        ...,
        description="Site identifier - UUID v4 format (36-character string with hyphens, e.g., '550e8400-e29b-41d4-a716-446655440000')",
    )
    datasource_id: str = Field(
        ...,
        description="Datasource identifier to link the sheet to - UUID v4 format (36-character string). Must reference an existing datasource.",
    )
    name: str = Field(
        default="Sheet 1",
        description="Sheet display name - 1-255 characters. Defaults to 'Sheet 1'.",
    )


class TableauCreateSheetOutput(BaseModel):
    """Result of creating a sheet."""

    view_id: str = Field(
        ...,
        description="Created view identifier - UUID v4 format (36-character string). Use this ID for subsequent configure_shelf and create_visualization calls.",
    )
    workbook_id: str = Field(
        ...,
        description="Created workbook identifier - UUID v4 format (36-character string). A new workbook is created to hold this sheet.",
    )
    name: str = Field(
        ...,
        description="Sheet name - as provided in input or 'Sheet 1' if default",
    )
    datasource_id: str = Field(
        ...,
        description="Linked datasource identifier - UUID v4 format (36-character string). Echo of the input datasource_id.",
    )
    message: str = Field(
        ...,
        description="Status message - human-readable confirmation of the operation",
    )
