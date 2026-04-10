"""Consolidated meta-tools for Looker MCP Server.

This module provides domain-based meta-tools that consolidate multiple
individual tools into unified interfaces with action-based routing.

Pattern: 30 individual tools → 7 meta-tools (77% reduction)

Meta-Tools:
- looker_lookml: LookML models, explores, views, and deployment
- looker_content: Folders, search, explores, fields discovery
- looker_queries: Query creation, execution, and export
- looker_looks: Look CRUD and rendering
- looker_dashboards: Dashboard CRUD, tiles, and rendering
- looker_admin: Health check and server status
- looker_schema: JSON schema introspection for all tools
"""

from typing import Any, Literal

# Import request/response models
from models import (
    CreateQueryRequest,
    ExploreRequest,
    ExportQueryRequest,
    GetDashboardRequest,
    GetLookRequest,
    HealthCheckRequest,
    ListDashboardsRequest,
    ListFoldersRequest,
    ListLooksRequest,
    ListViewsRequest,
    LookMLModelRequest,
    QueryFilter,
    RunLookPdfRequest,
    RunLookRequest,
    RunQueryByIdRequest,
    RunQueryPngRequest,
    RunQueryRequest,
    RunSqlRequest,
    SearchContentRequest,
)


def _convert_filters_dict_to_list(
    filters: dict[str, str] | None,
) -> list[QueryFilter]:
    """Convert a dict of filters to a list of QueryFilter objects.

    Args:
        filters: Dict mapping field names to filter values, or None

    Returns:
        List of QueryFilter objects (empty list if filters is None)
    """
    if not filters:
        return []
    return [QueryFilter(field=field, value=value) for field, value in filters.items()]


def _get_placeholder_context():
    """Create a placeholder Context for functions that require it but don't use it.

    Some LookML management functions (generate_lookml, deploy_lookml, etc.) accept
    a FastMCP Context parameter for compatibility with direct tool registration,
    but don't actually use the context. Meta-tools can't receive the injected
    context from FastMCP, so we provide a placeholder.

    Note: If these functions start using context methods, this will need to be
    refactored to properly propagate context through the meta-tool layer.
    """
    from mcp.server.fastmcp import Context

    return Context()


from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field

# Import original tool functions
from tools.content_discovery import (
    _search_content,
    get_dashboard,
    get_look,
    list_dashboards,
    list_explores,
    list_fields,
    list_folders,
    list_looks,
    run_look,
)
from tools.content_management import (
    add_tile_to_dashboard,
    create_dashboard,
    create_look,
)
from tools.content_rendering import run_look_pdf
from tools.health import health_check
from tools.lookml_discovery import get_explore, list_lookml_models, list_views
from tools.lookml_management import (
    deploy_lookml,
    generate_lookml,
    get_generated_lookml,
    list_available_views,
)
from tools.query_execution import (
    create_query,
    export_query,
    run_query_by_id,
    run_query_inline,
    run_query_png,
)
from tools.sql_runner import run_sql_query
from tools.v2.models import (
    AddTileRequest,
    CreateDashboardRequest,
    CreateLookRequest,
    DownloadRenderedFileRequest,
    ListExploresRequest,
    ListFieldsRequest,
)
from tools.v2.rendering import (
    looker_download_rendered_file,
    looker_export_dashboard_pdf,
    looker_export_dashboard_png,
)


# =============================================================================
# Help Response Model
# =============================================================================
class HelpResponse(BaseModel):
    """Standard help response for tool discovery."""

    tool_name: str = Field(..., description="Meta-tool name to introspect.")
    description: str = Field(..., description="Detailed description. Optional but recommended.")
    actions: dict[str, dict[str, Any]] = Field(
        ..., description="Available actions with their descriptions and parameters"
    )


# =============================================================================
# looker_lookml - LookML Discovery & Management
# =============================================================================
LOOKML_ACTIONS = Literal[
    "help",
    "list_models",
    "get_explore",
    "list_views",
    "generate",
    "get_generated",
    "list_available",
    "deploy",
]


class LookMLInput(BaseModel):
    """Input for LookML meta tool."""

    action: LOOKML_ACTIONS = Field(
        ...,
        description="Action to perform. Options: "
        "'help' (show available actions), "
        "'list_models' (list all LookML models), "
        "'get_explore' (get explore details - requires model + explore), "
        "'list_views' (list tables in an explore - requires model + explore), "
        "'generate' (generate LookML from uploaded CSVs), "
        "'get_generated' (get LookML code for a view - requires view_name), "
        "'list_available' (list views available for generation), "
        "'deploy' (deploy generated LookML to Git/Looker)",
    )
    # For get_explore, list_views
    model: str | None = Field(
        None,
        description="LookML model name. REQUIRED for 'get_explore' and 'list_views' actions. "
        "Use 'list_models' action first to discover available models.",
        examples=["ecommerce", "thelook", "user_data"],
    )
    explore: str | None = Field(
        None,
        description="LookML explore name. REQUIRED for 'get_explore' and 'list_views' actions. "
        "Use 'list_models' action first, then look at the 'explores' array in each model.",
        examples=["order_items", "users", "products"],
    )
    # For generate, deploy
    model_name: str | None = Field(None, description="Name for generated LookML model")
    connection: str | None = Field(None, description="Database connection name")
    # For get_generated
    view_name: str | None = Field(None, description="View name to get LookML for")
    # For deploy
    trigger_looker_deploy: bool = Field(True, description="Trigger Looker deploy after Git push")


class LookMLOutput(BaseModel):
    """Output for LookML meta tool."""

    action: str
    help: HelpResponse | None = None
    data: dict[str, Any] | None = None


LOOKML_HELP = HelpResponse(
    tool_name="looker_lookml",
    description="LookML discovery and management - models, explores, views, and deployment.",
    actions={
        "list_models": {
            "description": "List all available LookML models",
            "required_params": [],
            "optional_params": [],
        },
        "get_explore": {
            "description": "Get detailed information about a specific Explore",
            "required_params": ["model", "explore"],
            "optional_params": [],
        },
        "list_views": {
            "description": "List all views (tables) within an Explore",
            "required_params": ["model", "explore"],
            "optional_params": [],
        },
        "generate": {
            "description": "Generate LookML view and model files from CSV data",
            "required_params": [],
            "optional_params": ["model_name", "connection"],
        },
        "get_generated": {
            "description": "Get generated LookML content for a specific view",
            "required_params": ["view_name"],
            "optional_params": [],
        },
        "list_available": {
            "description": "List all views that can be generated from CSV data",
            "required_params": [],
            "optional_params": [],
        },
        "deploy": {
            "description": "Deploy generated LookML to Looker via Git",
            "required_params": [],
            "optional_params": ["model_name", "connection", "trigger_looker_deploy"],
        },
    },
)


async def looker_lookml(request: LookMLInput) -> LookMLOutput:
    """LookML discovery and management."""
    match request.action:
        case "help":
            return LookMLOutput(action="help", help=LOOKML_HELP)

        case "list_models":
            result = await list_lookml_models(LookMLModelRequest())
            return LookMLOutput(action="list_models", data=result.model_dump())

        case "get_explore":
            if not request.model or not request.explore:
                raise ValueError("model and explore are required for get_explore")
            result = await get_explore(ExploreRequest(model=request.model, explore=request.explore))
            return LookMLOutput(action="get_explore", data=result.model_dump())

        case "list_views":
            if not request.model or not request.explore:
                raise ValueError(
                    "model and explore are required for list_views. "
                    "First use action='list_models' to discover available models, "
                    "then action='get_explore' to see explores within a model."
                )
            req = ListViewsRequest(model=request.model, explore=request.explore)
            result = await list_views(req)
            return LookMLOutput(action="list_views", data=result.model_dump())

        case "generate":
            ctx = _get_placeholder_context()
            result = await generate_lookml(
                ctx,
                model_name=request.model_name or "seeded_data",
                connection=request.connection or "@{database_connection}",
            )
            return LookMLOutput(action="generate", data=result)

        case "get_generated":
            if not request.view_name:
                raise ValueError("view_name is required for get_generated")
            ctx = _get_placeholder_context()
            result = await get_generated_lookml(ctx, view_name=request.view_name)
            return LookMLOutput(action="get_generated", data=result)

        case "list_available":
            ctx = _get_placeholder_context()
            result = await list_available_views(ctx)
            return LookMLOutput(action="list_available", data=result)

        case "deploy":
            ctx = _get_placeholder_context()
            result = await deploy_lookml(
                ctx,
                model_name=request.model_name or "seeded_data",
                connection=request.connection or "@{database_connection}",
                trigger_looker_deploy=request.trigger_looker_deploy,
            )
            return LookMLOutput(action="deploy", data=result)

    raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# looker_content - Content Discovery
# =============================================================================
CONTENT_ACTIONS = Literal["help", "list_folders", "search", "list_explores", "list_fields"]


class ContentInput(BaseModel):
    """Input for content discovery meta tool."""

    action: CONTENT_ACTIONS = Field(
        ..., description="Action: 'help', 'list_folders', 'search', 'list_explores', 'list_fields'"
    )
    # For list_folders
    parent_id: str | None = Field(
        None,
        description="Folder ID to list contents of. Omit to list all folders from the root level. "
        "Folder IDs are strings that can be obtained from 'list_folders' action results.",
        examples=["1", "42", "shared_folder_123"],
    )
    # For search
    query: str | None = Field(
        None, description="Search text. Matches names, descriptions. Case-insensitive."
    )
    content_type: str | None = Field(
        None,
        description="Filter search results by content type. Only used with 'search' action. "
        "Options: 'look' (saved queries with visualizations), 'dashboard' (collections of tiles). "
        "Omit to search both types.",
    )
    # For list_explores, list_fields
    model: str | None = Field(None, description="LookML model name. REQUIRED for queries.")
    explore: str | None = Field(
        None, description="LookML explore name. REQUIRED with model for queries."
    )


class ContentOutput(BaseModel):
    """Output for content discovery meta tool."""

    action: str
    help: HelpResponse | None = None
    data: dict[str, Any] | None = None


CONTENT_HELP = HelpResponse(
    tool_name="looker_content",
    description="Content discovery - folders, search, explores, and fields.",
    actions={
        "list_folders": {
            "description": "List all folders containing Looks and Dashboards",
            "required_params": [],
            "optional_params": ["parent_id"],
        },
        "search": {
            "description": "Search for content (Looks, Dashboards) by text query",
            "required_params": ["query"],
            "optional_params": ["content_type"],
        },
        "list_explores": {
            "description": "List available explores for a model",
            "required_params": ["model"],
            "optional_params": [],
        },
        "list_fields": {
            "description": "List fields (dimensions and measures) for an explore",
            "required_params": ["model", "explore"],
            "optional_params": [],
        },
    },
)


async def looker_content(request: ContentInput) -> ContentOutput:
    """Content discovery - folders, search, explores, and fields."""
    match request.action:
        case "help":
            return ContentOutput(action="help", help=CONTENT_HELP)

        case "list_folders":
            result = await list_folders(ListFoldersRequest(parent_id=request.parent_id))
            return ContentOutput(action="list_folders", data=result.model_dump())

        case "search":
            if not request.query:
                raise ValueError("query is required for search")
            result = await _search_content(
                SearchContentRequest(
                    query=request.query,
                    content_type=request.content_type,
                )
            )
            return ContentOutput(action="search", data=result.model_dump())

        case "list_explores":
            if not request.model:
                raise ValueError("model is required for list_explores")
            result = await list_explores(ListExploresRequest(model=request.model))
            return ContentOutput(action="list_explores", data=result)

        case "list_fields":
            if not request.model or not request.explore:
                raise ValueError("model and explore are required for list_fields")
            req = ListFieldsRequest(model=request.model, explore=request.explore)
            result = await list_fields(req)
            return ContentOutput(action="list_fields", data=result.model_dump())

    raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# looker_queries - Query Execution
# =============================================================================
QUERY_ACTIONS = Literal["help", "create", "run_inline", "run_by_id", "run_png", "export", "sql"]


class QueryInput(BaseModel):
    """Input for query execution meta tool."""

    action: QUERY_ACTIONS = Field(
        ...,
        description="Action: 'help', 'create', 'run_inline', 'run_by_id', "
        "'run_png', 'export', 'sql'",
    )
    # For create, run_inline
    model: str | None = Field(None, description="LookML model name. REQUIRED for queries.")
    view: str | None = Field(
        None, description="View name within explore. REQUIRED for field selection."
    )
    fields: list[str] | None = Field(
        None,
        description="Fields to include in query results. REQUIRED for 'create' and 'run_inline' actions. "
        "Format: 'view_name.field_name' (e.g., 'order_items.count', 'users.created_date'). "
        "Include both dimensions (grouping fields) and measures (aggregations). "
        "Use 'looker_content' with action='list_fields' to discover available fields.",
        examples=[
            ["order_items.status", "order_items.count"],
            ["users.city", "orders.total_revenue"],
        ],
    )
    filters: dict[str, str] | None = Field(
        None,
        description="Filters to apply to the query. Keys are field names (e.g., 'order_items.status'), "
        "values are filter expressions. Filter syntax: exact match ('completed'), "
        "comparison ('>100', '>=2024-01-01'), range ('1 to 100'), "
        "contains ('%search%'), NOT ('NOT cancelled'), OR ('pending,processing'). "
        "Date filters: 'last 7 days', 'this month', 'before 2024-01-01'.",
        examples=[
            {"order_items.status": "completed"},
            {"orders.created_date": "last 30 days", "orders.total_amount": ">100"},
        ],
    )
    sorts: list[str] | None = Field(
        None,
        description="Sort order for results. Format: 'field_name' for ascending, 'field_name desc' for descending. "
        "First sort is primary, subsequent sorts are secondary.",
        examples=[["order_items.count desc"], ["users.name", "orders.created_date desc"]],
    )
    limit: int | None = Field(
        None,
        description="Maximum rows to return. Default and max: 5000. "
        "Use smaller limits (100-500) for faster responses when exploring data.",
    )
    # For run_by_id, run_png, export
    query_id: str | None = Field(
        None,
        description="Query ID to execute or export. REQUIRED for 'run_by_id', 'run_png', and 'export' actions. "
        "Obtain from the response of 'create' action or from Look/Dashboard tile configurations. "
        "Format: integer in offline mode (e.g., '1', '42'), alphanumeric slug in online mode.",
        examples=["1", "42", "AbCdEf123"],
    )
    # For run_png
    chart_type: str | None = Field(None, description="Chart type filter. Optional.")
    # For export
    format: str | None = Field(
        None,
        description="Export format for 'export' action. Options: 'json' (default, returns list of objects), "
        "'csv' (returns comma-separated text). JSON is better for further processing, CSV for spreadsheets.",
    )
    # For sql
    sql: str | None = Field(
        None,
        description="Raw SQL query for 'sql' action. SECURITY: Only SELECT statements and CTEs (WITH clauses) "
        "are allowed - INSERT/UPDATE/DELETE/DROP are blocked. The query runs against the specified connection's database. "
        "Use standard SQL syntax appropriate for the database type (BigQuery, Snowflake, PostgreSQL, etc.).",
        examples=[
            "SELECT * FROM users LIMIT 10",
            "WITH recent AS (SELECT * FROM orders WHERE date > '2024-01-01') SELECT * FROM recent",
        ],
    )
    connection: str | None = Field(None, description="Database connection name")


class QueryOutput(BaseModel):
    """Output for query execution meta tool."""

    action: str
    help: HelpResponse | None = None
    data: dict[str, Any] | None = None
    image_data: bytes | None = None


QUERY_HELP = HelpResponse(
    tool_name="looker_queries",
    description="Query creation, execution, and export.",
    actions={
        "create": {
            "description": "Create a reusable query definition",
            "required_params": ["model", "view", "fields"],
            "optional_params": ["filters", "sorts", "limit"],
        },
        "run_inline": {
            "description": "Execute a query inline without saving it",
            "required_params": ["model", "view", "fields"],
            "optional_params": ["filters", "sorts", "limit"],
        },
        "run_by_id": {
            "description": "Execute a saved query by its ID",
            "required_params": ["query_id"],
            "optional_params": [],
        },
        "run_png": {
            "description": "Execute a query and return results as PNG chart",
            "required_params": ["query_id"],
            "optional_params": [],
        },
        "export": {
            "description": "Export query results in JSON or CSV format",
            "required_params": ["query_id"],
            "optional_params": ["format", "limit"],
        },
        "sql": {
            "description": "Execute a SQL query via Looker SQL Runner (SELECT only)",
            "required_params": ["sql", "connection"],
            "optional_params": ["limit"],
        },
    },
)


async def looker_queries(request: QueryInput) -> QueryOutput:
    """Query creation, execution, and export."""
    match request.action:
        case "help":
            return QueryOutput(action="help", help=QUERY_HELP)

        case "create":
            if not request.model or not request.view or not request.fields:
                raise ValueError("model, view, and fields are required for create")
            # Build request with only non-None optional fields
            create_kwargs: dict[str, Any] = {
                "model": request.model,
                "view": request.view,
                "fields": request.fields,
                "filters": _convert_filters_dict_to_list(request.filters),
            }
            if request.sorts is not None:
                create_kwargs["sorts"] = request.sorts
            if request.limit is not None:
                create_kwargs["limit"] = request.limit
            result = await create_query(CreateQueryRequest(**create_kwargs))
            return QueryOutput(action="create", data=result.model_dump())

        case "run_inline":
            if not request.model or not request.view or not request.fields:
                raise ValueError("model, view, and fields are required for run_inline")
            # Build request with only non-None optional fields
            run_kwargs: dict[str, Any] = {
                "model": request.model,
                "view": request.view,
                "fields": request.fields,
                "filters": _convert_filters_dict_to_list(request.filters),
            }
            if request.sorts is not None:
                run_kwargs["sorts"] = request.sorts
            if request.limit is not None:
                run_kwargs["limit"] = request.limit
            result = await run_query_inline(RunQueryRequest(**run_kwargs))
            return QueryOutput(action="run_inline", data=result.model_dump())

        case "run_by_id":
            if not request.query_id:
                raise ValueError("query_id is required for run_by_id")
            # Note: RunQueryByIdRequest only accepts query_id, not limit
            result = await run_query_by_id(RunQueryByIdRequest(query_id=request.query_id))
            return QueryOutput(action="run_by_id", data=result.model_dump())

        case "run_png":
            if not request.query_id:
                raise ValueError("query_id is required for run_png")
            # Note: RunQueryPngRequest accepts query_id, width, height (not chart_type)
            result = await run_query_png(RunQueryPngRequest(query_id=request.query_id))
            # result is an Image, return bytes
            return QueryOutput(action="run_png", image_data=result.data)

        case "export":
            if not request.query_id:
                raise ValueError("query_id is required for export")
            result = await export_query(
                ExportQueryRequest(
                    query_id=request.query_id,
                    format=request.format or "json",
                    limit=request.limit,
                )
            )
            return QueryOutput(action="export", data=result.model_dump())

        case "sql":
            if not request.sql or not request.connection:
                raise ValueError("sql and connection are required for sql action")
            # Build request with only non-None optional fields
            sql_kwargs: dict[str, Any] = {
                "sql": request.sql,
                "connection": request.connection,
            }
            if request.limit is not None:
                sql_kwargs["limit"] = request.limit
            result = await run_sql_query(RunSqlRequest(**sql_kwargs))
            return QueryOutput(action="sql", data=result.model_dump())

    raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# looker_looks - Look Management
# =============================================================================
LOOK_ACTIONS = Literal["help", "list", "get", "create", "run", "render_pdf"]


class LookInput(BaseModel):
    """Input for look management meta tool."""

    action: LOOK_ACTIONS = Field(
        ..., description="Action: 'help', 'list', 'get', 'create', 'run', 'render_pdf'"
    )
    # For list
    folder_id: str | None = Field(
        None,
        description="Folder ID. REQUIRED for 'create' action (specifies where to save the Look). "
        "Optional for 'list' action (filters results to specific folder). "
        "Use 'looker_content' action='list_folders' to discover folder IDs.",
        examples=["1", "shared", "personal_123"],
    )
    # For get, run, render_pdf
    look_id: str | None = Field(
        None,
        description="Look ID to retrieve, run, or render. REQUIRED for 'get', 'run', and 'render_pdf' actions. "
        "Obtain from 'list' action results or from dashboard tile configurations.",
        examples=["1", "42", "123"],
    )
    # For create
    title: str | None = Field(None, description="Title for the entity. REQUIRED for create.")
    query_id: str | None = Field(None, description="Query ID. REQUIRED for query operations.")
    description: str | None = Field(
        None, description="Detailed description. Optional but recommended."
    )
    # For run
    limit: int | None = Field(None, description="Max results to return. Typical range: 1-100.")


class LookOutput(BaseModel):
    """Output for look management meta tool."""

    action: str
    help: HelpResponse | None = None
    data: dict[str, Any] | None = None
    image_data: bytes | None = None


LOOK_HELP = HelpResponse(
    tool_name="looker_looks",
    description="Look management - list, get, create, run, and render.",
    actions={
        "list": {
            "description": "List saved Looks (query visualizations)",
            "required_params": [],
            "optional_params": ["folder_id"],
        },
        "get": {
            "description": "Get a specific Look by ID with its query configuration",
            "required_params": ["look_id"],
            "optional_params": [],
        },
        "create": {
            "description": "Create a new saved Look from a query",
            "required_params": ["title", "query_id", "folder_id"],
            "optional_params": ["description"],
        },
        "run": {
            "description": "Execute a saved Look by ID and return query results",
            "required_params": ["look_id"],
            "optional_params": [],
        },
        "render_pdf": {
            "description": "Execute a Look and return results as a PDF document",
            "required_params": ["look_id"],
            "optional_params": [],
        },
    },
)


async def looker_looks(request: LookInput) -> LookOutput:
    """Look management - list, get, create, run, and render."""
    match request.action:
        case "help":
            return LookOutput(action="help", help=LOOK_HELP)

        case "list":
            result = await list_looks(ListLooksRequest(folder_id=request.folder_id))
            return LookOutput(action="list", data=result.model_dump())

        case "get":
            if not request.look_id:
                raise ValueError("look_id is required for get")
            result = await get_look(GetLookRequest(look_id=request.look_id))
            return LookOutput(action="get", data=result.model_dump())

        case "create":
            if not request.title or not request.query_id or not request.folder_id:
                raise ValueError("title, query_id, and folder_id are required for create")
            result = await create_look(
                CreateLookRequest(
                    title=request.title,
                    query_id=request.query_id,
                    folder_id=request.folder_id,
                    description=request.description,
                )
            )
            return LookOutput(action="create", data=result.model_dump())

        case "run":
            if not request.look_id:
                raise ValueError("look_id is required for run")
            # Note: RunLookRequest only accepts look_id, not limit
            result = await run_look(RunLookRequest(look_id=request.look_id))
            return LookOutput(action="run", data=result.model_dump())

        case "render_pdf":
            if not request.look_id:
                raise ValueError("look_id is required for render_pdf")
            result = await run_look_pdf(RunLookPdfRequest(look_id=request.look_id))
            # result is an Image, return bytes
            return LookOutput(action="render_pdf", image_data=result.data)

    raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# looker_dashboards - Dashboard Management
# =============================================================================
DASHBOARD_ACTIONS = Literal[
    "help", "list", "get", "create", "add_tile", "export_pdf", "export_png", "download_render"
]


class DashboardInput(BaseModel):
    """Input for dashboard management meta tool."""

    action: DASHBOARD_ACTIONS = Field(
        ...,
        description="Action: 'help', 'list', 'get', 'create', 'add_tile', "
        "'export_pdf', 'export_png', 'download_render'",
    )
    # For list
    folder_id: str | None = Field(None, description="Folder ID. REQUIRED for folder operations.")
    # For get, create, add_tile, export_pdf, export_png
    dashboard_id: str | None = Field(
        None,
        description="Dashboard ID. REQUIRED for 'get', 'add_tile', 'export_pdf', 'export_png' actions. "
        "Obtain from 'list' or 'create' action results.",
        examples=["1", "42", "my_dashboard_123"],
    )
    # For create
    title: str | None = Field(None, description="Title for the entity. REQUIRED for create.")
    description: str | None = Field(
        None, description="Detailed description. Optional but recommended."
    )
    # For add_tile
    query_id: str | None = Field(None, description="Query ID. REQUIRED for query operations.")
    look_id: str | None = Field(None, description="Look ID. REQUIRED for look operations.")
    tile_title: str | None = Field(None, description="Tile title. Optional.")
    tile_type: str = Field(
        "vis",
        description="Type of tile to add. Options: 'vis' (default, query visualization), "
        "'text' (static text/markdown content). Use 'vis' for charts and data displays.",
    )
    chart_type: str | None = Field(None, description="Chart type filter. Optional.")
    # For export_pdf, export_png
    width: int | None = Field(
        None,
        description="Width in pixels for PDF/PNG export. Optional, defaults to 1200 for dashboards, "
        "800 for queries. Reasonable range: 400-4000 pixels.",
    )
    height: int | None = Field(
        None,
        description="Height in pixels for PDF/PNG export. Optional, defaults to 800 for dashboards, "
        "600 for queries. Reasonable range: 300-4000 pixels.",
    )
    # For download_render
    render_task_id: str | None = Field(
        None,
        description="Render task ID for 'download_render' action. Obtained from 'export_pdf' or 'export_png' "
        "action results. Dashboard rendering is asynchronous - use this ID to check status and download "
        "the completed file.",
    )


class DashboardOutput(BaseModel):
    """Output for dashboard management meta tool."""

    action: str
    help: HelpResponse | None = None
    data: dict[str, Any] | None = None


DASHBOARD_HELP = HelpResponse(
    tool_name="looker_dashboards",
    description="Dashboard management - list, get, create, add tiles, and export.",
    actions={
        "list": {
            "description": "List all available dashboards",
            "required_params": [],
            "optional_params": ["folder_id"],
        },
        "get": {
            "description": "Get a specific dashboard by ID with full tile definitions",
            "required_params": ["dashboard_id"],
            "optional_params": [],
        },
        "create": {
            "description": "Create a new Dashboard",
            "required_params": ["title", "folder_id"],
            "optional_params": ["description"],
        },
        "add_tile": {
            "description": "Add a tile/element to a Dashboard",
            "required_params": ["dashboard_id"],
            "optional_params": ["query_id", "look_id", "tile_title", "tile_type", "chart_type"],
        },
        "export_pdf": {
            "description": "Export a Dashboard as PDF (returns render task ID)",
            "required_params": ["dashboard_id"],
            "optional_params": ["width", "height"],
        },
        "export_png": {
            "description": "Export a Dashboard as PNG (returns render task ID)",
            "required_params": ["dashboard_id"],
            "optional_params": ["width", "height"],
        },
        "download_render": {
            "description": "Download a rendered file from a completed render task",
            "required_params": ["render_task_id"],
            "optional_params": [],
        },
    },
)


async def looker_dashboards(request: DashboardInput) -> DashboardOutput:
    """Dashboard management - list, get, create, add tiles, and export."""
    match request.action:
        case "help":
            return DashboardOutput(action="help", help=DASHBOARD_HELP)

        case "list":
            result = await list_dashboards(ListDashboardsRequest(folder_id=request.folder_id))
            return DashboardOutput(action="list", data=result.model_dump())

        case "get":
            if not request.dashboard_id:
                raise ValueError("dashboard_id is required for get")
            result = await get_dashboard(GetDashboardRequest(dashboard_id=request.dashboard_id))
            return DashboardOutput(action="get", data=result.model_dump())

        case "create":
            if not request.title or not request.folder_id:
                raise ValueError("title and folder_id are required for create")
            result = await create_dashboard(
                CreateDashboardRequest(
                    title=request.title,
                    folder_id=request.folder_id,
                    description=request.description,
                )
            )
            return DashboardOutput(action="create", data=result.model_dump())

        case "add_tile":
            if not request.dashboard_id:
                raise ValueError("dashboard_id is required for add_tile")
            result = await add_tile_to_dashboard(
                AddTileRequest(
                    dashboard_id=request.dashboard_id,
                    query_id=request.query_id,
                    look_id=request.look_id,
                    title=request.tile_title,
                    type=request.tile_type,
                    chart_type=request.chart_type,
                )
            )
            return DashboardOutput(action="add_tile", data=result.model_dump())

        case "export_pdf":
            if not request.dashboard_id:
                raise ValueError("dashboard_id is required for export_pdf")
            result = await looker_export_dashboard_pdf(
                dashboard_id=request.dashboard_id,
                width=request.width,
                height=request.height,
            )
            return DashboardOutput(action="export_pdf", data=result.model_dump())

        case "export_png":
            if not request.dashboard_id:
                raise ValueError("dashboard_id is required for export_png")
            result = await looker_export_dashboard_png(
                dashboard_id=request.dashboard_id,
                width=request.width,
                height=request.height,
            )
            return DashboardOutput(action="export_png", data=result.model_dump())

        case "download_render":
            if not request.render_task_id:
                raise ValueError("render_task_id is required for download_render")
            result = await looker_download_rendered_file(
                DownloadRenderedFileRequest(
                    render_task_id=request.render_task_id,
                )
            )
            return DashboardOutput(action="download_render", data=result.model_dump())

    raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# looker_admin - Admin & Health
# =============================================================================
ADMIN_ACTIONS = Literal["help", "health"]


class AdminInput(BaseModel):
    """Input for admin meta tool."""

    action: ADMIN_ACTIONS = Field(
        ..., description="The operation to perform. REQUIRED. Call with action='help' first."
    )


class AdminOutput(BaseModel):
    """Output for admin meta tool."""

    action: str
    help: HelpResponse | None = None
    data: dict[str, Any] | None = None


ADMIN_HELP = HelpResponse(
    tool_name="looker_admin",
    description="Server administration and health check.",
    actions={
        "health": {
            "description": "Verify server status and configuration",
            "required_params": [],
            "optional_params": [],
        },
    },
)


async def looker_admin(request: AdminInput) -> AdminOutput:
    """Server administration and health check."""
    match request.action:
        case "help":
            return AdminOutput(action="help", help=ADMIN_HELP)

        case "health":
            result = await health_check(HealthCheckRequest())
            return AdminOutput(action="health", data=result.model_dump())

    raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# looker_schema - Schema Introspection
# =============================================================================
class SchemaInput(BaseModel):
    """Input for schema introspection tool."""

    tool: str = Field(..., description="Tool name for schema lookup.")
    action: str | None = Field(
        None, description="Optional: filter to show schema for specific action"
    )


class SchemaOutput(BaseModel):
    """Output for schema introspection tool."""

    tool: str
    action: str | None = None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


# Tool schema registry
TOOL_SCHEMAS = {
    "looker_lookml": {"input": LookMLInput, "output": LookMLOutput, "help": LOOKML_HELP},
    "looker_content": {"input": ContentInput, "output": ContentOutput, "help": CONTENT_HELP},
    "looker_queries": {"input": QueryInput, "output": QueryOutput, "help": QUERY_HELP},
    "looker_looks": {"input": LookInput, "output": LookOutput, "help": LOOK_HELP},
    "looker_dashboards": {
        "input": DashboardInput,
        "output": DashboardOutput,
        "help": DASHBOARD_HELP,
    },
    "looker_admin": {"input": AdminInput, "output": AdminOutput, "help": ADMIN_HELP},
    "looker_schema": {"input": SchemaInput, "output": SchemaOutput, "help": None},
}


async def looker_schema(request: SchemaInput) -> SchemaOutput:
    """Get JSON schema for any Looker tool's input/output."""
    if request.tool not in TOOL_SCHEMAS:
        available = list(TOOL_SCHEMAS.keys())
        raise ValueError(f"Unknown tool: {request.tool}. Available: {available}")

    schemas = TOOL_SCHEMAS[request.tool]
    input_schema = schemas["input"].model_json_schema()
    output_schema = schemas["output"].model_json_schema()

    # Filter schema if action is specified
    if request.action and schemas.get("help"):
        help_def = schemas["help"]
        if request.action in help_def.actions:
            action_info = help_def.actions[request.action]
            relevant_params = set(
                ["action"]  # Always include action
                + action_info.get("required_params", [])
                + action_info.get("optional_params", [])
            )
            # Filter input schema properties to only relevant params
            if "properties" in input_schema:
                filtered_props = {
                    k: v for k, v in input_schema["properties"].items() if k in relevant_params
                }
                input_schema = {**input_schema, "properties": filtered_props}
                # Update required list too
                if "required" in input_schema:
                    input_schema["required"] = [
                        r for r in input_schema["required"] if r in relevant_params
                    ]
        else:
            available_actions = list(help_def.actions.keys())
            raise ValueError(
                f"Unknown action '{request.action}' for {request.tool}. "
                f"Available: {available_actions}"
            )

    return SchemaOutput(
        tool=request.tool,
        action=request.action,
        input_schema=input_schema,
        output_schema=output_schema,
    )
