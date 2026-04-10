"""Content management tools for Looker.

Tools for creating Looks and managing Dashboard tiles.
"""

import sys
from pathlib import Path

# Add server to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import settings
from http_client import get_http_client
from loguru import logger

from tools.v2.models import (
    AddTileRequest,
    AddTileResponse,
    CreateDashboardRequest,
    CreateDashboardResponse,
    CreateLookRequest,
    CreateLookResponse,
)


async def create_dashboard(
    request: CreateDashboardRequest,
) -> CreateDashboardResponse:
    """Create a new Dashboard."""
    if settings.is_offline_mode():
        return await _create_dashboard_mock(request)
    else:
        return await _create_dashboard_live(request)


async def _create_dashboard_mock(
    request: CreateDashboardRequest,
) -> CreateDashboardResponse:
    """Create a mock Dashboard (offline mode)."""
    from query_store import get_dashboard_store, get_next_dashboard_id, get_query_lock
    from state_persistence import save_dashboard

    # Generate a deterministic Dashboard ID using incrementing counter
    dashboard_id = str(get_next_dashboard_id())

    # Build dashboard data
    dashboard_data = {
        "id": dashboard_id,
        "title": request.title,
        "description": request.description,
        "folder_id": request.folder_id,
        "url": f"/dashboards/{dashboard_id}",
        "tiles": [],  # Start with empty tiles
        "filters": [],
        "created_at": None,
        "updated_at": None,
    }

    # Store in dashboard_store and persist to file (thread-safe)
    dashboard_store = get_dashboard_store()
    with get_query_lock():
        dashboard_store[dashboard_id] = dashboard_data
        # Also persist to STATE_LOCATION for snapshot capture
        save_dashboard(dashboard_id, dashboard_data)

    logger.info(f"Created dashboard {dashboard_id} in offline mode")

    return CreateDashboardResponse(
        dashboard_id=dashboard_id,
        title=request.title,
        url=f"/dashboards/{dashboard_id}",
    )


async def _create_dashboard_live(
    request: CreateDashboardRequest,
) -> CreateDashboardResponse:
    """Create a Dashboard via Looker API (online mode)."""
    import httpx

    # Get auth service for token management
    from auth import LookerAuthService

    auth_service = LookerAuthService(
        base_url=settings.looker_base_url,
        client_id=settings.looker_client_id,
        client_secret=settings.looker_client_secret,
        verify_ssl=settings.looker_verify_ssl,
        timeout=settings.looker_timeout,
    )

    # Get access token
    access_token = await auth_service.get_access_token()

    # Build request body
    body = {
        "title": request.title,
        "folder_id": request.folder_id,
    }

    # Add optional fields
    if request.description:
        body["description"] = request.description

    # Make API call
    url = f"{settings.looker_base_url}/api/4.0/dashboards"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": f"looker-mcp-server/{_get_version()}",
    }

    logger.info(f"Creating Dashboard via API: {url}")

    client = get_http_client()
    try:
        response = await client.post(
            url,
            json=body,
            headers=headers,
            timeout=settings.looker_timeout,
        )
        response.raise_for_status()
        data = response.json()

        logger.info(f"Dashboard created successfully: {data.get('id')}")

        # Map Looker API response to our response model
        return CreateDashboardResponse(
            dashboard_id=str(data["id"]),
            title=data["title"],
            url=f"/dashboards/{data['id']}",
        )

    except httpx.HTTPStatusError as e:
        error_msg = f"API error: {e.response.status_code}"
        logger.error(f"{error_msg} - {e.response.text}")
        raise ValueError(f"{error_msg} - {e.response.text}") from e
    except httpx.RequestError as e:
        error_msg = f"Request failed: {str(e)}"
        logger.error(error_msg)
        raise ValueError(error_msg) from e


async def create_look(request: CreateLookRequest) -> CreateLookResponse:
    """Create a new saved Look from a query."""
    if settings.is_offline_mode():
        return await _create_look_mock(request)
    else:
        return await _create_look_live(request)


async def _create_look_mock(request: CreateLookRequest) -> CreateLookResponse:
    """Create a mock Look (offline mode)."""
    # Store the Look in the shared look store so it can be retrieved later
    from models import Look
    from query_store import get_look_store, get_next_look_id, get_query_lock, get_query_store

    # Generate a deterministic Look ID using incrementing counter
    look_id = str(get_next_look_id())

    # Get vis_config from request, or fall back to query's vis_config if available
    vis_config = request.vis_config
    if not vis_config and request.query_id:
        query_store = get_query_store()
        query_id_int = (
            int(request.query_id) if str(request.query_id).isdigit() else request.query_id
        )
        query = query_store.get(request.query_id) or query_store.get(query_id_int)
        if query and hasattr(query, "vis_config") and query.vis_config:
            # Convert VisConfig to dict if needed
            if hasattr(query.vis_config, "model_dump"):
                vis_config = query.vis_config.model_dump()
            elif hasattr(query.vis_config, "type"):
                vis_type = query.vis_config.type
                if hasattr(vis_type, "value"):
                    vis_config = {"type": vis_type.value}
                else:
                    vis_config = {"type": vis_type}
            else:
                vis_config = query.vis_config

    look = Look(
        id=look_id,
        title=request.title,
        description=request.description,
        folder_id=request.folder_id,
        query_id=request.query_id,
        vis_config=vis_config,
    )

    # Store in look store and persist to file (thread-safe)
    from state_persistence import save_look

    look_store = get_look_store()
    with get_query_lock():
        look_store[look_id] = look

        # Persist look to STATE_LOCATION for snapshot capture (inside lock to prevent race)
        look_data = {
            "id": look_id,
            "title": request.title,
            "description": request.description,
            "folder_id": request.folder_id,
            "query_id": request.query_id,
            "vis_config": vis_config,
            "url": f"/looks/{look_id}",
        }
        save_look(look_id, look_data)

    return CreateLookResponse(
        look_id=look_id,
        title=request.title,
        url=f"/looks/{look_id}",
    )


async def _create_look_live(request: CreateLookRequest) -> CreateLookResponse:
    """Create a Look via Looker API (online mode)."""
    import httpx

    # Get auth service for token management
    from auth import LookerAuthService

    auth_service = LookerAuthService(
        base_url=settings.looker_base_url,
        client_id=settings.looker_client_id,
        client_secret=settings.looker_client_secret,
        verify_ssl=settings.looker_verify_ssl,
        timeout=settings.looker_timeout,
    )

    # Get access token
    access_token = await auth_service.get_access_token()

    # Build request body
    body = {
        "title": request.title,
        "query_id": request.query_id,
        "folder_id": request.folder_id,
    }

    # Add optional fields
    if request.description:
        body["description"] = request.description

    # Make API call
    url = f"{settings.looker_base_url}/api/4.0/looks"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": f"looker-mcp-server/{_get_version()}",
    }

    logger.info(f"Creating Look via API: {url}")

    client = get_http_client()
    try:
        response = await client.post(
            url,
            json=body,
            headers=headers,
            timeout=settings.looker_timeout,
        )
        response.raise_for_status()
        data = response.json()

        logger.info(f"Look created successfully: {data.get('id')}")

        # Map Looker API response to our response model
        return CreateLookResponse(
            look_id=str(data["id"]),
            title=data["title"],
            url=f"/looks/{data['id']}",
        )

    except httpx.HTTPStatusError as e:
        error_msg = f"API error: {e.response.status_code}"
        logger.error(f"{error_msg} - {e.response.text}")
        raise ValueError(f"{error_msg} - {e.response.text}") from e
    except httpx.RequestError as e:
        error_msg = f"Request failed: {str(e)}"
        logger.error(error_msg)
        raise ValueError(error_msg) from e


async def add_tile_to_dashboard(request: AddTileRequest) -> AddTileResponse:
    """Add a tile/element to a Dashboard."""
    if settings.is_offline_mode():
        return await _add_tile_to_dashboard_mock(request)
    else:
        return await _add_tile_to_dashboard_live(request)


async def _add_tile_to_dashboard_mock(request: AddTileRequest) -> AddTileResponse:
    """Add a mock tile to a Dashboard (offline mode)."""
    from query_store import (
        get_dashboard_tile_store,
        get_look_store,
        get_next_tile_id,
        get_query_lock,
        get_query_store,
    )

    # Generate a deterministic element ID using incrementing counter
    element_id = f"elem_{get_next_tile_id()}"

    # Build the query definition for the tile
    query_def: dict | None = None
    look = None  # Will be set if look_id is provided

    if request.query_id:
        # Look up query from query store
        query_store = get_query_store()
        query_id = request.query_id
        # Try both string and int keys
        query = query_store.get(query_id)
        if not query:
            try:
                query = query_store.get(int(query_id))
            except (ValueError, TypeError):
                pass  # Non-numeric query_id, skip int lookup
        if query:
            query_def = {
                "model": query.model,
                "view": query.view,
                "fields": query.fields,
                "filters": query.filters or {},
                "sorts": query.sorts or [],
                "limit": query.limit or 500,
            }
            logger.debug(f"Found query {query_id} for tile: {query_def}")

    elif request.look_id:
        # Look up look from look store (dynamic) and LOOKS (static)
        from stores import LOOKS

        look_store = get_look_store()
        look_id = request.look_id
        look = look_store.get(look_id) or look_store.get(str(look_id))

        # Also check LOOKS if not found in dynamic store
        if not look:
            for static_look in LOOKS:
                if str(static_look.id) == str(look_id):
                    look = static_look
                    break

        if look and look.query_id:
            # First check dynamic query_store
            query_store = get_query_store()
            query = query_store.get(look.query_id)
            if not query:
                try:
                    query = query_store.get(int(look.query_id))
                except (ValueError, TypeError):
                    pass  # Non-numeric query_id, skip int lookup

            if query:
                query_def = {
                    "model": query.model,
                    "view": query.view,
                    "fields": query.fields,
                    "filters": query.filters or {},
                    "sorts": query.sorts or [],
                    "limit": query.limit or 500,
                }
                logger.debug(f"Found query via look {look_id} for tile: {query_def}")

    # Determine chart type from request, with sensible defaults
    # Priority: explicit chart_type > type field > look's vis_config > default "column"
    if request.chart_type:
        chart_type = request.chart_type
    elif request.type and request.type not in ("vis", "text"):
        # User specified a chart type in the type field (e.g., "bar", "column", "line")
        chart_type = request.type
    elif request.look_id and look and hasattr(look, "vis_config") and look.vis_config:
        # Get chart type from Look's vis_config
        vis_config = look.vis_config
        if isinstance(vis_config, dict):
            chart_type = vis_config.get("type") or "column"
        elif hasattr(vis_config, "type"):
            vis_type = vis_config.type
            if hasattr(vis_type, "value"):
                chart_type = vis_type.value or "column"
            else:
                chart_type = vis_type or "column"
        else:
            chart_type = "column"
        # Remove 'looker_' prefix if present for consistency
        if chart_type and chart_type.startswith("looker_"):
            chart_type = chart_type[7:]  # Remove 'looker_' prefix
    else:
        # Default to column (vertical bars) to match run_look_pdf behavior
        chart_type = "column"

    # Create tile data structure matching DashboardTile format
    tile_data = {
        "id": element_id,
        "title": request.title or f"Tile {element_id}",
        "type": chart_type,
        "query": query_def or {},
        "query_id": request.query_id,  # Include query_id for frontend to fetch data
        "look_id": request.look_id,  # Include look_id if provided
    }

    # Store the tile in the dashboard tile store (thread-safe)
    # Always use string keys for consistency with dashboard creation
    dashboard_id_key = str(request.dashboard_id)

    # Use lock to prevent race conditions when multiple threads add tiles
    from state_persistence import add_tile_to_dashboard_persistent

    with get_query_lock():
        dashboard_tile_store = get_dashboard_tile_store()
        if dashboard_id_key not in dashboard_tile_store:
            dashboard_tile_store[dashboard_id_key] = []
        dashboard_tile_store[dashboard_id_key].append(tile_data)

        # Persist tile to STATE_LOCATION for snapshot capture (inside lock to prevent race)
        add_tile_to_dashboard_persistent(dashboard_id_key, tile_data)

    logger.info(
        f"Added tile {element_id} to dashboard {dashboard_id_key} with query: {bool(query_def)}"
    )

    return AddTileResponse(
        dashboard_element_id=element_id,
        dashboard_id=request.dashboard_id,
    )


async def _add_tile_to_dashboard_live(request: AddTileRequest) -> AddTileResponse:
    """Add a tile to a Dashboard via Looker API (online mode)."""
    import httpx

    # Get auth service for token management
    from auth import LookerAuthService

    auth_service = LookerAuthService(
        base_url=settings.looker_base_url,
        client_id=settings.looker_client_id,
        client_secret=settings.looker_client_secret,
        verify_ssl=settings.looker_verify_ssl,
        timeout=settings.looker_timeout,
    )

    # Get access token
    access_token = await auth_service.get_access_token()

    # Build request body
    body = {
        "dashboard_id": request.dashboard_id,
        "type": request.type,
    }

    # Add optional fields
    if request.title:
        body["title"] = request.title
    if request.query_id:
        body["query_id"] = request.query_id
    if request.look_id:
        body["look_id"] = request.look_id
    if request.chart_type:
        # Set visualization config for the chart type
        body["vis_config"] = {"type": request.chart_type}

    # Make API call
    url = f"{settings.looker_base_url}/api/4.0/dashboard_elements"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": f"looker-mcp-server/{_get_version()}",
    }

    logger.info(f"Adding tile to dashboard via API: {url}")

    client = get_http_client()
    try:
        response = await client.post(
            url,
            json=body,
            headers=headers,
            timeout=settings.looker_timeout,
        )
        response.raise_for_status()
        data = response.json()

        logger.info(
            f"Dashboard element created successfully: {data.get('id')} "
            f"for dashboard {request.dashboard_id}"
        )

        # Map Looker API response to our response model
        return AddTileResponse(
            dashboard_element_id=str(data["id"]),
            dashboard_id=request.dashboard_id,
        )

    except httpx.HTTPStatusError as e:
        error_msg = f"API error: {e.response.status_code}"
        logger.error(f"{error_msg} - {e.response.text}")
        raise ValueError(f"{error_msg} - {e.response.text}") from e
    except httpx.RequestError as e:
        error_msg = f"Request failed: {str(e)}"
        logger.error(error_msg)
        raise ValueError(error_msg) from e


def _get_version() -> str:
    """Get version from pyproject.toml."""
    try:
        import tomllib
        from pathlib import Path

        # Find pyproject.toml - go up from this file's directory
        repo_root = Path(__file__).parent.parent
        pyproject_path = repo_root / "pyproject.toml"

        if pyproject_path.exists():
            with open(pyproject_path, "rb") as f:
                data = tomllib.load(f)
                return data.get("project", {}).get("version", "0.0.0")
    except Exception:
        pass
    return "0.0.0"
