"""Tableau Meta-Tools for LLM Context Optimization.

Consolidates 39 individual tools into 8 domain-based meta-tools.
Each meta-tool supports action="help" for discovery.

Meta-tools:
- tableau_admin: Sites and permissions management
- tableau_users: User CRUD operations
- tableau_projects: Project CRUD operations
- tableau_workbooks: Workbook CRUD, publish, and connections
- tableau_views: View queries and image export
- tableau_datasources: Datasource CRUD operations
- tableau_groups: Group and membership management
- tableau_schema: Tool introspection
"""

import sys
from pathlib import Path
from typing import Any, Literal

from fastmcp.utilities.types import Image

# Add parent directory to path for relative imports (consistent with other tool files)
sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_schema import GeminiBaseModel as BaseModel
from models import (
    ShelfConfig,
    TableauAddUserToGroupInput,
    TableauConfigureShelfInput,
    TableauCreateDatasourceInput,
    TableauCreateGroupInput,
    TableauCreateProjectInput,
    TableauCreateSheetInput,
    TableauCreateUserInput,
    TableauCreateVisualizationInput,
    TableauCreateWorkbookConnectionInput,
    TableauCreateWorkbookInput,
    TableauDeleteDatasourceInput,
    TableauDeleteProjectInput,
    TableauDeleteUserInput,
    TableauDeleteWorkbookConnectionInput,
    TableauDeleteWorkbookInput,
    TableauGetDatasourceInput,
    TableauGetProjectInput,
    TableauGetSheetsInput,
    TableauGetUserInput,
    TableauGetViewInput,
    TableauGetViewMetadataInput,
    TableauGetWorkbookInput,
    TableauGrantPermissionInput,
    TableauListDatasourcesInput,
    TableauListFieldsInput,
    TableauListGroupsInput,
    TableauListPermissionsInput,
    TableauListProjectsInput,
    TableauListSitesInput,
    TableauListUsersInput,
    TableauListViewsInput,
    TableauListWorkbookConnectionsInput,
    TableauListWorkbooksInput,
    TableauPublishWorkbookInput,
    TableauQueryViewDataInput,
    TableauQueryViewImageInput,
    TableauRemoveUserFromGroupInput,
    TableauRevokePermissionInput,
    TableauUpdateDatasourceInput,
    TableauUpdateProjectInput,
    TableauUpdateUserInput,
    TableauUpdateWorkbookInput,
    TableauUploadCsvInput,
)
from pydantic import Field

# Import the underlying tool implementations
from tools.connection_tools import (
    tableau_create_workbook_connection,
    tableau_delete_workbook_connection,
    tableau_list_workbook_connections,
)
from tools.datasource_tools import (
    tableau_create_datasource,
    tableau_delete_datasource,
    tableau_get_datasource,
    tableau_list_datasources,
    tableau_update_datasource,
)
from tools.group_tools import (
    tableau_add_user_to_group,
    tableau_create_group,
    tableau_list_groups,
    tableau_remove_user_from_group,
)
from tools.permission_tools import (
    tableau_grant_permission,
    tableau_list_permissions,
    tableau_revoke_permission,
)
from tools.project_tools import (
    tableau_create_project,
    tableau_delete_project,
    tableau_get_project,
    tableau_list_projects,
    tableau_update_project,
)
from tools.publish_workbook_tools import tableau_publish_workbook
from tools.site_tools import tableau_list_sites
from tools.user_tools import (
    tableau_create_user,
    tableau_delete_user,
    tableau_get_user,
    tableau_list_users,
    tableau_update_user,
)
from tools.view_tools import (
    tableau_get_view,
    tableau_get_view_metadata,
    tableau_list_views,
    tableau_query_view_data_to_file,
    tableau_query_view_image,
)
from tools.visualization_tools import (
    tableau_configure_shelf,
    tableau_create_sheet,
    tableau_create_visualization,
    tableau_get_sheets,
    tableau_list_fields,
    tableau_upload_csv,
)
from tools.workbook_tools import (
    tableau_create_workbook,
    tableau_delete_workbook,
    tableau_get_workbook,
    tableau_list_workbooks,
    tableau_update_workbook,
)

# =============================================================================
# HELP RESPONSE MODEL
# =============================================================================


class HelpResponse(BaseModel):
    """Standard help response for all tools."""

    tool_name: str
    description: str
    actions: dict[str, dict[str, Any]]


# =============================================================================
# META TOOL INPUT MODELS
# =============================================================================


class AdminInput(BaseModel):
    """Input for admin meta tool (sites + permissions)."""

    action: Literal[
        "help", "list_sites", "grant_permission", "list_permissions", "revoke_permission"
    ] = Field(
        ...,
        description="Action to perform: 'help' (show available actions), 'list_sites' (paginated site list), 'grant_permission', 'list_permissions', or 'revoke_permission'",
    )
    # For list_sites
    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number for pagination - integer starting at 1 (first page). Used by list_sites action.",
    )
    page_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Number of items per page - integer between 1 and 1000. Used by list_sites action.",
    )
    # For permissions (grant/list/revoke)
    site_id: str | None = Field(
        None,
        description="Site identifier - UUID v4 format (36-character string, e.g., '550e8400-e29b-41d4-a716-446655440000'). Required for all permission actions.",
    )
    resource_type: str | None = Field(
        None,
        description="Resource type for permissions - must be exactly one of: 'project', 'workbook', or 'datasource'. Required for grant/list/revoke permission actions.",
    )
    resource_id: str | None = Field(
        None,
        description="Resource identifier - UUID v4 format (36-character string). Must reference an existing project, workbook, or datasource (matching resource_type). Required for all permission actions.",
    )
    grantee_type: str | None = Field(
        None,
        description="Grantee type - must be exactly 'user' or 'group'. Required for grant/revoke permission actions.",
    )
    grantee_id: str | None = Field(
        None,
        description="Grantee identifier - UUID v4 format (36-character string). Must reference an existing user (if grantee_type='user') or group (if grantee_type='group'). Required for grant/revoke permission actions.",
    )
    capability: str | None = Field(
        None,
        description="Permission capability - must be exactly one of: 'Read' (view the resource), 'Write' (edit/modify the resource), or 'ChangePermissions' (modify permissions). Required for grant/revoke permission actions.",
    )
    mode: str | None = Field(
        None,
        description="Permission mode - must be exactly 'Allow' (grant permission) or 'Deny' (explicitly deny, overrides Allow). Required for grant/revoke permission actions.",
    )


class UsersInput(BaseModel):
    """Input for users meta tool."""

    action: Literal["help", "create", "list", "get", "update", "delete"] = Field(
        ...,
        description="Action to perform: 'help' (show available actions), 'create' (new user), 'list' (paginated user list), 'get' (single user details), 'update' (modify user), 'delete' (remove user)",
    )
    site_id: str | None = Field(
        None,
        description="Site identifier - UUID v4 format (36-character string, e.g., '550e8400-e29b-41d4-a716-446655440000'). Required for all actions except 'help'.",
    )
    user_id: str | None = Field(
        None,
        description="User identifier - UUID v4 format (36-character string). Required for get/update/delete actions.",
    )
    name: str | None = Field(
        None,
        description="Username - 1-255 characters, must be unique within the site. Required for create action.",
    )
    email: str | None = Field(
        None,
        description="Email address - valid email format. Optional for create; can be updated via update action. Only supported in Tableau Cloud API 3.26+.",
    )
    site_role: str | None = Field(
        None,
        description="Site role - must be one of: 'Creator', 'Explorer', 'ExplorerCanPublish', 'SiteAdministratorExplorer', 'SiteAdministratorCreator', 'Unlicensed', 'ReadOnly', 'Viewer'. Required for create action.",
    )
    full_name: str | None = Field(
        None,
        description="User's full display name - string (e.g., 'John Smith'). Optional for create/update.",
    )
    password: str | None = Field(
        None,
        description="Account password - string meeting password requirements. For local authentication only. Optional for create/update.",
    )
    auth_setting: str | None = Field(
        None,
        description="Authentication method - string specifying how user authenticates (e.g., 'ServerDefault', 'SAML', 'OpenID'). Optional for update.",
    )
    map_assets_to: str | None = Field(
        None,
        description="Target user identifier for content transfer on delete - UUID v4 format. If user owns content, this specifies who receives it. Required if deleting a user who owns content.",
    )
    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number for pagination - integer starting at 1. Used by list action.",
    )
    page_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Number of items per page - integer between 1 and 1000. Used by list action.",
    )


class ProjectsInput(BaseModel):
    """Input for projects meta tool."""

    action: Literal["help", "create", "list", "get", "update", "delete"] = Field(
        ...,
        description="Action to perform: 'help' (show available actions), 'create' (new project), 'list' (paginated project list), 'get' (single project details), 'update' (modify project), 'delete' (remove project and cascade to children)",
    )
    site_id: str | None = Field(
        None,
        description="Site identifier - UUID v4 format (36-character string, e.g., '550e8400-e29b-41d4-a716-446655440000'). Required for all actions except 'help'.",
    )
    project_id: str | None = Field(
        None,
        description="Project identifier - UUID v4 format (36-character string). Required for get/update/delete actions.",
    )
    name: str | None = Field(
        None,
        description="Project name - 1-255 characters. Required for create action. Optional for update (if provided, renames the project).",
    )
    description: str | None = Field(
        None,
        description="Project description - free-text field to describe purpose or content. Optional for create/update.",
    )
    parent_project_id: str | None = Field(
        None,
        description="Parent project identifier - UUID v4 format for nested project hierarchy. Optional for create (null = top-level project). Cannot be changed via update.",
    )
    owner_id: str | None = Field(
        None,
        description="Owner user identifier - UUID v4 format. Must reference an existing user. Required for create action.",
    )
    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number for pagination - integer starting at 1. Used by list action.",
    )
    page_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Number of items per page - integer between 1 and 1000. Used by list action.",
    )


class WorkbooksInput(BaseModel):
    """Input for workbooks meta tool (includes connections)."""

    action: Literal[
        "help",
        "create",
        "list",
        "get",
        "update",
        "delete",
        "publish",
        "connect",
        "list_connections",
        "disconnect",
    ] = Field(
        ...,
        description="Action to perform: 'help' (show available actions), 'create' (new workbook), 'list' (paginated workbook list), 'get' (single workbook details), 'update' (modify workbook), 'delete' (remove workbook and its views), 'publish' (upload .twb/.twbx file), 'connect' (link workbook to datasource), 'list_connections' (show datasource connections), 'disconnect' (remove workbook-datasource link)",
    )
    site_id: str | None = Field(
        None,
        description="Site identifier - UUID v4 format (36-character string, e.g., '550e8400-e29b-41d4-a716-446655440000'). Required for all actions except 'help'.",
    )
    workbook_id: str | None = Field(
        None,
        description="Workbook identifier - UUID v4 format (36-character string). Required for get/update/delete/connect/list_connections/disconnect actions.",
    )
    # For create/update/publish
    name: str | None = Field(
        None,
        description="Workbook name - 1-255 characters. Required for create/publish actions. Optional for update (if provided, renames the workbook).",
    )
    description: str | None = Field(
        None,
        description="Workbook description - free-text field to describe purpose or content. Optional for create/update/publish.",
    )
    project_id: str | None = Field(
        None,
        description="Project identifier - UUID v4 format. Must reference an existing project. Required for create/publish actions.",
    )
    owner_id: str | None = Field(
        None,
        description="Owner user identifier - UUID v4 format. Must reference an existing user. Required for create action. Optional for publish (defaults to first user in site).",
    )
    file_reference: str | None = Field(
        None,
        description="Reference to existing workbook file - string path or URI (for create action). Use file_path for publish action instead.",
    )
    # For publish
    file_path: str | None = Field(
        None,
        description="Filename of the task input file to publish (e.g., 'Mart_Sales.twbx'). For publish action. Mutually exclusive with file_content_base64. Takes precedence if both provided.",
    )
    file_content_base64: str | None = Field(
        None,
        description="Base64-encoded file content for upload - string containing base64-encoded .twb/.twbx file. For publish action via API.",
    )
    file_name: str | None = Field(
        None,
        description="Original filename with extension (e.g., 'dashboard.twbx'). Auto-detected from file_path if not provided.",
    )
    show_tabs: bool = Field(
        default=True,
        description="Show worksheet tabs in published workbook - boolean. If true, users see tab navigation between sheets.",
    )
    overwrite: bool = Field(
        default=False,
        description="Overwrite existing workbook - boolean. If true and workbook with same name exists in project, replaces it. If false, fails on name conflict.",
    )
    # For connect/disconnect (connections)
    datasource_id: str | None = Field(
        None,
        description="Datasource identifier - UUID v4 format. Must reference an existing datasource. Required for connect action.",
    )
    connection_id: str | None = Field(
        None,
        description="Connection identifier - UUID v4 format. Must reference an existing workbook-datasource connection. Required for disconnect action.",
    )
    # For list
    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number for pagination - integer starting at 1. Used by list action.",
    )
    page_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Number of items per page - integer between 1 and 1000. Used by list action.",
    )


class ViewsInput(BaseModel):
    """Input for views meta tool."""

    action: Literal["help", "list", "get", "metadata", "query_to_file", "image"] = Field(
        ...,
        description="Action to perform: 'help' (show available actions), 'list' (paginated view list), 'get' (single view details), 'metadata' (field metadata for a view), 'query_to_file' (export view data to CSV file), 'image' (export view as PNG image)",
    )
    site_id: str | None = Field(
        None,
        description="Site identifier - UUID v4 format (36-character string, e.g., '550e8400-e29b-41d4-a716-446655440000'). Required for all actions except 'help'.",
    )
    view_id: str | None = Field(
        None,
        description="View identifier - UUID v4 format (36-character string). Required for get/metadata/query_to_file/image actions.",
    )
    workbook_id: str | None = Field(
        None,
        description="Optional workbook filter - UUID v4 format. If provided, list action returns only views from this workbook.",
    )
    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number for pagination - integer starting at 1. Used by list action.",
    )
    page_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Number of items per page - integer between 1 and 1000. Used by list action.",
    )
    max_age: int | None = Field(
        None,
        ge=1,
        description="Maximum age of cached data/image in minutes - integer >= 1. If cached data is older, fresh data is fetched. Used by query_to_file/image actions.",
    )
    filters: dict[str, str] | None = Field(
        None,
        description="View filters as field_name: value pairs - dictionary where keys are exact field names (case-sensitive) and values are filter values. Example: {'Region': 'West', 'Year': '2024'}. Multiple values use comma: {'Region': 'West,East'}. Used by query_to_file/image actions.",
    )
    include_sample_values: bool = Field(
        default=True,
        description="Include sample values in metadata - boolean. If true, returns sample unique values for each field. Used by metadata action.",
    )
    sample_value_limit: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum sample values per field - integer between 1 and 20. Used by metadata action when include_sample_values is true.",
    )
    resolution: str = Field(
        default="standard",
        description="Image resolution - must be 'standard' (~800px width) or 'high' (~1600px width, 2x pixel density). Used by image action.",
    )


class DatasourcesInput(BaseModel):
    """Input for datasources meta tool."""

    action: Literal["help", "create", "list", "get", "update", "delete"] = Field(
        ...,
        description="Action to perform: 'help' (show available actions), 'create' (new datasource), 'list' (paginated datasource list), 'get' (single datasource details), 'update' (modify datasource), 'delete' (remove datasource)",
    )
    site_id: str | None = Field(
        None,
        description="Site identifier - UUID v4 format (36-character string, e.g., '550e8400-e29b-41d4-a716-446655440000'). Required for all actions except 'help'.",
    )
    datasource_id: str | None = Field(
        None,
        description="Datasource identifier - UUID v4 format (36-character string). Required for get/update/delete actions.",
    )
    name: str | None = Field(
        None,
        description="Datasource name - 1-255 characters. Required for create action. Optional for update (if provided, renames the datasource).",
    )
    description: str | None = Field(
        None,
        description="Datasource description - free-text field to describe purpose or content. Optional for create/update.",
    )
    project_id: str | None = Field(
        None,
        description="Project identifier - UUID v4 format. Must reference an existing project. Required for create action.",
    )
    owner_id: str | None = Field(
        None,
        description="Owner user identifier - UUID v4 format. Must reference an existing user. Required for create action.",
    )
    connection_type: str | None = Field(
        None,
        description="Database/file connection type - string up to 50 characters. Required for create action. Examples: 'postgres', 'mysql', 'sqlserver', 'oracle', 'snowflake', 'bigquery', 'redshift', 'excel', 'csv'. Optional for update.",
    )
    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number for pagination - integer starting at 1. Used by list action.",
    )
    page_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Number of items per page - integer between 1 and 1000. Used by list action.",
    )


class GroupsInput(BaseModel):
    """Input for groups meta tool."""

    action: Literal["help", "create", "list", "add_user", "remove_user"] = Field(
        ...,
        description="Action to perform: 'help' (show available actions), 'create' (new group), 'list' (paginated group list), 'add_user' (add user to group), 'remove_user' (remove user from group)",
    )
    site_id: str | None = Field(
        None,
        description="Site identifier - UUID v4 format (36-character string, e.g., '550e8400-e29b-41d4-a716-446655440000'). Required for all actions except 'help'.",
    )
    group_id: str | None = Field(
        None,
        description="Group identifier - UUID v4 format (36-character string). Required for add_user/remove_user actions.",
    )
    user_id: str | None = Field(
        None,
        description="User identifier - UUID v4 format (36-character string). Required for add_user/remove_user actions.",
    )
    name: str | None = Field(
        None,
        description="Group name - 1-255 characters, must be unique across all sites. Required for create action.",
    )
    description: str | None = Field(
        None,
        description="Group description - free-text field to describe the group's purpose or members. Optional for create.",
    )
    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number for pagination - integer starting at 1. Used by list action.",
    )
    page_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Number of items per page - integer between 1 and 1000. Used by list action.",
    )


class VisualizationInput(BaseModel):
    """Input for visualization meta tool (CSV upload, shelves, chart generation)."""

    action: Literal[
        "help",
        "upload_csv",
        "get_sheets",
        "list_fields",
        "configure_shelf",
        "create_visualization",
        "create_sheet",
    ] = Field(
        ...,
        description="Action to perform: 'help' (show available actions), 'upload_csv' (create datasource from CSV data), 'get_sheets' (list sheets with shelf configs), 'list_fields' (get field metadata from datasource), 'configure_shelf' (set up rows/columns/measures/filters for a view), 'create_visualization' (execute query and render chart), 'create_sheet' (create new workbook+view linked to datasource)",
    )
    site_id: str | None = Field(
        None,
        description="Site identifier - UUID v4 format (36-character string, e.g., '550e8400-e29b-41d4-a716-446655440000'). Required for all actions except 'help'.",
    )
    # For upload_csv
    project_id: str | None = Field(
        None,
        description="Project identifier - UUID v4 format. Required for upload_csv action.",
    )
    name: str | None = Field(
        None,
        description="Datasource/sheet name - 1-255 characters. Required for upload_csv action. Optional for create_sheet (defaults to 'Sheet 1').",
    )
    csv_content: str | None = Field(
        None,
        description="CSV content as plain text or base64-encoded string. Used by upload_csv action. Provide either csv_content or file_content_base64, not both.",
    )
    file_content_base64: str | None = Field(
        None,
        description="Base64-encoded CSV file content - for UI file upload. Used by upload_csv action.",
    )
    owner_id: str | None = Field(
        None,
        description="Owner user identifier - UUID v4 format. Optional for upload_csv (defaults to first user in site).",
    )
    # For get_sheets
    workbook_id: str | None = Field(
        None,
        description="Optional workbook filter - UUID v4 format. If provided, get_sheets returns only sheets from this workbook.",
    )
    # For list_fields
    datasource_id: str | None = Field(
        None,
        description="Datasource identifier - UUID v4 format. Required for list_fields and create_sheet actions.",
    )
    # For configure_shelf / create_visualization
    view_id: str | None = Field(
        None,
        description="View identifier - UUID v4 format. Required for configure_shelf and create_visualization actions.",
    )
    shelf_config: dict[str, Any] | None = Field(
        None,
        description="Shelf configuration object - dictionary with keys: datasource_id, rows (list), columns (list), measures (list of {field, aggregation}), filters (list of {field, op, value}), mark_type, color, size, label, sort_field, sort_order, limit. Required for configure_shelf action.",
    )
    # For create_visualization
    width: int = Field(
        default=800,
        ge=200,
        le=2000,
        description="Image width in pixels - integer between 200 and 2000. Used by create_visualization action.",
    )
    height: int = Field(
        default=500,
        ge=200,
        le=2000,
        description="Image height in pixels - integer between 200 and 2000. Used by create_visualization action.",
    )
    format: str = Field(
        default="png",
        description="Output image format - must be 'png' or 'svg'. Used by create_visualization action.",
    )


class SchemaInput(BaseModel):
    """Input for schema introspection tool."""

    tool: Literal[
        "tableau_admin",
        "tableau_users",
        "tableau_projects",
        "tableau_workbooks",
        "tableau_views",
        "tableau_datasources",
        "tableau_groups",
        "tableau_visualization",
    ] = Field(..., description="Tool name to get schema for")
    action: str | None = Field(
        None,
        description="Optional action to filter schema for. If omitted, returns full schema for the tool.",
    )


# =============================================================================
# META TOOL OUTPUT MODELS
# =============================================================================


class AdminOutput(BaseModel):
    """Output for admin meta tool."""

    action: str
    help: HelpResponse | None = None
    sites: list[Any] | None = None
    total_count: int | None = None
    page_number: int | None = None
    page_size: int | None = None
    permission: Any | None = None
    permissions: list[Any] | None = None
    success: bool | None = None


class UsersOutput(BaseModel):
    """Output for users meta tool."""

    action: str
    help: HelpResponse | None = None
    user: Any | None = None
    users: list[Any] | None = None
    total_count: int | None = None
    page_number: int | None = None
    page_size: int | None = None
    success: bool | None = None
    message: str | None = None


class ProjectsOutput(BaseModel):
    """Output for projects meta tool."""

    action: str
    help: HelpResponse | None = None
    project: Any | None = None
    projects: list[Any] | None = None
    total_count: int | None = None
    page_number: int | None = None
    page_size: int | None = None
    success: bool | None = None
    message: str | None = None


class WorkbooksOutput(BaseModel):
    """Output for workbooks meta tool."""

    action: str
    help: HelpResponse | None = None
    workbook: Any | None = None
    workbooks: list[Any] | None = None
    views: list[str] | None = None
    connection: Any | None = None
    connections: list[Any] | None = None
    total_count: int | None = None
    page_number: int | None = None
    page_size: int | None = None
    success: bool | None = None
    message: str | None = None


class ViewsOutput(BaseModel):
    """Output for views meta tool."""

    action: str
    help: HelpResponse | None = None
    view: Any | None = None
    views: list[Any] | None = None
    metadata: Any | None = None
    csv_data: str | None = None
    file_path: str | None = None
    row_count: int | None = None
    image_data_base64: str | None = None
    content_type: str | None = None
    total_count: int | None = None
    page_number: int | None = None
    page_size: int | None = None


class DatasourcesOutput(BaseModel):
    """Output for datasources meta tool."""

    action: str
    help: HelpResponse | None = None
    datasource: Any | None = None
    datasources: list[Any] | None = None
    total_count: int | None = None
    page_number: int | None = None
    page_size: int | None = None
    success: bool | None = None
    message: str | None = None


class GroupsOutput(BaseModel):
    """Output for groups meta tool."""

    action: str
    help: HelpResponse | None = None
    group: Any | None = None
    groups: list[Any] | None = None
    membership: Any | None = None
    total_count: int | None = None
    page_number: int | None = None
    page_size: int | None = None
    success: bool | None = None


class VisualizationOutput(BaseModel):
    """Output for visualization meta tool."""

    action: str
    help: HelpResponse | None = None
    # upload_csv output
    datasource_id: str | None = None
    table_name: str | None = None
    fields: list[Any] | None = None
    row_count: int | None = None
    # get_sheets output
    sheets: list[Any] | None = None
    total_count: int | None = None
    # configure_shelf output
    shelf_config: Any | None = None
    generated_sql: str | None = None
    # create_visualization / create_sheet output
    view_id: str | None = None
    workbook_id: str | None = None
    chart_type: str | None = None
    data: Any | None = None
    image_base64: str | None = None
    content_type: str | None = None
    name: str | None = None
    message: str | None = None


class SchemaOutput(BaseModel):
    """Output for schema introspection tool."""

    tool: str
    action: str | None = None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


# =============================================================================
# HELP DEFINITIONS
# =============================================================================

ADMIN_HELP = HelpResponse(
    tool_name="tableau_admin",
    description="Manage Tableau sites and resource permissions.",
    actions={
        "list_sites": {
            "description": "List all Tableau sites with pagination",
            "required_params": [],
            "optional_params": ["page_number", "page_size"],
        },
        "grant_permission": {
            "description": "Grant a permission on a resource to a user or group",
            "required_params": [
                "site_id",
                "resource_type",
                "resource_id",
                "grantee_type",
                "grantee_id",
                "capability",
                "mode",
            ],
            "optional_params": [],
        },
        "list_permissions": {
            "description": "List all permissions for a resource",
            "required_params": ["site_id", "resource_type", "resource_id"],
            "optional_params": [],
        },
        "revoke_permission": {
            "description": "Revoke a permission from a resource",
            "required_params": [
                "site_id",
                "resource_type",
                "resource_id",
                "grantee_type",
                "grantee_id",
                "capability",
                "mode",
            ],
            "optional_params": [],
        },
    },
)

USERS_HELP = HelpResponse(
    tool_name="tableau_users",
    description="Manage Tableau users on a site.",
    actions={
        "create": {
            "description": "Create a new user on a site",
            "required_params": ["site_id", "name", "site_role"],
            "optional_params": ["email"],
        },
        "list": {
            "description": "List all users with pagination",
            "required_params": ["site_id"],
            "optional_params": ["page_number", "page_size"],
        },
        "get": {
            "description": "Get a specific user by ID",
            "required_params": ["site_id", "user_id"],
            "optional_params": [],
        },
        "update": {
            "description": "Update user properties",
            "required_params": ["site_id", "user_id"],
            "optional_params": [
                "name",
                "full_name",
                "email",
                "password",
                "site_role",
                "auth_setting",
            ],
        },
        "delete": {
            "description": "Remove a user from a site",
            "required_params": ["site_id", "user_id"],
            "optional_params": ["map_assets_to"],
        },
    },
)

PROJECTS_HELP = HelpResponse(
    tool_name="tableau_projects",
    description="Manage Tableau projects.",
    actions={
        "create": {
            "description": "Create a new project",
            "required_params": ["site_id", "name", "owner_id"],
            "optional_params": ["description", "parent_project_id"],
        },
        "list": {
            "description": "List all projects with pagination",
            "required_params": ["site_id"],
            "optional_params": ["parent_project_id", "page_number", "page_size"],
        },
        "get": {
            "description": "Get a specific project by ID",
            "required_params": ["site_id", "project_id"],
            "optional_params": [],
        },
        "update": {
            "description": "Update project properties",
            "required_params": ["site_id", "project_id"],
            "optional_params": ["name", "description"],
        },
        "delete": {
            "description": "Delete a project (cascades to children)",
            "required_params": ["site_id", "project_id"],
            "optional_params": [],
        },
    },
)

WORKBOOKS_HELP = HelpResponse(
    tool_name="tableau_workbooks",
    description="Manage Tableau workbooks and their datasource connections.",
    actions={
        "create": {
            "description": "Create a new workbook",
            "required_params": ["site_id", "name", "project_id", "owner_id"],
            "optional_params": ["description", "file_reference"],
        },
        "list": {
            "description": "List all workbooks with pagination",
            "required_params": ["site_id"],
            "optional_params": ["project_id", "owner_id", "page_number", "page_size"],
        },
        "get": {
            "description": "Get a specific workbook by ID",
            "required_params": ["site_id", "workbook_id"],
            "optional_params": [],
        },
        "update": {
            "description": "Update workbook properties",
            "required_params": ["site_id", "workbook_id"],
            "optional_params": ["name", "description"],
        },
        "delete": {
            "description": "Delete a workbook",
            "required_params": ["site_id", "workbook_id"],
            "optional_params": [],
        },
        "publish": {
            "description": "Publish a workbook file (.twb/.twbx)",
            "required_params": ["site_id", "name", "project_id"],
            "optional_params": [
                "file_path",
                "file_content_base64",
                "file_name",
                "description",
                "show_tabs",
                "overwrite",
                "owner_id",
            ],
        },
        "connect": {
            "description": "Link a workbook to a datasource",
            "required_params": ["site_id", "workbook_id", "datasource_id"],
            "optional_params": [],
        },
        "list_connections": {
            "description": "List all datasource connections for a workbook",
            "required_params": ["site_id", "workbook_id"],
            "optional_params": [],
        },
        "disconnect": {
            "description": "Remove a workbook-datasource connection",
            "required_params": ["site_id", "workbook_id", "connection_id"],
            "optional_params": [],
        },
    },
)

VIEWS_HELP = HelpResponse(
    tool_name="tableau_views",
    description="Query Tableau views (read-only operations).",
    actions={
        "list": {
            "description": "List all views with pagination",
            "required_params": ["site_id"],
            "optional_params": ["workbook_id", "page_number", "page_size"],
        },
        "get": {
            "description": "Get a specific view by ID",
            "required_params": ["site_id", "view_id"],
            "optional_params": [],
        },
        "metadata": {
            "description": "Get field metadata for a view",
            "required_params": ["site_id", "view_id"],
            "optional_params": ["include_sample_values", "sample_value_limit"],
        },
        "query_to_file": {
            "description": "Query view data and save to file",
            "required_params": ["site_id", "view_id"],
            "optional_params": ["max_age", "filters"],
        },
        "image": {
            "description": "Get view as PNG image",
            "required_params": ["site_id", "view_id"],
            "optional_params": ["resolution", "max_age", "filters"],
        },
    },
)

DATASOURCES_HELP = HelpResponse(
    tool_name="tableau_datasources",
    description="Manage Tableau datasources.",
    actions={
        "create": {
            "description": "Create a new datasource",
            "required_params": ["site_id", "name", "project_id", "owner_id", "connection_type"],
            "optional_params": ["description"],
        },
        "list": {
            "description": "List all datasources with pagination",
            "required_params": ["site_id"],
            "optional_params": ["project_id", "page_number", "page_size"],
        },
        "get": {
            "description": "Get a specific datasource by ID",
            "required_params": ["site_id", "datasource_id"],
            "optional_params": [],
        },
        "update": {
            "description": "Update datasource properties",
            "required_params": ["site_id", "datasource_id"],
            "optional_params": ["name", "description", "connection_type"],
        },
        "delete": {
            "description": "Delete a datasource",
            "required_params": ["site_id", "datasource_id"],
            "optional_params": [],
        },
    },
)

GROUPS_HELP = HelpResponse(
    tool_name="tableau_groups",
    description="Manage Tableau groups and memberships.",
    actions={
        "create": {
            "description": "Create a new group",
            "required_params": ["site_id", "name"],
            "optional_params": ["description"],
        },
        "list": {
            "description": "List all groups with pagination",
            "required_params": ["site_id"],
            "optional_params": ["page_number", "page_size"],
        },
        "add_user": {
            "description": "Add a user to a group",
            "required_params": ["site_id", "group_id", "user_id"],
            "optional_params": [],
        },
        "remove_user": {
            "description": "Remove a user from a group",
            "required_params": ["site_id", "group_id", "user_id"],
            "optional_params": [],
        },
    },
)

VISUALIZATION_HELP = HelpResponse(
    tool_name="tableau_visualization",
    description="Upload CSV data, configure drag-and-drop shelves, and generate visualizations.",
    actions={
        "upload_csv": {
            "description": "Upload CSV data and create a queryable datasource",
            "required_params": ["site_id", "project_id", "name"],
            "optional_params": ["csv_content", "file_content_base64", "owner_id"],
        },
        "get_sheets": {
            "description": "Get sheets (views) with their shelf configurations",
            "required_params": ["site_id"],
            "optional_params": ["workbook_id"],
        },
        "list_fields": {
            "description": "List fields from a datasource's underlying data table",
            "required_params": ["site_id", "datasource_id"],
            "optional_params": [],
        },
        "configure_shelf": {
            "description": "Configure shelf layout for a view (rows, columns, measures, filters, mark_type)",
            "required_params": ["site_id", "view_id", "shelf_config"],
            "optional_params": [],
        },
        "create_visualization": {
            "description": "Generate SQL from shelf config, execute query, and render chart",
            "required_params": ["site_id", "view_id"],
            "optional_params": ["width", "height", "format"],
        },
        "create_sheet": {
            "description": "Create a new sheet (Workbook + View) linked to a datasource",
            "required_params": ["site_id", "datasource_id"],
            "optional_params": ["name"],
        },
    },
)


# =============================================================================
# SCHEMA DEFINITIONS FOR INTROSPECTION
# =============================================================================

TOOL_SCHEMAS = {
    "tableau_admin": {"input": AdminInput, "output": AdminOutput},
    "tableau_users": {"input": UsersInput, "output": UsersOutput},
    "tableau_projects": {"input": ProjectsInput, "output": ProjectsOutput},
    "tableau_workbooks": {"input": WorkbooksInput, "output": WorkbooksOutput},
    "tableau_views": {"input": ViewsInput, "output": ViewsOutput},
    "tableau_datasources": {"input": DatasourcesInput, "output": DatasourcesOutput},
    "tableau_groups": {"input": GroupsInput, "output": GroupsOutput},
    "tableau_visualization": {"input": VisualizationInput, "output": VisualizationOutput},
}


# =============================================================================
# META TOOL IMPLEMENTATIONS
# =============================================================================


async def tableau_admin(request: AdminInput) -> AdminOutput:
    """Manage Tableau sites and permissions."""
    match request.action:
        case "help":
            return AdminOutput(action="help", help=ADMIN_HELP)

        case "list_sites":
            result = await tableau_list_sites(
                TableauListSitesInput(
                    page_number=request.page_number,
                    page_size=request.page_size,
                )
            )
            return AdminOutput(
                action="list_sites",
                sites=[s.model_dump() for s in result.sites],
                total_count=result.total_count,
                page_number=result.page_number,
                page_size=result.page_size,
            )

        case "grant_permission":
            if not all(
                [
                    request.site_id,
                    request.resource_type,
                    request.resource_id,
                    request.grantee_type,
                    request.grantee_id,
                    request.capability,
                    request.mode,
                ]
            ):
                raise ValueError(
                    "site_id, resource_type, resource_id, grantee_type, grantee_id, capability, and mode are required"
                )
            result = await tableau_grant_permission(
                TableauGrantPermissionInput(
                    site_id=request.site_id,
                    resource_type=request.resource_type,
                    resource_id=request.resource_id,
                    grantee_type=request.grantee_type,
                    grantee_id=request.grantee_id,
                    capability=request.capability,
                    mode=request.mode,
                )
            )
            return AdminOutput(action="grant_permission", permission=result.model_dump())

        case "list_permissions":
            if not all([request.site_id, request.resource_type, request.resource_id]):
                raise ValueError("site_id, resource_type, and resource_id are required")
            result = await tableau_list_permissions(
                TableauListPermissionsInput(
                    site_id=request.site_id,
                    resource_type=request.resource_type,
                    resource_id=request.resource_id,
                )
            )
            return AdminOutput(
                action="list_permissions",
                permissions=[p.model_dump() for p in result.permissions],
            )

        case "revoke_permission":
            if not all(
                [
                    request.site_id,
                    request.resource_type,
                    request.resource_id,
                    request.grantee_type,
                    request.grantee_id,
                    request.capability,
                    request.mode,
                ]
            ):
                raise ValueError(
                    "site_id, resource_type, resource_id, grantee_type, grantee_id, capability, and mode are required"
                )
            result = await tableau_revoke_permission(
                TableauRevokePermissionInput(
                    site_id=request.site_id,
                    resource_type=request.resource_type,
                    resource_id=request.resource_id,
                    grantee_type=request.grantee_type,
                    grantee_id=request.grantee_id,
                    capability=request.capability,
                    mode=request.mode,
                )
            )
            return AdminOutput(action="revoke_permission", success=result.success)

    raise ValueError(f"Unknown action: {request.action}")


async def tableau_users(request: UsersInput) -> UsersOutput:
    """Manage Tableau users."""
    match request.action:
        case "help":
            return UsersOutput(action="help", help=USERS_HELP)

        case "create":
            if not request.site_id or not request.name or not request.site_role:
                raise ValueError("site_id, name, and site_role are required for create")
            result = await tableau_create_user(
                TableauCreateUserInput(
                    site_id=request.site_id,
                    name=request.name,
                    email=request.email,
                    site_role=request.site_role,
                )
            )
            return UsersOutput(action="create", user=result.model_dump())

        case "list":
            if not request.site_id:
                raise ValueError("site_id is required for list")
            result = await tableau_list_users(
                TableauListUsersInput(
                    site_id=request.site_id,
                    page_number=request.page_number,
                    page_size=request.page_size,
                )
            )
            return UsersOutput(
                action="list",
                users=[u.model_dump() for u in result.users],
                total_count=result.total_count,
                page_number=result.page_number,
                page_size=result.page_size,
            )

        case "get":
            if not request.site_id or not request.user_id:
                raise ValueError("site_id and user_id are required for get")
            result = await tableau_get_user(
                TableauGetUserInput(site_id=request.site_id, user_id=request.user_id)
            )
            return UsersOutput(action="get", user=result.model_dump())

        case "update":
            if not request.site_id or not request.user_id:
                raise ValueError("site_id and user_id are required for update")
            result = await tableau_update_user(
                TableauUpdateUserInput(
                    site_id=request.site_id,
                    user_id=request.user_id,
                    name=request.name,
                    full_name=request.full_name,
                    email=request.email,
                    password=request.password,
                    site_role=request.site_role,
                    auth_setting=request.auth_setting,
                )
            )
            return UsersOutput(action="update", user=result.model_dump())

        case "delete":
            if not request.site_id or not request.user_id:
                raise ValueError("site_id and user_id are required for delete")
            result = await tableau_delete_user(
                TableauDeleteUserInput(
                    site_id=request.site_id,
                    user_id=request.user_id,
                    map_assets_to=request.map_assets_to,
                )
            )
            return UsersOutput(action="delete", success=result.success, message=result.message)

    raise ValueError(f"Unknown action: {request.action}")


async def tableau_projects(request: ProjectsInput) -> ProjectsOutput:
    """Manage Tableau projects."""
    match request.action:
        case "help":
            return ProjectsOutput(action="help", help=PROJECTS_HELP)

        case "create":
            if not request.site_id or not request.name or not request.owner_id:
                raise ValueError("site_id, name, and owner_id are required for create")
            result = await tableau_create_project(
                TableauCreateProjectInput(
                    site_id=request.site_id,
                    name=request.name,
                    description=request.description or "",
                    parent_project_id=request.parent_project_id,
                    owner_id=request.owner_id,
                )
            )
            return ProjectsOutput(action="create", project=result.model_dump())

        case "list":
            if not request.site_id:
                raise ValueError("site_id is required for list")
            result = await tableau_list_projects(
                TableauListProjectsInput(
                    site_id=request.site_id,
                    parent_project_id=request.parent_project_id,
                    page_number=request.page_number,
                    page_size=request.page_size,
                )
            )
            return ProjectsOutput(
                action="list",
                projects=[p.model_dump() for p in result.projects],
                total_count=result.total_count,
                page_number=result.page_number,
                page_size=result.page_size,
            )

        case "get":
            if not request.site_id or not request.project_id:
                raise ValueError("site_id and project_id are required for get")
            result = await tableau_get_project(
                TableauGetProjectInput(site_id=request.site_id, project_id=request.project_id)
            )
            return ProjectsOutput(action="get", project=result.model_dump())

        case "update":
            if not request.site_id or not request.project_id:
                raise ValueError("site_id and project_id are required for update")
            result = await tableau_update_project(
                TableauUpdateProjectInput(
                    site_id=request.site_id,
                    project_id=request.project_id,
                    name=request.name,
                    description=request.description,
                )
            )
            return ProjectsOutput(action="update", project=result.model_dump())

        case "delete":
            if not request.site_id or not request.project_id:
                raise ValueError("site_id and project_id are required for delete")
            result = await tableau_delete_project(
                TableauDeleteProjectInput(site_id=request.site_id, project_id=request.project_id)
            )
            return ProjectsOutput(action="delete", success=result.success, message=result.message)

    raise ValueError(f"Unknown action: {request.action}")


async def tableau_workbooks(request: WorkbooksInput) -> WorkbooksOutput:
    """Manage Tableau workbooks and connections."""
    match request.action:
        case "help":
            return WorkbooksOutput(action="help", help=WORKBOOKS_HELP)

        case "create":
            if (
                not request.site_id
                or not request.name
                or not request.project_id
                or not request.owner_id
            ):
                raise ValueError("site_id, name, project_id, and owner_id are required for create")
            result = await tableau_create_workbook(
                TableauCreateWorkbookInput(
                    site_id=request.site_id,
                    name=request.name,
                    project_id=request.project_id,
                    owner_id=request.owner_id,
                    description=request.description or "",
                    file_reference=request.file_reference,
                )
            )
            return WorkbooksOutput(action="create", workbook=result.model_dump())

        case "list":
            if not request.site_id:
                raise ValueError("site_id is required for list")
            result = await tableau_list_workbooks(
                TableauListWorkbooksInput(
                    site_id=request.site_id,
                    project_id=request.project_id,
                    owner_id=request.owner_id,
                    page_number=request.page_number,
                    page_size=request.page_size,
                )
            )
            return WorkbooksOutput(
                action="list",
                workbooks=[w.model_dump() for w in result.workbooks],
                total_count=result.total_count,
                page_number=result.page_number,
                page_size=result.page_size,
            )

        case "get":
            if not request.site_id or not request.workbook_id:
                raise ValueError("site_id and workbook_id are required for get")
            result = await tableau_get_workbook(
                TableauGetWorkbookInput(site_id=request.site_id, workbook_id=request.workbook_id)
            )
            return WorkbooksOutput(action="get", workbook=result.model_dump())

        case "update":
            if not request.site_id or not request.workbook_id:
                raise ValueError("site_id and workbook_id are required for update")
            result = await tableau_update_workbook(
                TableauUpdateWorkbookInput(
                    site_id=request.site_id,
                    workbook_id=request.workbook_id,
                    name=request.name,
                    description=request.description,
                )
            )
            return WorkbooksOutput(action="update", workbook=result.model_dump())

        case "delete":
            if not request.site_id or not request.workbook_id:
                raise ValueError("site_id and workbook_id are required for delete")
            result = await tableau_delete_workbook(
                TableauDeleteWorkbookInput(site_id=request.site_id, workbook_id=request.workbook_id)
            )
            return WorkbooksOutput(action="delete", success=result.success, message=result.message)

        case "publish":
            if not request.site_id or not request.name or not request.project_id:
                raise ValueError("site_id, name, and project_id are required for publish")
            result = await tableau_publish_workbook(
                TableauPublishWorkbookInput(
                    site_id=request.site_id,
                    name=request.name,
                    project_id=request.project_id,
                    file_path=request.file_path,
                    file_content_base64=request.file_content_base64,
                    file_name=request.file_name,
                    description=request.description or "",
                    show_tabs=request.show_tabs,
                    overwrite=request.overwrite,
                    owner_id=request.owner_id,
                )
            )
            return WorkbooksOutput(
                action="publish", workbook=result.model_dump(), views=result.views
            )

        case "connect":
            if not request.site_id or not request.workbook_id or not request.datasource_id:
                raise ValueError("site_id, workbook_id, and datasource_id are required for connect")
            result = await tableau_create_workbook_connection(
                TableauCreateWorkbookConnectionInput(
                    site_id=request.site_id,
                    workbook_id=request.workbook_id,
                    datasource_id=request.datasource_id,
                )
            )
            return WorkbooksOutput(action="connect", connection=result.model_dump())

        case "list_connections":
            if not request.site_id or not request.workbook_id:
                raise ValueError("site_id and workbook_id are required for list_connections")
            result = await tableau_list_workbook_connections(
                TableauListWorkbookConnectionsInput(
                    site_id=request.site_id, workbook_id=request.workbook_id
                )
            )
            return WorkbooksOutput(
                action="list_connections",
                connections=[c.model_dump() for c in result.connections],
            )

        case "disconnect":
            if not request.site_id or not request.workbook_id or not request.connection_id:
                raise ValueError(
                    "site_id, workbook_id, and connection_id are required for disconnect"
                )
            result = await tableau_delete_workbook_connection(
                TableauDeleteWorkbookConnectionInput(
                    site_id=request.site_id,
                    workbook_id=request.workbook_id,
                    connection_id=request.connection_id,
                )
            )
            return WorkbooksOutput(action="disconnect", success=result.success)

    raise ValueError(f"Unknown action: {request.action}")


async def tableau_views(request: ViewsInput) -> ViewsOutput | Image:
    """Query Tableau views (read-only)."""
    match request.action:
        case "help":
            return ViewsOutput(action="help", help=VIEWS_HELP)

        case "list":
            if not request.site_id:
                raise ValueError("site_id is required for list")
            result = await tableau_list_views(
                TableauListViewsInput(
                    site_id=request.site_id,
                    workbook_id=request.workbook_id,
                    page_number=request.page_number,
                    page_size=request.page_size,
                )
            )
            return ViewsOutput(
                action="list",
                views=[v.model_dump() for v in result.views],
                total_count=result.total_count,
                page_number=result.page_number,
                page_size=result.page_size,
            )

        case "get":
            if not request.site_id or not request.view_id:
                raise ValueError("site_id and view_id are required for get")
            result = await tableau_get_view(
                TableauGetViewInput(site_id=request.site_id, view_id=request.view_id)
            )
            return ViewsOutput(action="get", view=result.model_dump())

        case "metadata":
            if not request.site_id or not request.view_id:
                raise ValueError("site_id and view_id are required for metadata")
            result = await tableau_get_view_metadata(
                TableauGetViewMetadataInput(
                    site_id=request.site_id,
                    view_id=request.view_id,
                    include_sample_values=request.include_sample_values,
                    sample_value_limit=request.sample_value_limit,
                )
            )
            return ViewsOutput(action="metadata", metadata=result.model_dump())

        case "query_to_file":
            if not request.site_id or not request.view_id:
                raise ValueError("site_id and view_id are required for query_to_file")
            result = await tableau_query_view_data_to_file(
                TableauQueryViewDataInput(
                    site_id=request.site_id,
                    view_id=request.view_id,
                    max_age=request.max_age,
                    filters=request.filters,
                )
            )
            return ViewsOutput(
                action="query_to_file",
                file_path=result.file_path,
                row_count=result.row_count,
            )

        case "image":
            if not request.site_id or not request.view_id:
                raise ValueError("site_id and view_id are required for image")
            return await tableau_query_view_image(
                TableauQueryViewImageInput(
                    site_id=request.site_id,
                    view_id=request.view_id,
                    resolution=request.resolution,
                    max_age=request.max_age,
                    filters=request.filters,
                )
            )

    raise ValueError(f"Unknown action: {request.action}")


async def tableau_datasources(request: DatasourcesInput) -> DatasourcesOutput:
    """Manage Tableau datasources."""
    match request.action:
        case "help":
            return DatasourcesOutput(action="help", help=DATASOURCES_HELP)

        case "create":
            if (
                not request.site_id
                or not request.name
                or not request.project_id
                or not request.owner_id
                or not request.connection_type
            ):
                raise ValueError(
                    "site_id, name, project_id, owner_id, and connection_type are required for create"
                )
            result = await tableau_create_datasource(
                TableauCreateDatasourceInput(
                    site_id=request.site_id,
                    name=request.name,
                    project_id=request.project_id,
                    owner_id=request.owner_id,
                    connection_type=request.connection_type,
                    description=request.description or "",
                )
            )
            return DatasourcesOutput(action="create", datasource=result.model_dump())

        case "list":
            if not request.site_id:
                raise ValueError("site_id is required for list")
            result = await tableau_list_datasources(
                TableauListDatasourcesInput(
                    site_id=request.site_id,
                    project_id=request.project_id,
                    page_number=request.page_number,
                    page_size=request.page_size,
                )
            )
            return DatasourcesOutput(
                action="list",
                datasources=[d.model_dump() for d in result.datasources],
                total_count=result.total_count,
                page_number=result.page_number,
                page_size=result.page_size,
            )

        case "get":
            if not request.site_id or not request.datasource_id:
                raise ValueError("site_id and datasource_id are required for get")
            result = await tableau_get_datasource(
                TableauGetDatasourceInput(
                    site_id=request.site_id, datasource_id=request.datasource_id
                )
            )
            return DatasourcesOutput(action="get", datasource=result.model_dump())

        case "update":
            if not request.site_id or not request.datasource_id:
                raise ValueError("site_id and datasource_id are required for update")
            result = await tableau_update_datasource(
                TableauUpdateDatasourceInput(
                    site_id=request.site_id,
                    datasource_id=request.datasource_id,
                    name=request.name,
                    description=request.description,
                    connection_type=request.connection_type,
                )
            )
            return DatasourcesOutput(action="update", datasource=result.model_dump())

        case "delete":
            if not request.site_id or not request.datasource_id:
                raise ValueError("site_id and datasource_id are required for delete")
            result = await tableau_delete_datasource(
                TableauDeleteDatasourceInput(
                    site_id=request.site_id, datasource_id=request.datasource_id
                )
            )
            return DatasourcesOutput(
                action="delete", success=result.success, message=result.message
            )

    raise ValueError(f"Unknown action: {request.action}")


async def tableau_groups(request: GroupsInput) -> GroupsOutput:
    """Manage Tableau groups and memberships."""
    match request.action:
        case "help":
            return GroupsOutput(action="help", help=GROUPS_HELP)

        case "create":
            if not request.site_id or not request.name:
                raise ValueError("site_id and name are required for create")
            result = await tableau_create_group(
                TableauCreateGroupInput(
                    site_id=request.site_id,
                    name=request.name,
                    description=request.description or "",
                )
            )
            return GroupsOutput(action="create", group=result.model_dump())

        case "list":
            if not request.site_id:
                raise ValueError("site_id is required for list")
            result = await tableau_list_groups(
                TableauListGroupsInput(
                    site_id=request.site_id,
                    page_number=request.page_number,
                    page_size=request.page_size,
                )
            )
            return GroupsOutput(
                action="list",
                groups=[g.model_dump() for g in result.groups],
                total_count=result.total_count,
                page_number=result.page_number,
                page_size=result.page_size,
            )

        case "add_user":
            if not request.site_id or not request.group_id or not request.user_id:
                raise ValueError("site_id, group_id, and user_id are required for add_user")
            result = await tableau_add_user_to_group(
                TableauAddUserToGroupInput(
                    site_id=request.site_id,
                    group_id=request.group_id,
                    user_id=request.user_id,
                )
            )
            return GroupsOutput(action="add_user", membership=result.model_dump())

        case "remove_user":
            if not request.site_id or not request.group_id or not request.user_id:
                raise ValueError("site_id, group_id, and user_id are required for remove_user")
            result = await tableau_remove_user_from_group(
                TableauRemoveUserFromGroupInput(
                    site_id=request.site_id,
                    group_id=request.group_id,
                    user_id=request.user_id,
                )
            )
            return GroupsOutput(action="remove_user", success=result.success)

    raise ValueError(f"Unknown action: {request.action}")


async def tableau_visualization(request: VisualizationInput) -> VisualizationOutput:
    """Upload CSV data, configure shelves, and generate visualizations."""
    match request.action:
        case "help":
            return VisualizationOutput(action="help", help=VISUALIZATION_HELP)

        case "upload_csv":
            if not request.site_id or not request.project_id or not request.name:
                raise ValueError("site_id, project_id, and name are required for upload_csv")
            if not request.csv_content and not request.file_content_base64:
                raise ValueError("Either csv_content or file_content_base64 is required")
            result = await tableau_upload_csv(
                TableauUploadCsvInput(
                    site_id=request.site_id,
                    project_id=request.project_id,
                    name=request.name,
                    csv_content=request.csv_content,
                    file_content_base64=request.file_content_base64,
                    owner_id=request.owner_id,
                )
            )
            return VisualizationOutput(
                action="upload_csv",
                datasource_id=result.datasource_id,
                table_name=result.table_name,
                fields=[f.model_dump() for f in result.fields],
                row_count=result.row_count,
                message=result.message,
            )

        case "get_sheets":
            if not request.site_id:
                raise ValueError("site_id is required for get_sheets")
            result = await tableau_get_sheets(
                TableauGetSheetsInput(
                    site_id=request.site_id,
                    workbook_id=request.workbook_id,
                )
            )
            return VisualizationOutput(
                action="get_sheets",
                sheets=[s.model_dump() for s in result.sheets],
                total_count=result.total_count,
            )

        case "list_fields":
            if not request.site_id or not request.datasource_id:
                raise ValueError("site_id and datasource_id are required for list_fields")
            result = await tableau_list_fields(
                TableauListFieldsInput(
                    site_id=request.site_id,
                    datasource_id=request.datasource_id,
                )
            )
            return VisualizationOutput(
                action="list_fields",
                datasource_id=result.datasource_id,
                table_name=result.table_name,
                fields=[f.model_dump() for f in result.fields],
                row_count=result.row_count,
            )

        case "configure_shelf":
            if not request.site_id or not request.view_id or not request.shelf_config:
                raise ValueError(
                    "site_id, view_id, and shelf_config are required for configure_shelf"
                )
            shelf = ShelfConfig(**request.shelf_config)
            result = await tableau_configure_shelf(
                TableauConfigureShelfInput(
                    site_id=request.site_id,
                    view_id=request.view_id,
                    shelf_config=shelf,
                )
            )
            return VisualizationOutput(
                action="configure_shelf",
                view_id=result.view_id,
                shelf_config=result.shelf_config.model_dump(),
                generated_sql=result.generated_sql,
                message=result.message,
            )

        case "create_visualization":
            if not request.site_id or not request.view_id:
                raise ValueError("site_id and view_id are required for create_visualization")
            result = await tableau_create_visualization(
                TableauCreateVisualizationInput(
                    site_id=request.site_id,
                    view_id=request.view_id,
                    width=request.width,
                    height=request.height,
                    format=request.format,
                )
            )
            # Omit image_base64 for the LLM path — it can't render it
            # and it bloats context. Keep rows so the LLM has data to
            # reason about.
            return VisualizationOutput(
                action="create_visualization",
                view_id=result.view_id,
                chart_type=result.chart_type,
                generated_sql=result.generated_sql,
                data=result.data.model_dump(),
                image_base64=None,
                content_type=result.content_type,
                message=result.message,
            )

        case "create_sheet":
            if not request.site_id or not request.datasource_id:
                raise ValueError("site_id and datasource_id are required for create_sheet")
            result = await tableau_create_sheet(
                TableauCreateSheetInput(
                    site_id=request.site_id,
                    datasource_id=request.datasource_id,
                    name=request.name or "Sheet 1",
                )
            )
            return VisualizationOutput(
                action="create_sheet",
                view_id=result.view_id,
                workbook_id=result.workbook_id,
                name=result.name,
                datasource_id=result.datasource_id,
                message=result.message,
            )

    raise ValueError(f"Unknown action: {request.action}")


async def tableau_schema(request: SchemaInput) -> SchemaOutput:
    """Get JSON schema for any Tableau tool's input/output."""
    if request.tool not in TOOL_SCHEMAS:
        raise ValueError(f"Unknown tool: {request.tool}. Available: {list(TOOL_SCHEMAS.keys())}")

    schemas = TOOL_SCHEMAS[request.tool]
    input_schema = schemas["input"].model_json_schema()
    output_schema = schemas["output"].model_json_schema()

    # If action is specified, filter to show only relevant fields
    if request.action:
        # Add action context to the output
        return SchemaOutput(
            tool=request.tool,
            action=request.action,
            input_schema=input_schema,
            output_schema=output_schema,
        )

    return SchemaOutput(
        tool=request.tool,
        action=None,
        input_schema=input_schema,
        output_schema=output_schema,
    )
