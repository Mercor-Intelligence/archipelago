"""Repository factory for creating appropriate repository instances based on mode.

This module provides a centralized factory function that creates either mock (offline)
or live (online) repositories based on the configuration settings.
"""

import asyncio
import sys
from pathlib import Path
from typing import TypeVar

# Add server to path
sys.path.insert(0, str(Path(__file__).parent))

from config import settings
from http_client import get_http_client
from mcp_schema import GeminiBaseModel as BaseModel
from models import (
    ExploreRequest,
    GetLookRequest,
    ListFoldersRequest,
    ListLooksRequest,
    LookMLModelRequest,
    RunQueryByIdRequest,
    SearchContentRequest,
)
from sql_builder import build_where_clause, convert_filters_to_dict
from stores import (
    DEFAULT_FOLDERS,
    LOOKS,
    SEARCH_RESULTS,
    get_all_explores,
    get_all_models,
)
from utils.repository import (
    DataRepository,
    FunctionalRepository,
    InMemoryDataRepository,
    LiveDataRepository,
    Repository,
    _get_version,
)

# Type variables for the factory
T = TypeVar("T", bound=BaseModel)
InputT = TypeVar("InputT", bound=BaseModel)

# Singleton key for query store access
_SINGLETON_KEY = "__looker_query_store_singleton__"

# Global auth service instance (lazily initialized)
_auth_service = None


def _generate_sql_where_clause(filters: dict[str, list[str]]) -> str:
    """Generate SQL WHERE clause from filter dict.

    Uses build_where_clause with extract_column=False since this operates
    on field names directly (not Looker view.column format).
    """
    return build_where_clause(filters, extract_column=False)


def _get_measures_for_fields(model: str, view: str, fields: list[str]) -> dict[str, str]:
    """Look up which requested fields are measures and their aggregation types.

    This enables Looker's semantic layer behavior: when a query includes measures,
    the system should GROUP BY dimensions and aggregate measures.

    Args:
        model: The LookML model name (e.g., 'nyc_311')
        view: The explore/view name (e.g., 'service_requests')
        fields: List of requested field names

    Returns:
        Dict mapping measure field names to their aggregation type
        (e.g., {'service_requests.count': 'count'})
    """
    # Get all explores (including user-uploaded data)
    all_explores = get_all_explores()

    # Look up the explore definition by exact (model, view) tuple first
    explore = all_explores.get((model, view))

    # Fallback: if not found, try to find by view name alone
    # This handles cases where users specify view name as model or
    # don't know the exact model name
    if not explore:
        for (m, v), exp in all_explores.items():
            if v == view or v == model:
                explore = exp
                break

    # Final fallback: try to infer view from field names
    # e.g., if fields contain "orders.count", look for view "orders"
    if not explore and fields:
        for field in fields:
            if "." in field:
                field_view = field.split(".")[0]
                for (m, v), exp in all_explores.items():
                    if v == field_view:
                        explore = exp
                        break
                if explore:
                    break

    if not explore:
        return {}

    # Build a set of measure names with their types
    measure_types = {}
    for measure in explore.fields.measures:
        measure_types[measure.name] = measure.type

    # Return only the measures that are in the requested fields
    return {field: measure_types[field] for field in fields if field in measure_types}


def _apply_sql_limit(sql: str, limit: int) -> str:
    """Apply a LIMIT clause to a SQL query if not already present.

    Looker's run_sql_query endpoint does NOT support a limit parameter,
    so we must enforce limits by modifying the SQL query itself.

    Args:
        sql: Original SQL query
        limit: Maximum rows to return

    Returns:
        SQL query with LIMIT clause applied
    """
    import re

    # Normalize whitespace for pattern matching
    sql_normalized = " ".join(sql.split())

    # Check if query already has a LIMIT clause at the END (case-insensitive)
    # Must account for optional trailing semicolon
    # Match LIMIT followed by a number, possibly with OFFSET, then optional semicolon
    limit_pattern = r"\bLIMIT\s+(\d+)(\s+OFFSET\s+\d+)?\s*;?\s*$"
    end_limit_match = re.search(limit_pattern, sql_normalized, re.IGNORECASE)

    if end_limit_match:
        # Query has LIMIT at the end - extract the OUTER limit value
        # This correctly handles CTEs/subqueries with their own LIMIT clauses
        existing_limit = int(end_limit_match.group(1))
        if existing_limit <= limit:
            # Existing limit is already more restrictive, keep as-is
            return sql
        else:
            # Replace the OUTER LIMIT (at the end) with our more restrictive limit
            # Use a pattern that matches from the end to avoid replacing inner LIMITs
            sql_stripped = sql.rstrip()
            has_semicolon = sql_stripped.endswith(";")
            if has_semicolon:
                sql_stripped = sql_stripped[:-1].rstrip()

            # Replace the last LIMIT clause
            # Find position of last LIMIT in the original SQL
            last_limit_pattern = r"\bLIMIT\s+\d+(\s+OFFSET\s+\d+)?\s*$"
            replaced = re.sub(
                last_limit_pattern,
                f"LIMIT {limit}" + (r"\1" if end_limit_match.group(2) else ""),
                sql_stripped,
                flags=re.IGNORECASE,
            )
            return replaced + (";" if has_semicolon else "")

    # No LIMIT clause at the end - append one
    # Handle trailing semicolon if present
    sql_stripped = sql.rstrip()
    if sql_stripped.endswith(";"):
        return f"{sql_stripped[:-1]} LIMIT {limit};"
    else:
        return f"{sql_stripped} LIMIT {limit}"


def _get_auth_service():
    """Get or create the global auth service instance.

    Creates a singleton LookerAuthService that's shared across all
    repositories to avoid redundant OAuth2 logins.

    Returns:
        LookerAuthService instance

    Raises:
        ValueError: If required credentials are not configured
    """
    global _auth_service

    if _auth_service is None:
        # Validate all required credentials
        if not settings.looker_base_url:
            raise ValueError(
                "Online mode requires LOOKER_BASE_URL to be set. "
                "Please configure your Looker instance URL."
            )

        if not settings.looker_client_id or not settings.looker_client_secret:
            raise ValueError(
                "Online mode requires LOOKER_CLIENT_ID and LOOKER_CLIENT_SECRET to be set. "
                "Please configure your Looker API credentials."
            )

        from auth import LookerAuthService

        _auth_service = LookerAuthService(
            base_url=settings.looker_base_url,
            client_id=settings.looker_client_id,
            client_secret=settings.looker_client_secret,
            verify_ssl=settings.looker_verify_ssl,
            timeout=settings.looker_timeout,
        )

    return _auth_service


def create_repository[InputT: BaseModel, T: BaseModel](
    input_class: type[InputT],
    response_class: type[T],
) -> Repository[T, InputT]:
    """Create appropriate repository based on input model type and current mode.

    This factory function delegates to the input_class's create_repository() method,
    which is defined in APIConfigurableBase with a default implementation.

    Models can override create_repository() to customize behavior for specific modes,
    and call super().create_repository() to fall back to default behavior.

    Args:
        input_class: The input model class (must inherit from APIConfigurableBase)
        response_class: The response model class

    Returns:
        Repository instance configured for the current mode

    Raises:
        ValueError: If input_class is not supported or if online mode is not configured

    Example:
        >>> from models import LookMLModelRequest, LookMLModelResponse
        >>> repo = create_repository(LookMLModelRequest, LookMLModelResponse)
        >>> response = await repo.get(LookMLModelRequest())
    """
    # Delegate to the input class's create_repository method (virtual function)
    return input_class.create_repository(response_class)


def _create_mock_repository[InputT: BaseModel, T: BaseModel](
    input_class: type[InputT],
    response_class: type[T],
) -> DataRepository[T, InputT]:
    """Create offline repository with appropriate data based on input model type."""
    # Map input classes to their data stores
    if input_class is LookMLModelRequest:
        # Use dynamic getter to include user-uploaded models
        all_models = get_all_models()
        data = {"models": [model.model_dump() for model in all_models]}
        endpoint = "lookml_models"

    elif input_class is ExploreRequest:
        # Convert explores dict to lookup_key/response format
        # Use dynamic getter to include user-uploaded explores
        all_explores = get_all_explores()
        data = []
        for (model, explore), explore_response in all_explores.items():
            data.append(
                {
                    "lookup_key": {"model": model, "explore": explore},
                    "response": explore_response.model_dump(),
                }
            )
        endpoint = "explores"

    elif input_class is ListFoldersRequest:
        data = {"folders": [folder.model_dump() for folder in DEFAULT_FOLDERS]}
        endpoint = "folders"

    elif input_class is ListLooksRequest:
        # Create entries for both filtered and unfiltered requests
        data = []

        # Filtered by folder (more specific - add first)
        folder_looks = {}
        for look in LOOKS:
            if look.folder_id:
                if look.folder_id not in folder_looks:
                    folder_looks[look.folder_id] = []
                folder_looks[look.folder_id].append(look)

        for folder_id, looks in folder_looks.items():
            folder_looks_data = [look.model_dump() for look in looks]
            data.append(
                {"lookup_key": {"folder_id": folder_id}, "response": {"looks": folder_looks_data}}
            )

        # Unfiltered (all looks) - empty lookup_key matches all (add last as fallback)
        all_looks = [look.model_dump() for look in LOOKS]
        data.append({"lookup_key": {}, "response": {"looks": all_looks}})
        endpoint = "looks"

    elif input_class is GetLookRequest:
        # Create entries for each Look by ID
        # Note: GetLookResponse now extends Look directly, so return Look data at root level
        data = []
        for look in LOOKS:
            data.append({"lookup_key": {"look_id": look.id}, "response": look.model_dump()})
        endpoint = "looks"

    elif input_class is SearchContentRequest:
        # Create entries for known search terms
        data = []
        for keyword, results in SEARCH_RESULTS.items():
            types_str = "dashboard,look"  # sorted
            data.append(
                {
                    "lookup_key": {"query": keyword, "types": types_str, "limit": "100"},
                    "response": {
                        "results": [r.model_dump() for r in results],
                        "total": len(results),
                    },
                }
            )
        endpoint = "search"

    else:
        raise ValueError(f"Unsupported input class for mock repository: {input_class.__name__}")

    # Return InMemoryDataRepository with the configured data
    return InMemoryDataRepository(
        endpoint=endpoint,
        model_class=response_class,
        data=data,
        input_class=input_class,
    )


def _create_live_repository[InputT: BaseModel, T: BaseModel](
    input_class: type[InputT],
    response_class: type[T],
) -> LiveDataRepository[T, InputT]:
    """Create live repository that calls Looker SDK (to be implemented)."""
    if not settings.looker_base_url:
        raise ValueError(
            "Online mode requires LOOKER_BASE_URL to be set. "
            "Please configure your Looker instance settings or set OFFLINE_MODE=true"
        )

    # Get endpoint from input class's API configuration
    try:
        api_config = input_class.get_api_config()
        endpoint = api_config.get("endpoint", "unknown")
    except AttributeError:
        raise ValueError(
            f"Unsupported input class for live repository: {input_class.__name__}. "
            "Input class must implement get_api_config() method."
        )

    # Get singleton auth service
    auth_service = _get_auth_service()

    # Create live repository with OAuth2 auth
    return LiveDataRepository(
        endpoint=endpoint,
        model_class=response_class,
        input_class=input_class,
        base_url=settings.looker_base_url,
        auth_service=auth_service,
    )


def _create_query_repository[T: BaseModel](
    response_class: type[T],
):
    """Create a custom repository for CreateQueryRequest in offline mode.

    This repository handles query creation by storing queries in the
    shared query store (accessed via sys.modules singleton).

    Note: This should only be called in offline mode. Online mode is handled
    by the parent class's create_repository() implementation.

    Args:
        response_class: The response model class (CreateQueryResponse)

    Returns:
        Repository that can create and store queries
    """
    from models import CreateQueryRequest, CreateQueryResponse, Query
    from query_store import get_next_query_id, get_query_lock, get_query_store
    from sql_builder import convert_filters_to_dict

    async def create_query(request: CreateQueryRequest) -> CreateQueryResponse:
        """Create a new query and store it."""
        # Convert filter list to dict
        filter_dict = convert_filters_to_dict(request.filters)

        # Get next query ID (thread-safe)
        query_id = get_next_query_id()

        # Convert TableCalculation instances to dicts for Pydantic validation
        # Pydantic v2 requires dicts when constructing nested models
        dynamic_fields = None
        if request.dynamic_fields:
            dynamic_fields = [
                df.model_dump() if hasattr(df, "model_dump") else df
                for df in request.dynamic_fields
            ]

        query = Query(
            id=query_id,
            model=request.model,
            view=request.view,
            fields=request.fields,
            filters=filter_dict,
            sorts=request.sorts,
            limit=request.limit,
            dynamic_fields=dynamic_fields,
            vis_config=request.vis_config,
        )

        # Store and persist (thread-safe)
        from state_persistence import save_query

        query_store = get_query_store()
        with get_query_lock():
            query_store[query_id] = query

            # Persist query to STATE_LOCATION for snapshot capture
            query_data = {
                "id": query_id,
                "model": request.model,
                "view": request.view,
                "fields": request.fields,
                "filters": filter_dict,
                "sorts": request.sorts,
                "limit": request.limit,
                "dynamic_fields": dynamic_fields,
                "vis_config": request.vis_config,
            }
            save_query(query_id, query_data)

        # Return CreateQueryResponse
        return CreateQueryResponse(query=query)

    return FunctionalRepository(
        endpoint="queries",
        model_class=response_class,
        input_class=CreateQueryRequest,
        func=create_query,
    )


def _create_run_query_repository[T: BaseModel](
    response_class: type[T],
):
    """Create a custom repository for RunQueryByIdRequest in offline mode.

    This repository checks the shared query store (accessed via sys.modules singleton)
    for dynamically created queries and executes them against DuckDB.

    When queries include measures, this performs GROUP BY aggregation like
    Looker's semantic layer.

    Note: This should only be called in offline mode. Online mode is handled
    by the parent class's create_repository() implementation.

    Args:
        response_class: The response model class (QueryResult)

    Returns:
        Repository that can execute queries by ID
    """
    from duckdb_query_executor import get_query_data
    from query_store import get_query_store
    from utils.table_calculations import (
        apply_table_calcs,
        dynamic_fields_to_dict,
        get_table_calculation_field_names,
    )

    async def execute_query_by_id(request: RunQueryByIdRequest) -> T | None:
        """Execute a query by ID."""
        query_id = request.query_id

        # Try both string and int versions for query_store lookup
        # API may pass query_id as string, but query_store uses int keys
        query_id_int = None
        if isinstance(query_id, str) and query_id.isdigit():
            query_id_int = int(query_id)

        # Check query store for dynamically created queries
        query_store = get_query_store()

        if query_id in query_store or (query_id_int is not None and query_id_int in query_store):
            # Use whichever version exists in the store
            lookup_id = query_id if query_id in query_store else query_id_int
            query = query_store[lookup_id]

            # Convert filter dict back to list of dicts
            # Create one filter dict per value to preserve multi-value filters
            filters = [
                {"field": field, "value": value}
                for field, values in query.filters.items()
                if values  # Skip empty value lists
                for value in values  # Create one filter per value
            ]

            # Convert filter list to dict for execution
            filter_dict = convert_filters_to_dict(filters)

            # Look up which fields are measures (for aggregation)
            measures = _get_measures_for_fields(query.model, query.view, query.fields)

            # Try seeded data first (real CSV data)
            data = get_query_data(
                fields=query.fields,
                filters=filter_dict,
                sorts=query.sorts,
                limit=query.limit,
                measures=measures,
            )

            # Raise error if no data available (table doesn't exist)
            if data is None:
                raise ValueError(
                    f"Table '{query.view}' not found in database. "
                    f"Ensure the table exists in DuckDB."
                )

            # Apply table calculations if specified
            if query.dynamic_fields:
                data = apply_table_calcs(data, dynamic_fields_to_dict(query.dynamic_fields))

            # Build fields list including table calculation field names
            fields = list(query.fields)
            if query.dynamic_fields:
                fields.extend(get_table_calculation_field_names(query.dynamic_fields))

            # Generate SQL representation (include GROUP BY if measures are present)
            field_list = ", ".join(query.fields)
            sql = f"SELECT {field_list} FROM {query.view}"
            if filter_dict:
                where_clause = _generate_sql_where_clause(filter_dict)
                if where_clause:
                    sql += f" WHERE {where_clause}"
            if measures:
                # Add GROUP BY for dimensions (non-measure fields)
                dimension_fields = [f for f in query.fields if f not in measures]
                if dimension_fields:
                    sql += f" GROUP BY {', '.join(dimension_fields)}"
            if query.sorts:
                sql += f" ORDER BY {', '.join(query.sorts)}"
            sql += f" LIMIT {query.limit}"

            return response_class(
                data=data,
                fields=fields,
                row_count=len(data),
                sql=sql,
            )

        # Query not found - raise ValueError matching LiveDataRepository's 404 format
        raise ValueError(f"API error 404: Query {query_id} not found")

    return FunctionalRepository(
        endpoint="queries",
        model_class=response_class,
        input_class=RunQueryByIdRequest,
        func=execute_query_by_id,
    )


def _create_inline_query_repository[T: BaseModel](
    response_class: type[T],
):
    """Create a custom repository for RunQueryRequest (inline execution) in offline mode.

    This repository executes queries inline without saving them against DuckDB.
    Raises an error if the requested table is not found in the database.

    When queries include measures (e.g., service_requests.count), this performs
    GROUP BY aggregation like Looker's semantic layer.

    Note: This should only be called in offline mode. Online mode is handled
    by the parent class's create_repository() implementation.

    Args:
        response_class: The response model class (QueryResult)

    Returns:
        Repository that can execute queries inline
    """
    from duckdb_query_executor import get_query_data
    from models import RunQueryRequest
    from utils.table_calculations import (
        apply_table_calcs,
        dynamic_fields_to_dict,
        get_table_calculation_field_names,
    )

    async def execute_query_inline(request: RunQueryRequest) -> T:
        """Execute a query inline without saving it."""
        # Convert filter list to dict
        filter_dict = convert_filters_to_dict(request.filters)

        # Look up which fields are measures (for aggregation)
        measures = _get_measures_for_fields(request.model, request.view, request.fields)

        # Try seeded data first (real CSV data)
        data = get_query_data(
            fields=request.fields,
            filters=filter_dict,
            sorts=request.sorts,
            limit=request.limit,
            measures=measures,
        )

        # Raise error if no data available (table doesn't exist)
        if data is None:
            raise ValueError(
                f"Table '{request.view}' not found in database. Ensure the table exists in DuckDB."
            )

        # Apply table calculations if specified
        if request.dynamic_fields:
            data = apply_table_calcs(data, dynamic_fields_to_dict(request.dynamic_fields))

        # Build fields list including table calculation field names
        fields = list(request.fields)
        if request.dynamic_fields:
            fields.extend(get_table_calculation_field_names(request.dynamic_fields))

        # Generate SQL representation (include GROUP BY if measures are present)
        field_list = ", ".join(request.fields)
        sql = f"SELECT {field_list} FROM {request.view}"
        if filter_dict:
            where_clause = _generate_sql_where_clause(filter_dict)
            if where_clause:
                sql += f" WHERE {where_clause}"
        if measures:
            # Add GROUP BY for dimensions (non-measure fields)
            dimension_fields = [f for f in request.fields if f not in measures]
            if dimension_fields:
                sql += f" GROUP BY {', '.join(dimension_fields)}"
        if request.sorts:
            sql += f" ORDER BY {', '.join(request.sorts)}"
        sql += f" LIMIT {request.limit}"

        return response_class(
            data=data,
            fields=fields,
            row_count=len(data),
            sql=sql,
        )

    return FunctionalRepository(
        endpoint="queries_inline",
        model_class=response_class,
        input_class=RunQueryRequest,
        func=execute_query_inline,
    )


def _create_search_content_repository[T: BaseModel](
    response_class: type[T],
):
    """Create a custom repository for SearchContentRequest in offline mode.

    Searches both static data and dynamically created Looks/Dashboards
    by performing case-insensitive matching on titles and descriptions.

    Note: This should only be called in offline mode. Online mode is handled
    by the parent class's create_repository() implementation.

    Args:
        response_class: The response model class (SearchContentResponse)

    Returns:
        Repository configured for offline mode
    """
    from models import SearchContentRequest
    from store_accessors import search_content

    async def search_content_func(request: SearchContentRequest) -> T:
        """Search for content by query text."""
        # Normalize types to strings (handle both enum and string values)
        types = [t.value if hasattr(t, "value") else str(t) for t in request.types]

        # Use unified accessor for searching
        results = search_content(
            query=request.query,
            types=types,
            limit=request.limit,
        )

        return response_class(
            results=results,
            total=len(results),
        )

    return FunctionalRepository(
        endpoint="search",
        model_class=response_class,
        input_class=SearchContentRequest,
        func=search_content_func,
    )


def _create_looks_repository[T: BaseModel](
    response_class: type[T],
):
    """Create a custom repository for ListLooksRequest in offline mode.

    Implements custom filtering and pagination for looks.
    Includes both pre-seeded mock looks and dynamically created looks.

    Note: This should only be called in offline mode. Online mode is handled
    by the parent class's create_repository() implementation.

    Args:
        response_class: The response model class (ListLooksResponse)

    Returns:
        Repository configured for offline mode
    """
    from models import ListLooksRequest
    from store_accessors import get_all_looks

    async def list_looks_func(request: ListLooksRequest) -> T:
        """List all available looks with optional filtering."""
        # Get all looks using unified accessor
        looks = list(get_all_looks())

        # Filter by folder
        if request.folder_id:
            looks = [look for look in looks if look.folder_id == request.folder_id]

        # Filter by title (case-insensitive contains)
        if request.title:
            looks = [look for look in looks if request.title.lower() in look.title.lower()]

        # Apply limit
        looks = looks[: request.limit]

        return response_class(looks=looks)

    return FunctionalRepository(
        endpoint="looks",
        model_class=response_class,
        input_class=ListLooksRequest,
        func=list_looks_func,
    )


def _create_dashboard_repository[T: BaseModel](
    response_class: type[T],
):
    """Create a custom repository for ListDashboardsRequest in offline mode.

    Implements custom filtering, searching, sorting, and pagination for dashboards.
    Includes both pre-seeded mock dashboards and dynamically created dashboards.

    Note: This should only be called in offline mode. Online mode is handled
    by the parent class's create_repository() implementation.

    Args:
        response_class: The response model class (ListDashboardsResponse)

    Returns:
        Repository configured for offline mode
    """
    from models import ListDashboardsRequest
    from store_accessors import get_all_dashboards

    async def list_dashboards(request: ListDashboardsRequest) -> T:
        """List all available dashboards with optional filtering."""
        # Get all dashboards using unified accessor
        dashboard_infos = [
            {
                "id": d["id"],
                "title": d["title"],
                "description": d["description"],
                "folder_id": d["folder_id"],
                "tile_count": len(d["tiles"]),
                "tiles": [
                    {"id": t["id"], "title": t["title"], "type": t["type"]} for t in d["tiles"]
                ],
                "created_at": d["created_at"],
                "updated_at": d["updated_at"],
                "_is_mock": d.get("_is_mock", False),
            }
            for d in get_all_dashboards()
        ]

        # Filter by search (title contains)
        if request.search:
            dashboard_infos = [
                d for d in dashboard_infos if request.search.lower() in d["title"].lower()
            ]

        # Filter by folder
        if request.folder_id:
            dashboard_infos = [d for d in dashboard_infos if d["folder_id"] == request.folder_id]

        # Sort
        if request.sorts:
            for sort_field in reversed(request.sorts):
                # Handle descending sort (field starts with -)
                if sort_field.startswith("-"):
                    field_name = sort_field[1:]
                    reverse = True
                else:
                    field_name = sort_field
                    reverse = False

                # Sort by the field
                if dashboard_infos and field_name in dashboard_infos[0]:
                    dashboard_infos.sort(key=lambda d: d.get(field_name) or "", reverse=reverse)

        # Get total before pagination
        total = len(dashboard_infos)

        # Paginate
        dashboard_infos = dashboard_infos[: request.limit]

        # Remove internal flags before returning
        for d in dashboard_infos:
            d.pop("_is_mock", None)

        return response_class(dashboards=dashboard_infos, total_count=total)

    return FunctionalRepository(
        endpoint="dashboards",
        model_class=response_class,
        input_class=ListDashboardsRequest,
        func=list_dashboards,
    )


def _create_get_look_repository[T: BaseModel](
    response_class: type[T],
):
    """Create a custom repository for GetLookRequest in offline mode.

    This repository checks both LOOKS and the shared look store for dynamically created Looks.

    Note: This should only be called in offline mode. Online mode is handled
    by the parent class's create_repository() implementation.

    Args:
        response_class: The response model class (GetLookResponse)

    Returns:
        Repository configured for offline mode
    """
    from models import GetLookRequest
    from store_accessors import find_look_by_id

    async def get_look(request: GetLookRequest) -> T | None:
        """Get a Look by ID."""
        look = find_look_by_id(request.look_id)
        if look:
            return response_class(**look.model_dump())
        return None

    return FunctionalRepository(
        endpoint="looks",
        model_class=response_class,
        input_class=GetLookRequest,
        func=get_look,
    )


def _create_run_look_repository[T: BaseModel](
    response_class: type[T],
):
    """Create a custom repository for RunLookRequest in offline mode.

    This repository chains GetLook + RunQueryById operations to execute a Look.

    Note: This should only be called in offline mode. Online mode is handled
    by the parent class's create_repository() implementation.

    Args:
        response_class: The response model class (RunLookResponse)

    Returns:
        Repository configured for offline mode
    """
    from models import (
        GetLookRequest,
        GetLookResponse,
        QueryResult,
        RunLookRequest,
    )

    async def run_look(request: RunLookRequest) -> T:
        """Execute a Look by chaining get_look + run_query_by_id."""
        # Get the Look definition
        get_look_repo = create_repository(GetLookRequest, GetLookResponse)
        look_response = await get_look_repo.get(GetLookRequest(look_id=request.look_id))

        if look_response is None:
            raise ValueError(f"API error 404: Look {request.look_id} not found")

        # GetLookResponse now extends Look directly, so use it as-is
        look = look_response

        # Verify Look has an associated query
        if look.query_id is None:
            raise ValueError(f"Look {request.look_id} has no associated query")

        # Run the query using the RunQueryById repository
        run_query_repo = create_repository(RunQueryByIdRequest, QueryResult)
        result = await run_query_repo.get(RunQueryByIdRequest(query_id=look.query_id))

        return response_class(result=result)

    return FunctionalRepository(
        endpoint="looks_run",
        model_class=response_class,
        input_class=RunLookRequest,
        func=run_look,
    )


def _create_get_dashboard_repository[T: BaseModel](
    response_class: type[T],
):
    """Create a custom repository for GetDashboardRequest in offline mode.

    This repository retrieves a specific dashboard by ID with full tile definitions.
    Checks both DASHBOARDS and the dashboard_store for dynamically created dashboards.
    Includes dynamically added tiles from the dashboard_tile_store.

    Note: This should only be called in offline mode. Online mode is handled
    by the parent class's create_repository() implementation.

    Args:
        response_class: The response model class (GetDashboardResponse)

    Returns:
        Repository configured for offline mode
    """
    from models import GetDashboardRequest
    from store_accessors import find_dashboard_by_id

    async def get_dashboard(request: GetDashboardRequest) -> T:
        """Get dashboard definition with tiles, filters, and metadata."""
        dashboard = find_dashboard_by_id(request.dashboard_id)

        if not dashboard:
            raise ValueError(f"API error 404: Dashboard {request.dashboard_id} not found")

        return response_class(
            id=dashboard["id"],
            title=dashboard["title"],
            description=dashboard["description"],
            tiles=dashboard["tiles"],
            filters=dashboard["filters"],
            folder_id=dashboard["folder_id"],
            created_at=dashboard["created_at"],
            updated_at=dashboard["updated_at"],
        )

    return FunctionalRepository(
        endpoint="dashboards",
        model_class=response_class,
        input_class=GetDashboardRequest,
        func=get_dashboard,
    )


def _create_run_dashboard_repository[T: BaseModel](
    response_class: type[T],
):
    """Create a custom repository for RunDashboardRequest in offline mode.

    This repository executes all tile queries in a dashboard with filter merging.
    Checks both DASHBOARDS and the dashboard_store for dynamically created dashboards.
    Includes dynamically added tiles from the dashboard_tile_store.

    Note: This should only be called in offline mode. Online mode is handled
    by the parent class's create_repository() implementation.

    Args:
        response_class: The response model class (RunDashboardResponse)

    Returns:
        Repository configured for offline mode
    """
    from duckdb_query_executor import get_query_data
    from loguru import logger
    from models import (
        QueryResult,
        RunDashboardRequest,
        RunLookRequest,
        RunLookResponse,
    )
    from store_accessors import find_dashboard_by_id
    from utils.chart_renderer import render_chart

    async def run_dashboard(request: RunDashboardRequest) -> T:
        """Execute all tile queries for a dashboard."""
        dashboard = find_dashboard_by_id(request.dashboard_id)

        if not dashboard:
            raise ValueError(f"API error 404: Dashboard {request.dashboard_id} not found")

        # Collect all tiles from the unified dashboard dict
        all_tiles = [
            {
                "id": tile["id"],
                "title": tile["title"],
                "type": tile.get("type", "column"),
                "query": tile.get("query", {}),
                "look_id": tile.get("look_id"),
                "query_id": tile.get("query_id"),
            }
            for tile in dashboard["tiles"]
        ]

        tile_results = []
        for tile in all_tiles:
            try:
                tile_query = tile.get("query", {})
                look_id = tile.get("look_id")
                query_id = tile.get("query_id")

                # Try to get actual data from look_id or query_id first
                # NOTE: Dashboard-level filters (request.filters) are not applied to
                # look_id/query_id tiles because RunLookRequest and RunQueryByIdRequest
                # don't support filter parameters. This is a known limitation - to apply
                # filters, tiles should use inline query definitions with the filters merged.
                data = None
                fields = None
                sql = None

                if look_id:
                    # Run the Look to get actual data
                    try:
                        run_look_repo = create_repository(RunLookRequest, RunLookResponse)
                        look_result = await run_look_repo.get(RunLookRequest(look_id=look_id))
                        if look_result and look_result.result:
                            data = look_result.result.data
                            fields = look_result.result.fields
                            sql = look_result.result.sql
                    except Exception:
                        # Log but continue to try other methods
                        pass

                if data is None and query_id:
                    # Run the query by ID to get actual data
                    try:
                        run_query_repo = create_repository(RunQueryByIdRequest, QueryResult)
                        req = RunQueryByIdRequest(query_id=query_id)
                        query_result = await run_query_repo.get(req)
                        if query_result:
                            data = query_result.data
                            fields = query_result.fields
                            sql = query_result.sql
                    except Exception:
                        # Log but continue to try inline query
                        pass

                # Fall back to inline query if no look_id/query_id or they failed
                if data is None:
                    if not tile_query or not tile_query.get("fields"):
                        # Skip tiles without query definition or missing fields key
                        tile_results.append(
                            {
                                "tile_id": tile["id"],
                                "tile_title": tile["title"],
                                "tile_type": tile.get("type", "column"),
                                "query_result": None,
                                "png": None,
                                "error": "Missing fields key in query definition",
                            }
                        )
                        continue

                    # Merge dashboard-level filters with tile-level filters
                    tile_filters = tile_query.get("filters", {})
                    merged_filters = {**tile_filters, **request.filters}

                    # Execute tile query against DuckDB
                    fields = tile_query["fields"]
                    model_name = tile_query.get("model", "ecommerce")
                    view_name = tile_query.get("view", "unknown")

                    # Look up which fields are measures (for aggregation)
                    measures = _get_measures_for_fields(model_name, view_name, fields)

                    data = get_query_data(
                        fields=fields,
                        filters=merged_filters if merged_filters else None,
                        sorts=tile_query.get("sorts"),
                        limit=tile_query.get("limit", 5000),
                        measures=measures,
                    )

                    # Handle missing table gracefully for dashboard tiles
                    if data is None:
                        tile_results.append(
                            {
                                "tile_id": tile["id"],
                                "tile_title": tile["title"],
                                "tile_type": tile.get("type", "column"),
                                "query_result": None,
                                "png": None,
                                "error": f"Table '{view_name}' not found in database",
                            }
                        )
                        continue

                    # Generate SQL for debugging
                    field_list = ", ".join(fields)
                    sql = f"SELECT {field_list} FROM {view_name}"
                    if merged_filters:
                        where_clause = _generate_sql_where_clause(merged_filters)
                        if where_clause:
                            sql += f" WHERE {where_clause}"
                    if tile_query.get("sorts"):
                        sql += f" ORDER BY {', '.join(tile_query['sorts'])}"
                    sql += f" LIMIT {tile_query.get('limit', 5000)}"

                # Use dicts to avoid module mismatch issues with Pydantic models
                result = {
                    "data": data,
                    "fields": fields,
                    "row_count": len(data),
                    "sql": sql,
                }

                # Render PNG for visualization tiles (only if requested)
                tile_type = tile.get("type", "column")
                png_data = None
                if request.include_png and tile_type not in ("table",) and data and fields:
                    try:
                        # Map tile type to chart type (add looker_ prefix if needed)
                        if tile_type.startswith("looker_"):
                            chart_type = tile_type
                        else:
                            chart_type = f"looker_{tile_type}"
                        png_data = render_chart(
                            data=data,
                            fields=fields,
                            chart_type=chart_type,
                            width=400,
                            height=300,
                            title=tile["title"],
                        )
                    except Exception as e:
                        # Log but don't fail the tile
                        logger.warning(f"Failed to render PNG for tile {tile['id']}: {e}")

                tile_results.append(
                    {
                        "tile_id": tile["id"],
                        "tile_title": tile["title"],
                        "tile_type": tile_type,
                        "query_result": result,
                        "png": png_data,
                        "error": None,
                    }
                )
            except Exception as e:
                # Don't fail entire dashboard on single tile error
                tile_results.append(
                    {
                        "tile_id": tile["id"],
                        "tile_title": tile["title"],
                        "tile_type": tile.get("type", "column"),
                        "query_result": None,
                        "png": None,
                        "error": str(e),
                    }
                )

        return response_class(
            dashboard_id=dashboard["id"],
            dashboard_title=dashboard["title"],
            tiles=tile_results,
        )

    return FunctionalRepository(
        endpoint="dashboards_run",
        model_class=response_class,
        input_class=RunDashboardRequest,
        func=run_dashboard,
    )


def _create_run_dashboard_online_repository[T: BaseModel](
    response_class: type[T],
):
    """Create a custom repository for RunDashboardRequest in online mode.

    This repository fetches the dashboard, then runs each tile's query individually
    via the Looker API. The Looker API doesn't have a single endpoint to run all
    tiles, so we compose the response ourselves.

    Args:
        response_class: The response model class (RunDashboardResponse)

    Returns:
        Repository configured for online mode
    """
    from loguru import logger
    from models import GetDashboardRequest, GetDashboardResponse, QueryResult, RunDashboardRequest
    from utils.chart_renderer import render_chart

    async def run_dashboard_online(request: RunDashboardRequest) -> T:
        """Execute all tile queries for a dashboard via Looker API."""

        # Step 1: Get the dashboard to retrieve tile definitions
        dashboard_repo = create_repository(GetDashboardRequest, GetDashboardResponse)
        dashboard = await dashboard_repo.get(GetDashboardRequest(dashboard_id=request.dashboard_id))

        if not dashboard:
            raise ValueError(f"Dashboard {request.dashboard_id} not found")

        # Step 2: Run each tile's query with a shared HTTP client for connection pooling
        tile_results = []
        auth_service = _get_auth_service()

        base_url = settings.looker_base_url
        if not base_url:
            raise ValueError("LOOKER_BASE_URL environment variable required for online mode")

        base_url = base_url.rstrip("/")
        client = get_http_client()
        for tile in dashboard.tiles:
            tile_id = str(tile.id)
            tile_title = tile.title or f"Tile {tile_id}"

            # Skip tiles without query_id
            if not tile.query_id:
                tile_results.append(
                    {
                        "tile_id": tile_id,
                        "tile_title": tile_title,
                        "tile_type": getattr(tile, "type", None),
                        "query_result": None,
                        "png": None,
                        "error": "No query_id associated with this tile",
                    }
                )
                continue

            try:
                # Run the query via API
                query_id = str(tile.query_id)
                token = await auth_service.get_access_token()
                headers = {"Authorization": f"Bearer {token}"}

                # Build query parameters for filter overrides
                # Looker API accepts filters as query params: filters[field_name]=value
                params = {}
                if request.filters:
                    for field_name, values in request.filters.items():
                        # Looker expects comma-separated values for multi-value filters
                        if isinstance(values, list):
                            params[f"filters[{field_name}]"] = ",".join(str(v) for v in values)
                        else:
                            params[f"filters[{field_name}]"] = str(values)

                # Run the query and get JSON results
                url = f"{base_url}/api/4.0/queries/{query_id}/run/json"
                if params:
                    logger.debug(
                        f"Running query {query_id} for tile {tile_id} with filters: {params}"
                    )
                else:
                    logger.debug(f"Running query {query_id} for tile {tile_id}")
                response = await client.get(url, headers=headers, params=params)
                response.raise_for_status()
                data = response.json()

                # Convert to QueryResult format
                if isinstance(data, list) and len(data) > 0:
                    fields = list(data[0].keys()) if data else []
                    query_result = QueryResult(
                        data=data,
                        fields=fields,
                        row_count=len(data),
                        sql=f"-- Query {query_id}",
                    )
                else:
                    query_result = QueryResult(
                        data=[],
                        fields=[],
                        row_count=0,
                        sql=f"-- Query {query_id}",
                    )

                # Render PNG for visualization tiles (only if requested)
                tile_type = getattr(tile, "type", None) or "column"
                png_data = None
                should_render = (
                    request.include_png
                    and tile_type not in ("table",)
                    and query_result.data
                    and query_result.fields
                )
                if should_render:
                    try:
                        if tile_type.startswith("looker_"):
                            chart_type = tile_type
                        else:
                            chart_type = f"looker_{tile_type}"
                        png_data = render_chart(
                            data=query_result.data,
                            fields=query_result.fields,
                            chart_type=chart_type,
                            width=400,
                            height=300,
                            title=tile_title,
                        )
                    except Exception as e:
                        logger.warning(f"Failed to render PNG for tile {tile_id}: {e}")

                tile_results.append(
                    {
                        "tile_id": tile_id,
                        "tile_title": tile_title,
                        "tile_type": tile_type,
                        "query_result": query_result.model_dump(),
                        "png": png_data,
                        "error": None,
                    }
                )
            except Exception as e:
                logger.error(f"Error running query for tile {tile_id}: {e}")
                tile_results.append(
                    {
                        "tile_id": tile_id,
                        "tile_title": tile_title,
                        "tile_type": getattr(tile, "type", None),
                        "query_result": None,
                        "png": None,
                        "error": str(e),
                    }
                )

        return response_class(
            dashboard_id=dashboard.id,
            dashboard_title=dashboard.title,
            tiles=tile_results,
        )

    return FunctionalRepository(
        endpoint="dashboards_run_online",
        model_class=response_class,
        input_class=RunDashboardRequest,
        func=run_dashboard_online,
    )


def _create_run_query_png_repository[T: BaseModel](
    response_class: type[T],
):
    """Create a custom repository for RunQueryPngRequest.

    This repository executes a query by ID and renders the results as a PNG chart.
    It chains query execution with chart rendering.

    Args:
        response_class: The response model class (RunQueryPngResponse)

    Returns:
        Repository configured for offline mode
    """
    from models import QueryResult, RunQueryPngRequest
    from query_store import get_query_store
    from utils.chart_renderer import render_chart

    async def run_query_png(request: RunQueryPngRequest) -> T:
        """Execute query and render as PNG chart."""
        # First, execute the query to get the data
        query_repo = create_repository(RunQueryByIdRequest, QueryResult)
        query_result = await query_repo.get(RunQueryByIdRequest(query_id=request.query_id))

        if query_result is None:
            raise ValueError(f"API error 404: Query {request.query_id} not found")

        # Determine chart type: request param > query vis_config > default
        chart_type = "looker_column"  # Default

        # Use request.chart_type if provided (allows changing visualization without new query)
        if request.chart_type:
            chart_type = request.chart_type
        else:
            # Fall back to query store vis_config
            query_store = get_query_store()
            # Try both string and int versions for query_store lookup
            query_id = request.query_id
            query_id_int = None
            if isinstance(query_id, str) and query_id.isdigit():
                query_id_int = int(query_id)

            # Use whichever version exists in the store
            in_store = query_id in query_store
            in_store = in_store or (query_id_int is not None and query_id_int in query_store)
            if in_store:
                lookup_id = query_id if query_id in query_store else query_id_int
                query = query_store[lookup_id]
                # Check if query has vis_config
                if hasattr(query, "vis_config") and query.vis_config:
                    vis_config = query.vis_config
                    # Handle both VisConfig object and dict
                    if hasattr(vis_config, "type"):
                        # VisConfig model - extract string value from enum
                        chart_type_val = vis_config.type
                        if hasattr(chart_type_val, "value"):
                            vis_type = chart_type_val.value
                        else:
                            vis_type = chart_type_val
                    elif isinstance(vis_config, dict):
                        vis_type = vis_config.get("type")
                    else:
                        vis_type = None
                    if vis_type:  # Only use if not None/empty
                        chart_type = vis_type

        # Render the chart
        png_data = render_chart(
            data=query_result.data,
            fields=query_result.fields,
            chart_type=chart_type,
            width=request.width,
            height=request.height,
        )

        return response_class(
            query_id=request.query_id,
            image_data=png_data,
            content_type="image/png",
            width=request.width,
            height=request.height,
            chart_type=chart_type,
        )

    return FunctionalRepository(
        endpoint="queries_png",
        model_class=response_class,
        input_class=RunQueryPngRequest,
        func=run_query_png,
    )


def _create_run_look_live_repository[T: BaseModel](
    response_class: type[T],
):
    """Create a custom repository for RunLookRequest in online mode.

    The Looker API's /looks/{look_id}/run/json endpoint returns the query
    results as a bare list, not wrapped in a RunLookResponse structure. This repository
    makes the API call and wraps the response in the proper format.

    Args:
        response_class: The response model class (RunLookResponse)

    Returns:
        Repository configured for online mode with proper response wrapping
    """
    from models import RunLookRequest

    async def run_look_live(request: RunLookRequest) -> T:
        """Execute Look by ID via live Looker API and wrap response."""
        from loguru import logger

        # Get configuration
        base_url = settings.looker_base_url
        if not base_url:
            raise ValueError("LOOKER_BASE_URL environment variable required for online mode")

        base_url = base_url.rstrip("/")

        # Get auth token
        auth_service = _get_auth_service()
        access_token = await auth_service.get_access_token()

        # Build URL
        url = f"{base_url}/api/4.0/looks/{request.look_id}/run/json"

        headers = {
            "Authorization": f"Bearer {access_token}",
            "User-Agent": f"looker-mcp-server/{_get_version()}",
        }

        logger.info(f"Executing Look via Looker API: {url}")

        client = get_http_client()
        response = await client.get(url, headers=headers, timeout=30.0)
        response.raise_for_status()

        # The Looker API returns a bare list of result rows
        data = response.json()

        if not isinstance(data, list):
            raise ValueError(
                f"Unexpected response format from Looker API for Look {request.look_id}. "
                f"Expected list, got {type(data).__name__}"
            )

        # Extract field names from the first row if available
        fields = list(data[0].keys()) if data else []

        logger.info(
            f"Received Look results from Looker API: {len(data)} rows for Look {request.look_id}"
        )

        # Wrap in QueryResult dict, then in response_class
        # Use dict to avoid module mismatch issues
        query_result = {
            "data": data,
            "fields": fields,
            "row_count": len(data),
            "sql": None,  # Looker API doesn't return SQL for executed queries
        }

        return response_class(result=query_result)

    return FunctionalRepository(
        endpoint="looks_run_live",
        model_class=response_class,
        input_class=RunLookRequest,
        func=run_look_live,
    )


def _create_run_query_by_id_live_repository[T: BaseModel](
    response_class: type[T],
):
    """Create a custom repository for RunQueryByIdRequest in online mode.

    The Looker API's /queries/{query_id}/run/json endpoint returns the query
    results as a bare list, not wrapped in a QueryResult structure. This repository
    makes the API call and wraps the response in the proper QueryResult format.

    Args:
        response_class: The response model class (QueryResult)

    Returns:
        Repository configured for online mode with proper response wrapping
    """

    async def run_query_by_id_live(request: RunQueryByIdRequest) -> T:
        """Execute query by ID via live Looker API and wrap response."""
        from loguru import logger

        # Get configuration
        base_url = settings.looker_base_url
        if not base_url:
            raise ValueError("LOOKER_BASE_URL environment variable required for online mode")

        base_url = base_url.rstrip("/")

        # Get auth token
        auth_service = _get_auth_service()
        access_token = await auth_service.get_access_token()

        # Build URL
        url = f"{base_url}/api/4.0/queries/{request.query_id}/run/json"

        headers = {
            "Authorization": f"Bearer {access_token}",
            "User-Agent": f"looker-mcp-server/{_get_version()}",
        }

        logger.info(f"Executing query via Looker API: {url}")

        client = get_http_client()
        response = await client.get(url, headers=headers, timeout=30.0)
        response.raise_for_status()

        # The Looker API returns a bare list of result rows
        data = response.json()

        if not isinstance(data, list):
            raise ValueError(
                f"Unexpected response format from Looker API for query {request.query_id}. "
                f"Expected list, got {type(data).__name__}"
            )

        # Extract field names from the first row if available
        fields = list(data[0].keys()) if data else []

        logger.info(
            f"Received query results from Looker API: {len(data)} rows for query {request.query_id}"
        )

        # Wrap in QueryResult structure
        return response_class(
            data=data,
            fields=fields,
            row_count=len(data),
            sql=None,  # Looker API doesn't return SQL for executed queries
        )

    return FunctionalRepository(
        endpoint="queries_run_live",
        model_class=response_class,
        input_class=RunQueryByIdRequest,
        func=run_query_by_id_live,
    )


def _create_run_query_inline_live_repository[T: BaseModel](
    response_class: type[T],
):
    """Create a custom repository for RunQueryRequest (inline) in online mode.

    The Looker API's /queries/run/json endpoint returns the query
    results as a bare list, not wrapped in a QueryResult structure. This repository
    makes the API call and wraps the response in the proper QueryResult format.

    Args:
        response_class: The response model class (QueryResult)

    Returns:
        Repository configured for online mode with proper response wrapping
    """
    from models import RunQueryRequest

    async def run_query_inline_live(request: RunQueryRequest) -> T:
        """Execute inline query via live Looker API and wrap response."""
        from loguru import logger
        from utils.table_calculations import validate_expression

        # Validate dynamic fields against supported subset
        if request.dynamic_fields:
            for calc in request.dynamic_fields:
                if not validate_expression(calc.expression):
                    raise ValueError(
                        f"Unsupported table calculation: {calc.expression}. "
                        f"Supported: ${{field}} / sum(${{field}}) for percent of total, "
                        f"basic arithmetic (${{field1}} + - * / ${{field2}})"
                    )

        # Get configuration
        base_url = settings.looker_base_url
        if not base_url:
            raise ValueError("LOOKER_BASE_URL environment variable required for online mode")

        base_url = base_url.rstrip("/")

        # Get auth token
        auth_service = _get_auth_service()
        access_token = await auth_service.get_access_token()

        # Build URL
        url = f"{base_url}/api/4.0/queries/run/json"

        headers = {
            "Authorization": f"Bearer {access_token}",
            "User-Agent": f"looker-mcp-server/{_get_version()}",
            "Content-Type": "application/json",
        }

        # Convert request to API body format
        body = request.to_api_body()

        logger.info(f"Executing inline query via Looker API: {url}")

        client = get_http_client()
        response = await client.post(url, headers=headers, json=body, timeout=30.0)
        response.raise_for_status()

        # The Looker API returns a bare list of result rows
        data = response.json()

        if not isinstance(data, list):
            raise ValueError(
                f"Unexpected response format from Looker API for inline query. "
                f"Expected list, got {type(data).__name__}"
            )

        # Extract field names from the first row if available
        fields = list(data[0].keys()) if data else []

        logger.info(f"Received inline query results from Looker API: {len(data)} rows")

        # Wrap in QueryResult structure
        return response_class(
            data=data,
            fields=fields,
            row_count=len(data),
            sql=None,  # Looker API doesn't return SQL for executed queries
        )

    return FunctionalRepository(
        endpoint="queries_run_inline_live",
        model_class=response_class,
        input_class=RunQueryRequest,
        func=run_query_inline_live,
    )


def _create_run_query_png_live_repository[T: BaseModel](
    response_class: type[T],
):
    """Create a custom repository for RunQueryPngRequest in online mode.

    This repository handles the Looker API's binary PNG response format.
    The standard LiveDataRepository expects JSON responses, but the
    /queries/{query_id}/run/png endpoint returns raw binary PNG data.

    Args:
        response_class: The response model class (RunQueryPngResponse)

    Returns:
        Repository configured for online mode with binary response handling
    """
    import base64

    from loguru import logger
    from models import RunQueryPngRequest

    async def run_query_png_live(request: RunQueryPngRequest) -> T:
        """Execute query and return PNG from live Looker API."""
        # Get configuration
        base_url = settings.looker_base_url
        if not base_url:
            raise ValueError("LOOKER_BASE_URL environment variable required for online mode")

        base_url = base_url.rstrip("/")

        # Get auth token
        auth_service = _get_auth_service()
        access_token = await auth_service.get_access_token()

        # Build URL with optional width/height parameters
        url = f"{base_url}/api/4.0/queries/{request.query_id}/run/png"
        params = {}
        if request.width != 800:  # Only add if non-default
            params["width"] = request.width
        if request.height != 600:  # Only add if non-default
            params["height"] = request.height

        headers = {
            "Authorization": f"Bearer {access_token}",
            "User-Agent": f"looker-mcp-server/{_get_version()}",
        }

        logger.info(f"Fetching PNG from Looker API: {url}")

        client = get_http_client()
        response = await client.get(url, headers=headers, params=params, timeout=30.0)
        response.raise_for_status()

        # Get the binary PNG data
        png_bytes = response.content

        # Verify it's actually PNG data
        if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError(
                f"Looker API did not return valid PNG data for query {request.query_id}"
            )

        # Convert to base64 for the response
        png_base64 = base64.b64encode(png_bytes).decode("utf-8")

        logger.info(
            f"Received PNG from Looker API: {len(png_bytes)} bytes for query {request.query_id}"
        )

        return response_class(
            query_id=request.query_id,
            image_data=png_base64,
            content_type="image/png",
            width=request.width,
            height=request.height,
            chart_type="looker_visualization",  # We don't know the actual type from API
        )

    return FunctionalRepository(
        endpoint="queries_png_live",
        model_class=response_class,
        input_class=RunQueryPngRequest,
        func=run_query_png_live,
    )


def _extract_chart_type_from_vis_config(vis_config) -> str:
    """Extract chart type from vis_config, handling both VisConfig models and dicts.

    Args:
        vis_config: Either a VisConfig model or a dict with 'type' key

    Returns:
        Chart type string, defaults to 'looker_column' if not found
    """
    if not vis_config:
        return "looker_column"

    if hasattr(vis_config, "type") and vis_config.type:
        # VisConfig model - extract string value from enum
        chart_type_val = vis_config.type
        if hasattr(chart_type_val, "value"):
            return chart_type_val.value
        return str(chart_type_val)
    elif isinstance(vis_config, dict) and vis_config.get("type"):
        return vis_config["type"]

    return "looker_column"


def _create_run_look_pdf_repository[T: BaseModel](
    response_class: type[T],
):
    """Create a custom repository for RunLookPdfRequest.

    This repository executes a Look and renders results as a PDF document.

    Args:
        response_class: The response model class (RunLookPdfResponse)

    Returns:
        Repository configured for offline mode
    """
    from models import GetLookRequest, GetLookResponse, RunLookPdfRequest
    from utils.pdf_renderer import render_look_pdf

    async def run_look_pdf(request: RunLookPdfRequest) -> T:
        """Execute Look and render as PDF."""
        # First, get the Look to find its query
        look_repo = create_repository(GetLookRequest, GetLookResponse)
        look_response = await look_repo.get(GetLookRequest(look_id=request.look_id))

        if look_response is None:
            raise ValueError(f"API error 404: Look {request.look_id} not found")

        # GetLookResponse now extends Look directly, so use it as-is
        look = look_response

        # Get the query data
        from models import QueryResult

        if look.query_id is None:
            raise ValueError(f"Look {request.look_id} has no associated query")

        query_repo = create_repository(RunQueryByIdRequest, QueryResult)
        query_result = await query_repo.get(RunQueryByIdRequest(query_id=look.query_id))

        if query_result is None:
            raise ValueError(f"Query {look.query_id} not found for Look {request.look_id}")

        # Extract chart type from Look's vis_config
        chart_type = _extract_chart_type_from_vis_config(look.vis_config)

        # Render the PDF
        pdf_data = render_look_pdf(
            data=query_result.data,
            fields=query_result.fields,
            look_title=look.title,
            chart_type=chart_type,
            width=request.width,
            height=request.height,
        )

        return response_class(
            look_id=request.look_id,
            image_data=pdf_data,
            content_type="application/pdf",
            width=request.width,
            height=request.height,
        )

    return FunctionalRepository(
        endpoint="looks_pdf",
        model_class=response_class,
        input_class=RunLookPdfRequest,
        func=run_look_pdf,
    )


def _create_run_look_pdf_live_repository[T: BaseModel](
    response_class: type[T],
):
    """Create a custom repository for RunLookPdfRequest in online mode.

    NOTE: The Looker API does not support PDF format for Look render tasks (only PNG/JPG).
    This implementation follows the same approach as offline mode:
    1. Get the Look to find its query
    2. Run the query to get data
    3. Render PDF locally using the pdf_renderer utility

    Args:
        response_class: The response model class (RunLookPdfResponse)

    Returns:
        Repository configured for online mode with local PDF rendering
    """
    from models import (
        GetLookRequest,
        GetLookResponse,
        QueryResult,
        RunLookPdfRequest,
    )
    from utils.pdf_renderer import render_look_pdf

    async def run_look_pdf_live(request: RunLookPdfRequest) -> T:
        """Execute Look and render as PDF.

        Since Looker API doesn't support PDF for Looks (only PNG/JPG),
        we get the query data and render PDF locally.
        """
        # First, get the Look to find its query
        look_repo = create_repository(GetLookRequest, GetLookResponse)
        look_response = await look_repo.get(GetLookRequest(look_id=request.look_id))

        if look_response is None:
            raise ValueError(f"API error 404: Look {request.look_id} not found")

        # GetLookResponse now extends Look directly, so use it as-is
        look = look_response

        # Get the query data
        if look.query_id is None:
            raise ValueError(f"Look {request.look_id} has no associated query")

        query_repo = create_repository(RunQueryByIdRequest, QueryResult)
        query_result = await query_repo.get(RunQueryByIdRequest(query_id=look.query_id))

        if query_result is None:
            raise ValueError(f"Query {look.query_id} not found for Look {request.look_id}")

        # Extract chart type from Look's vis_config
        chart_type = _extract_chart_type_from_vis_config(look.vis_config)

        # Render the PDF locally (Looker API doesn't support PDF for Looks)
        pdf_data = render_look_pdf(
            data=query_result.data,
            fields=query_result.fields,
            look_title=look.title,
            chart_type=chart_type,
            width=request.width,
            height=request.height,
        )

        return response_class(
            look_id=request.look_id,
            image_data=pdf_data,
            content_type="application/pdf",
            width=request.width,
            height=request.height,
        )

    return FunctionalRepository(
        endpoint="looks_pdf_live",
        model_class=response_class,
        input_class=RunLookPdfRequest,
        func=run_look_pdf_live,
    )


def _create_run_dashboard_pdf_repository[T: BaseModel](
    response_class: type[T],
):
    """Create a custom repository for RunDashboardPdfRequest.

    This repository executes all dashboard tiles and renders as a PDF document.

    Args:
        response_class: The response model class (RunDashboardPdfResponse)

    Returns:
        Repository configured for offline mode
    """
    from models import GetDashboardRequest, GetDashboardResponse, RunDashboardPdfRequest
    from utils.pdf_renderer import render_dashboard_pdf

    async def run_dashboard_pdf(request: RunDashboardPdfRequest) -> T:
        """Execute Dashboard tiles and render as PDF."""
        # First, get the Dashboard
        dashboard_repo = create_repository(GetDashboardRequest, GetDashboardResponse)
        dashboard = await dashboard_repo.get(GetDashboardRequest(dashboard_id=request.dashboard_id))

        if dashboard is None:
            raise ValueError(f"API error 404: Dashboard {request.dashboard_id} not found")

        # Collect tile data
        tiles_data = []
        from loguru import logger as pdf_logger
        from models import QueryFilter, QueryResult, RunQueryRequest

        for tile in dashboard.tiles:
            tile_info = {
                "title": tile.title,
                "data": [],
                "fields": [],
                "chart_type": tile.type if tile.type else "looker_column",
            }

            # Get query data for the tile using inline query
            if tile.query:
                try:
                    query_config = tile.query
                    # Convert dict filters to list of QueryFilter objects
                    raw_filters = query_config.get("filters", {})
                    filter_list = []
                    if isinstance(raw_filters, dict):
                        for field, value in raw_filters.items():
                            # Value can be a list or string
                            if isinstance(value, list):
                                filter_list.append(
                                    QueryFilter(field=field, value=",".join(str(v) for v in value))
                                )
                            else:
                                filter_list.append(QueryFilter(field=field, value=str(value)))
                    elif isinstance(raw_filters, list):
                        filter_list = [
                            QueryFilter(**f) if isinstance(f, dict) else f for f in raw_filters
                        ]

                    inline_request = RunQueryRequest(
                        model=query_config.get("model", ""),
                        view=query_config.get("view", ""),
                        fields=query_config.get("fields", []),
                        filters=filter_list,
                        sorts=query_config.get("sorts", []),
                        limit=query_config.get("limit", 500),
                    )
                    query_repo = create_repository(RunQueryRequest, QueryResult)
                    query_result = await query_repo.get(inline_request)
                    if query_result:
                        tile_info["data"] = query_result.data
                        tile_info["fields"] = query_result.fields
                except Exception as e:
                    pdf_logger.warning(f"Error getting data for tile {tile.title}: {e}")

            tiles_data.append(tile_info)

        # Render the PDF
        pdf_data = render_dashboard_pdf(
            tiles=tiles_data,
            dashboard_title=dashboard.title,
            width=request.width,
            height=request.height,
        )

        return response_class(
            dashboard_id=request.dashboard_id,
            image_data=pdf_data,
            content_type="application/pdf",
            width=request.width,
            height=request.height,
        )

    return FunctionalRepository(
        endpoint="dashboards_pdf",
        model_class=response_class,
        input_class=RunDashboardPdfRequest,
        func=run_dashboard_pdf,
    )


def _create_run_dashboard_pdf_live_repository[T: BaseModel](
    response_class: type[T],
):
    """Create a custom repository for RunDashboardPdfRequest in online mode.

    This repository handles the Looker API's binary PDF response format.

    Args:
        response_class: The response model class (RunDashboardPdfResponse)

    Returns:
        Repository configured for online mode with binary response handling
    """
    import base64

    from loguru import logger
    from models import RunDashboardPdfRequest

    async def run_dashboard_pdf_live(request: RunDashboardPdfRequest) -> T:
        """Execute Dashboard and return PDF from live Looker API.

        Uses the Looker render task workflow:
        1. POST /render_tasks/dashboards/{dashboard_id}/pdf - creates render task
        2. GET /render_tasks/{task_id} - poll until complete
        3. GET /render_tasks/{task_id}/results - download PDF
        """
        base_url = settings.looker_base_url
        if not base_url:
            raise ValueError("LOOKER_BASE_URL environment variable required for online mode")

        base_url = base_url.rstrip("/")

        # Get auth token
        auth_service = _get_auth_service()
        access_token = await auth_service.get_access_token()

        headers = {
            "Authorization": f"Bearer {access_token}",
            "User-Agent": f"looker-mcp-server/{_get_version()}",
            "Content-Type": "application/json",
        }

        client = get_http_client()

        # Step 1: Create render task
        # width and height are query parameters per Looker API spec
        create_url = f"{base_url}/api/4.0/render_tasks/dashboards/{request.dashboard_id}/pdf"
        query_params = {
            "width": request.width,
            "height": request.height,
        }
        # Body contains dashboard-specific render options
        create_body = {
            "dashboard_style": "tiled",
        }

        logger.info(f"Creating Dashboard PDF render task: {create_url}")
        response = await client.post(
            create_url, headers=headers, params=query_params, json=create_body, timeout=30.0
        )
        response.raise_for_status()
        task_data = response.json()
        task_id = task_data.get("id")

        if not task_id:
            raise ValueError(
                f"Looker API did not return render task ID for Dashboard {request.dashboard_id}"
            )

        logger.info(f"Created render task {task_id} for Dashboard {request.dashboard_id}")

        # Step 2: Poll for completion
        poll_url = f"{base_url}/api/4.0/render_tasks/{task_id}"
        max_polls = 120  # Dashboards may take longer, max 2 minutes
        poll_interval = 1.0

        for _ in range(max_polls):
            await asyncio.sleep(poll_interval)
            poll_response = await client.get(poll_url, headers=headers, timeout=30.0)
            poll_response.raise_for_status()
            status_data = poll_response.json()

            status = status_data.get("status")
            if status in ("complete", "success"):
                logger.info(f"Render task {task_id} complete (status={status})")
                break
            elif status == "failure":
                error_msg = status_data.get("status_detail", "Unknown error")
                raise ValueError(
                    f"Render task failed for Dashboard {request.dashboard_id}: {error_msg}"
                )
            # Still running, continue polling
        else:
            raise ValueError(
                f"Render task {task_id} timed out for Dashboard {request.dashboard_id}"
            )

        # Step 3: Download results
        results_url = f"{base_url}/api/4.0/render_tasks/{task_id}/results"
        results_response = await client.get(results_url, headers=headers, timeout=60.0)
        results_response.raise_for_status()

        pdf_bytes = results_response.content

        # Verify it's PDF data
        if not pdf_bytes.startswith(b"%PDF"):
            raise ValueError(
                f"Looker API did not return valid PDF data for Dashboard {request.dashboard_id}"
            )

        pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")

        logger.info(
            f"Received PDF from Looker API: {len(pdf_bytes)} bytes "
            f"for Dashboard {request.dashboard_id}"
        )

        return response_class(
            dashboard_id=request.dashboard_id,
            image_data=pdf_base64,
            content_type="application/pdf",
            width=request.width,
            height=request.height,
        )

    return FunctionalRepository(
        endpoint="dashboards_pdf_live",
        model_class=response_class,
        input_class=RunDashboardPdfRequest,
        func=run_dashboard_pdf_live,
    )


def _create_sql_runner_repository[T: BaseModel](
    response_class: type[T],
):
    """Create a custom repository for RunSqlRequest.

    This repository handles the Looker SQL Runner API's 2-step workflow:
    1. Create SQL query (POST /sql_queries) -> returns slug
    2. Run query (POST /sql_queries/{slug}/run/json) -> returns results

    In offline mode, executes against DuckDB using CSV data.
    In online mode, executes the 2-step workflow against Looker API.

    Args:
        response_class: The response model class (SqlQueryResult)

    Returns:
        Repository configured for current mode
    """
    import time

    from models import RunSqlRequest

    if settings.is_offline_mode():
        # Offline mode: execute against DuckDB using CSV data
        import re

        def _extract_table_from_sql(sql: str) -> str | None:
            """Extract table name from SQL query.

            Handles common patterns like:
            - SELECT * FROM table_name
            - SELECT * FROM table_name LIMIT 10
            - SELECT col1, col2 FROM table_name WHERE ...
            """
            # Normalize whitespace and case for matching
            normalized = " ".join(sql.split())

            # Match FROM clause - captures table name after FROM
            # Handles: FROM table, FROM table LIMIT, FROM table WHERE, etc.
            pattern = r"\bFROM\s+([a-zA-Z_][a-zA-Z0-9_]*)\b"
            match = re.search(pattern, normalized, re.IGNORECASE)
            if match:
                return match.group(1).lower()
            return None

        def _convert_csv_row_to_sql_format(row: dict, table_name: str) -> dict:
            """Convert CSV row with Looker-style keys to SQL-style keys.

            CSV headers: table_name.column_name -> column_name
            """
            result = {}
            prefix = f"{table_name}."
            for key, value in row.items():
                # Remove table prefix if present
                if key.startswith(prefix):
                    new_key = key[len(prefix) :]
                else:
                    new_key = key
                result[new_key] = value
            return result

        async def execute_sql_offline(request: RunSqlRequest) -> T:
            """Execute SQL in offline mode against DuckDB database."""
            import asyncio

            import duckdb
            from data_layer import get_runtime_duckdb_path

            start_time = time.time()

            # Runtime DuckDB is in STATE_LOCATION (copied from bundled on startup)
            db_path = get_runtime_duckdb_path()

            if not db_path.exists():
                raise ValueError(
                    f"Runtime database not found at {db_path}. "
                    f"This should have been copied from bundled DB at startup."
                )

            def _execute_query() -> tuple[list[dict], list[str]]:
                """Execute query synchronously (DuckDB doesn't have async driver)."""
                conn = duckdb.connect(str(db_path), read_only=True)
                try:
                    # Note: DuckDB's search_path only accepts a single schema.
                    # Tables in 'main' schema are found by default.
                    # Tables in 'public' schema need explicit prefix (public.table_name).
                    result = conn.execute(request.sql)
                    fields = [desc[0] for desc in result.description] if result.description else []
                    rows = result.fetchall()
                    data = [dict(zip(fields, row)) for row in rows]
                    return data, fields
                finally:
                    conn.close()

            try:
                data, fields = await asyncio.to_thread(_execute_query)

                # Apply limit to results
                data = data[: request.limit]
                runtime = time.time() - start_time

                return response_class(
                    data=data,
                    fields=fields,
                    row_count=len(data),
                    runtime_seconds=round(runtime, 3),
                    connection=request.connection,
                    sql=request.sql,
                )
            except Exception as e:
                raise ValueError(f"SQL query failed: {e}") from e

        return FunctionalRepository(
            endpoint="sql_queries",
            model_class=response_class,
            input_class=RunSqlRequest,
            func=execute_sql_offline,
        )
    else:
        # Online mode: implement 2-step workflow
        async def execute_sql_online(request: RunSqlRequest) -> T:
            """Execute SQL query using Looker's 2-step API workflow.

            Step 1: Create SQL query (POST /sql_queries) -> get slug
            Step 2: Run query (POST /sql_queries/{slug}/run/json) -> get results
            """

            # Get auth service
            auth_service = _get_auth_service()
            access_token = await auth_service.get_access_token()

            base_url = settings.looker_base_url.rstrip("/")
            headers = {
                "Authorization": f"token {access_token}",
                "Content-Type": "application/json",
                "User-Agent": f"looker-mcp-server/{_get_version()}",
            }

            # Validate connection name (alphanumeric with underscores/hyphens)
            if not request.connection.replace("_", "").replace("-", "").isalnum():
                raise ValueError(
                    f"Invalid connection name: {request.connection}. "
                    f"Only alphanumeric characters, hyphens, and underscores are allowed."
                )

            client = get_http_client()

            # Step 1: Create SQL query
            # Looker's run_sql_query endpoint does NOT support a limit parameter.
            # We must enforce the limit by modifying the SQL query itself.
            sql_with_limit = _apply_sql_limit(request.sql, request.limit)

            create_payload = {
                "connection_name": request.connection,
                "sql": sql_with_limit,
            }

            create_response = await client.post(
                f"{base_url}/api/4.0/sql_queries",
                headers=headers,
                json=create_payload,
                timeout=settings.looker_sql_create_timeout,
            )

            if create_response.status_code != 200:
                raise ValueError(
                    f"API error {create_response.status_code}: "
                    f"Failed to create SQL query - {create_response.text}"
                )

            create_data = create_response.json()
            slug = create_data.get("slug")

            if not slug:
                raise ValueError("API error: No slug returned from create_sql_query")

            # Validate slug is alphanumeric (prevent SQL injection via URL manipulation)
            if not slug.replace("_", "").replace("-", "").isalnum():
                raise ValueError(
                    f"Invalid slug format returned from API: {slug}. "
                    f"Expected alphanumeric characters only."
                )

            # Step 2: Run the query with slug
            # Note: Limit is already applied in the SQL query itself (Step 1)
            run_url = f"{base_url}/api/4.0/sql_queries/{slug}/run/json"

            run_response = await client.post(
                run_url,
                headers=headers,
                timeout=settings.looker_sql_run_timeout,
            )

            if run_response.status_code != 200:
                raise ValueError(
                    f"API error {run_response.status_code}: "
                    f"Failed to run SQL query - {run_response.text}"
                )

            # Parse the result
            result_data = run_response.json()

            # Looker returns results in various formats depending on result_format
            # For "json" format, it returns an array of objects
            if isinstance(result_data, list):
                # Extract field names from first row if available
                fields = list(result_data[0].keys()) if result_data else []

                return response_class(
                    data=result_data,
                    fields=fields,
                    row_count=len(result_data),
                    runtime_seconds=None,  # Not provided in response
                    connection=request.connection,
                    sql=sql_with_limit,  # Return the actual executed SQL with LIMIT
                )
            else:
                raise ValueError(f"Unexpected response format from SQL Runner: {type(result_data)}")

        return FunctionalRepository(
            endpoint="sql_queries",
            model_class=response_class,
            input_class=RunSqlRequest,
            func=execute_sql_online,
        )
