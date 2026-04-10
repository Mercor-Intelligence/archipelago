"""Unified store accessors for merging static and dynamic data.

This module provides a clean abstraction over the multiple data sources
(static stores and dynamic stores) used in offline mode. Repository functions
should use these accessors instead of manually iterating and merging.

Design pattern: Facade pattern - provides a simplified interface to a
complex subsystem (multiple stores).
"""

from collections.abc import Iterator

from models import Look


def get_all_looks() -> Iterator[Look]:
    """Yield all looks from both static and dynamic stores.

    Returns an iterator to avoid creating unnecessary copies.
    Static looks are yielded first, then dynamic looks.

    Yields:
        Look objects from all sources
    """
    from query_store import get_look_store
    from stores import LOOKS

    # Yield static looks
    yield from LOOKS

    # Yield dynamic looks
    look_store = get_look_store()
    yield from look_store.values()


def find_look_by_id(look_id: str | int) -> Look | None:
    """Find a look by ID in any source.

    Searches static store first, then dynamic store.
    Handles both string and int ID comparisons.

    Args:
        look_id: The look ID to find

    Returns:
        Look object if found, None otherwise
    """
    from query_store import get_look_store
    from stores import LOOKS

    # Check static looks first
    str_id = str(look_id)
    for look in LOOKS:
        if str(look.id) == str_id:
            return look

    # Check dynamic store
    look_store = get_look_store()
    look = look_store.get(look_id)
    if look:
        return look
    # Try string conversion
    return look_store.get(str_id)


def get_all_dashboards() -> Iterator[dict]:
    """Yield all dashboards from both static and dynamic stores.

    Dashboards are returned as dicts with a consistent structure.
    Static dashboards are yielded first, then dynamic dashboards.

    Yields:
        Dashboard dicts with keys: id, title, description, folder_id,
        tiles (list), filters (list), created_at, updated_at
    """
    from query_store import get_dashboard_store, get_dashboard_tile_store
    from stores import DASHBOARDS

    dashboard_tile_store = get_dashboard_tile_store()

    # Yield static dashboards with consistent structure
    for dashboard in DASHBOARDS.values():
        # Include any dynamically added tiles (try both int and string keys)
        dynamic_tiles = dashboard_tile_store.get(dashboard.id, [])
        if not dynamic_tiles:
            dynamic_tiles = dashboard_tile_store.get(str(dashboard.id), [])
        all_tiles = [
            {"id": t.id, "title": t.title, "type": t.type, "query": t.query}
            for t in dashboard.tiles
        ] + dynamic_tiles

        yield {
            "id": dashboard.id,
            "title": dashboard.title,
            "description": dashboard.description,
            "folder_id": dashboard.folder_id,
            "tiles": all_tiles,
            "filters": dashboard.filters,
            "created_at": dashboard.created_at,
            "updated_at": dashboard.updated_at,
            "_is_static": True,
        }

    # Yield dynamic dashboards
    dashboard_store = get_dashboard_store()
    for dash_id, dash_data in dashboard_store.items():
        # Get any tiles added to this dashboard
        dynamic_tiles = dashboard_tile_store.get(dash_id, [])
        if not dynamic_tiles:
            dynamic_tiles = dashboard_tile_store.get(str(dash_id), [])

        yield {
            "id": dash_data.get("id", dash_id),
            "title": dash_data.get("title", "Untitled"),
            "description": dash_data.get("description"),
            "folder_id": dash_data.get("folder_id"),
            "tiles": dynamic_tiles,
            "filters": dash_data.get("filters", []),
            "created_at": dash_data.get("created_at"),
            "updated_at": dash_data.get("updated_at"),
            "_is_static": False,
        }


def find_dashboard_by_id(dashboard_id: str | int) -> dict | None:
    """Find a dashboard by ID in any source.

    Searches static store first, then dynamic store.
    Handles both string and int ID comparisons.

    Args:
        dashboard_id: The dashboard ID to find

    Returns:
        Dashboard dict if found, None otherwise
    """
    from query_store import get_dashboard_store, get_dashboard_tile_store
    from stores import DASHBOARDS

    dashboard_tile_store = get_dashboard_tile_store()
    str_id = str(dashboard_id)

    # Check static dashboards first (try direct lookup, then int conversion)
    dashboard = DASHBOARDS.get(dashboard_id)
    if not dashboard and isinstance(dashboard_id, str):
        try:
            dashboard = DASHBOARDS.get(int(dashboard_id))
        except ValueError:
            pass

    if dashboard:
        # Include any dynamically added tiles (try both int and string keys)
        dynamic_tiles = dashboard_tile_store.get(dashboard.id, [])
        if not dynamic_tiles:
            dynamic_tiles = dashboard_tile_store.get(str(dashboard.id), [])
        all_tiles = [
            {"id": t.id, "title": t.title, "type": t.type, "query": t.query}
            for t in dashboard.tiles
        ] + dynamic_tiles

        return {
            "id": dashboard.id,
            "title": dashboard.title,
            "description": dashboard.description,
            "folder_id": dashboard.folder_id,
            "tiles": all_tiles,
            "filters": dashboard.filters,
            "created_at": dashboard.created_at,
            "updated_at": dashboard.updated_at,
        }

    # Check dynamic store
    dashboard_store = get_dashboard_store()
    dash_data = dashboard_store.get(dashboard_id)
    if not dash_data:
        dash_data = dashboard_store.get(str_id)

    if dash_data:
        dash_id = dash_data.get("id", dashboard_id)
        # Get any tiles added to this dashboard
        tiles = dashboard_tile_store.get(dash_id, [])
        if not tiles:
            tiles = dashboard_tile_store.get(str(dash_id), [])

        return {
            "id": dash_id,
            "title": dash_data.get("title", "Untitled"),
            "description": dash_data.get("description"),
            "folder_id": dash_data.get("folder_id"),
            "tiles": tiles,
            "filters": dash_data.get("filters", []),
            "created_at": dash_data.get("created_at"),
            "updated_at": dash_data.get("updated_at"),
        }

    return None


def get_tiles_for_dashboard(dashboard_id: str | int) -> list[dict]:
    """Get all tiles for a dashboard (mock + dynamic).

    Args:
        dashboard_id: The dashboard ID

    Returns:
        List of tile dicts with keys: id, title, type, query
    """
    dashboard = find_dashboard_by_id(dashboard_id)
    if dashboard:
        return dashboard.get("tiles", [])
    return []


def delete_look(look_id: str | int) -> bool:
    """Delete a look from the dynamic store.

    Note: Cannot delete mock looks.

    Args:
        look_id: The look ID to delete

    Returns:
        True if deleted, False if not found
    """
    from query_store import get_look_store, get_query_lock

    with get_query_lock():
        look_store = get_look_store()
        deleted = False
        if look_id in look_store:
            del look_store[look_id]
            deleted = True
        str_id = str(look_id)
        if str_id in look_store:
            del look_store[str_id]
            deleted = True
        return deleted


def update_look(look_id: str | int, **updates) -> Look | None:
    """Update a look in the dynamic store.

    Note: Cannot update mock looks.

    Args:
        look_id: The look ID to update
        **updates: Fields to update (title, description, query_id)

    Returns:
        Updated Look object if found, None otherwise
    """
    from query_store import get_look_store, get_query_lock

    with get_query_lock():
        look_store = get_look_store()
        look = look_store.get(look_id) or look_store.get(str(look_id))
        if look:
            if "title" in updates and updates["title"]:
                look.title = updates["title"]
            if "description" in updates and updates["description"] is not None:
                look.description = updates["description"]
            if "query_id" in updates and updates["query_id"]:
                look.query_id = updates["query_id"]
            return look
    return None


def delete_dashboard(dashboard_id: str | int) -> bool:
    """Delete a dashboard and its tiles from the dynamic store.

    Note: Cannot delete mock dashboards.

    Args:
        dashboard_id: The dashboard ID to delete

    Returns:
        True if deleted, False if not found
    """
    from query_store import get_dashboard_store, get_dashboard_tile_store, get_query_lock

    with get_query_lock():
        dashboard_store = get_dashboard_store()
        dashboard_tile_store = get_dashboard_tile_store()
        deleted = False

        # Delete from dashboard store
        if dashboard_id in dashboard_store:
            del dashboard_store[dashboard_id]
            deleted = True
        str_id = str(dashboard_id)
        if str_id in dashboard_store:
            del dashboard_store[str_id]
            deleted = True

        # Delete associated tiles
        if dashboard_id in dashboard_tile_store:
            del dashboard_tile_store[dashboard_id]
        if str_id in dashboard_tile_store:
            del dashboard_tile_store[str_id]

        return deleted


def delete_tile(tile_id: str) -> bool:
    """Delete a tile from any dashboard.

    Args:
        tile_id: The tile element ID to delete

    Returns:
        True if deleted, False if not found
    """
    from query_store import get_dashboard_tile_store, get_query_lock

    with get_query_lock():
        dashboard_tile_store = get_dashboard_tile_store()
        for dashboard_id, tiles in list(dashboard_tile_store.items()):
            dashboard_tile_store[dashboard_id] = [
                tile for tile in tiles if tile.get("id") != tile_id
            ]
        return True  # Always return True since we don't track if tile existed


def search_content(
    query: str,
    types: list[str] | None = None,
    limit: int = 100,
) -> list[dict]:
    """Search for content (Looks, Dashboards) by text query.

    Performs case-insensitive search on titles and descriptions
    across both mock data and dynamically created content.

    Args:
        query: Search query text
        types: Content types to search ("look", "dashboard"). Defaults to both.
        limit: Maximum number of results

    Returns:
        List of search result dicts with keys: id, title, description, type
    """
    if types is None:
        types = ["look", "dashboard"]

    results = []
    query_lower = query.lower()

    # Search looks
    if "look" in types:
        for look in get_all_looks():
            title = look.title or ""
            description = getattr(look, "description", "") or ""
            if query_lower in title.lower() or query_lower in description.lower():
                results.append(
                    {
                        "id": str(look.id),
                        "title": look.title,
                        "description": description,
                        "type": "look",
                    }
                )

    # Search dashboards
    if "dashboard" in types:
        for dashboard in get_all_dashboards():
            title = dashboard.get("title", "")
            description = dashboard.get("description", "") or ""
            if query_lower in title.lower() or query_lower in description.lower():
                results.append(
                    {
                        "id": str(dashboard["id"]),
                        "title": title,
                        "description": description,
                        "type": "dashboard",
                    }
                )

    return results[:limit]
