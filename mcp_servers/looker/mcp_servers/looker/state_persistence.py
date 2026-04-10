"""State persistence for Looker offline mode.

Persists Looks, Dashboards, Queries, and Tiles to JSON files in STATE_LOCATION
so they can be captured in snapshots and verified.

Files created:
- looks.json: All created Looks
- dashboards.json: All created Dashboards
- queries.json: All created Queries
- tiles.json: Dashboard tiles mapping
"""

import json
import os
from pathlib import Path
from typing import Any

from loguru import logger


def _get_state_dir() -> Path | None:
    """Get the state directory (STATE_LOCATION) if available."""
    state_location = os.environ.get("STATE_LOCATION")
    if state_location:
        state_path = Path(state_location)
        state_path.mkdir(parents=True, exist_ok=True)
        return state_path
    return None


def _save_json(filename: str, data: Any) -> bool:
    """Save data to a JSON file in STATE_LOCATION."""
    state_dir = _get_state_dir()
    if not state_dir:
        return False

    file_path = state_dir / filename
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        logger.debug(f"Saved state to {file_path}")
        return True
    except Exception as e:
        logger.warning(f"Failed to save state to {file_path}: {e}")
        return False


def _load_json(filename: str) -> Any | None:
    """Load data from a JSON file in STATE_LOCATION."""
    state_dir = _get_state_dir()
    if not state_dir:
        return None

    file_path = state_dir / filename
    if not file_path.exists():
        return None

    try:
        with open(file_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load state from {file_path}: {e}")
        return None


# ============================================================================
# Looks persistence
# ============================================================================


def save_look(look_id: str, look_data: dict) -> bool:
    """Save a Look to persistent storage."""
    looks = _load_json("looks.json") or {}
    looks[look_id] = look_data
    return _save_json("looks.json", looks)


def load_looks() -> dict[str, dict]:
    """Load all Looks from persistent storage."""
    return _load_json("looks.json") or {}


def get_all_looks() -> list[dict]:
    """Get all Looks as a list."""
    looks = load_looks()
    return list(looks.values())


# ============================================================================
# Dashboards persistence
# ============================================================================


def save_dashboard(dashboard_id: str, dashboard_data: dict) -> bool:
    """Save a Dashboard to persistent storage."""
    dashboards = _load_json("dashboards.json") or {}
    dashboards[dashboard_id] = dashboard_data
    return _save_json("dashboards.json", dashboards)


def load_dashboards() -> dict[str, dict]:
    """Load all Dashboards from persistent storage."""
    return _load_json("dashboards.json") or {}


def get_all_dashboards() -> list[dict]:
    """Get all Dashboards as a list."""
    dashboards = load_dashboards()
    return list(dashboards.values())


# ============================================================================
# Queries persistence
# ============================================================================


def save_query(query_id: str | int, query_data: dict) -> bool:
    """Save a Query to persistent storage."""
    queries = _load_json("queries.json") or {}
    queries[str(query_id)] = query_data
    return _save_json("queries.json", queries)


def load_queries() -> dict[str, dict]:
    """Load all Queries from persistent storage."""
    return _load_json("queries.json") or {}


# ============================================================================
# Dashboard tiles persistence
# ============================================================================


def save_dashboard_tiles(dashboard_id: str | int, tiles: list[dict]) -> bool:
    """Save tiles for a Dashboard to persistent storage."""
    all_tiles = _load_json("tiles.json") or {}
    all_tiles[str(dashboard_id)] = tiles
    return _save_json("tiles.json", all_tiles)


def add_tile_to_dashboard_persistent(dashboard_id: str | int, tile_data: dict) -> bool:
    """Add a tile to a Dashboard in persistent storage."""
    all_tiles = _load_json("tiles.json") or {}
    dashboard_key = str(dashboard_id)
    if dashboard_key not in all_tiles:
        all_tiles[dashboard_key] = []
    all_tiles[dashboard_key].append(tile_data)
    return _save_json("tiles.json", all_tiles)


def load_dashboard_tiles() -> dict[str, list[dict]]:
    """Load all Dashboard tiles from persistent storage."""
    return _load_json("tiles.json") or {}


def get_tiles_for_dashboard(dashboard_id: str | int) -> list[dict]:
    """Get tiles for a specific Dashboard."""
    all_tiles = load_dashboard_tiles()
    return all_tiles.get(str(dashboard_id), [])


# ============================================================================
# Bulk state operations
# ============================================================================


def save_all_state(
    looks: dict | None = None,
    dashboards: dict | None = None,
    queries: dict | None = None,
    tiles: dict | None = None,
) -> bool:
    """Save all state at once."""
    success = True
    if looks is not None:
        success = _save_json("looks.json", looks) and success
    if dashboards is not None:
        success = _save_json("dashboards.json", dashboards) and success
    if queries is not None:
        success = _save_json("queries.json", queries) and success
    if tiles is not None:
        success = _save_json("tiles.json", tiles) and success
    return success


def load_all_state() -> dict[str, Any]:
    """Load all state at once."""
    return {
        "looks": load_looks(),
        "dashboards": load_dashboards(),
        "queries": load_queries(),
        "tiles": load_dashboard_tiles(),
    }


def get_state_summary() -> dict[str, int]:
    """Get a summary of persisted state counts."""
    state = load_all_state()
    return {
        "looks": len(state["looks"]),
        "dashboards": len(state["dashboards"]),
        "queries": len(state["queries"]),
        "dashboards_with_tiles": len(state["tiles"]),
    }


# ============================================================================
# State restoration at startup
# ============================================================================


def restore_persisted_state() -> dict[str, int]:
    """Restore persisted state from disk into in-memory stores at startup.

    Loads dashboards, looks, queries, and tiles from STATE_LOCATION JSON files
    and populates the singleton stores in query_store.py. Also bumps ID counters
    above the highest restored IDs to prevent collisions.

    Returns:
        Dict with counts of restored items per type.
    """
    from models import Look, Query
    from query_store import (
        get_dashboard_store,
        get_dashboard_tile_store,
        get_look_store,
        get_query_lock,
        get_query_store,
    )

    state = load_all_state()
    counts = {"dashboards": 0, "looks": 0, "queries": 0, "tiles": 0}

    with get_query_lock():
        dashboard_store = get_dashboard_store()
        look_store = get_look_store()
        query_store = get_query_store()
        tile_store = get_dashboard_tile_store()

        # Restore dashboards
        for dash_id, dash_data in state["dashboards"].items():
            if dash_id not in dashboard_store:
                dashboard_store[dash_id] = dash_data
                counts["dashboards"] += 1

        # Restore looks (need to create Look model objects)
        for look_id, look_data in state["looks"].items():
            if look_id not in look_store:
                try:
                    look = Look(
                        id=look_data.get("id", look_id),
                        title=look_data.get("title", "Untitled"),
                        description=look_data.get("description"),
                        folder_id=look_data.get("folder_id"),
                        query_id=look_data.get("query_id"),
                        vis_config=look_data.get("vis_config"),
                    )
                    look_store[look_id] = look
                    counts["looks"] += 1
                except Exception as e:
                    logger.warning(f"Failed to restore look {look_id}: {e}")

        # Restore queries (need to create Query model objects)
        for query_id_str, query_data in state["queries"].items():
            query_id = int(query_id_str) if query_id_str.isdigit() else query_id_str
            if query_id not in query_store:
                try:
                    query = Query(
                        id=query_data.get("id", query_id),
                        model=query_data["model"],
                        view=query_data["view"],
                        fields=query_data["fields"],
                        filters=query_data.get("filters"),
                        sorts=query_data.get("sorts", []),
                        limit=query_data.get("limit", 5000),
                        dynamic_fields=query_data.get("dynamic_fields"),
                        vis_config=query_data.get("vis_config"),
                    )
                    query_store[query_id] = query
                    counts["queries"] += 1
                except Exception as e:
                    logger.warning(f"Failed to restore query {query_id_str}: {e}")

        # Restore tiles
        for dash_id, tiles in state["tiles"].items():
            if dash_id not in tile_store:
                tile_store[dash_id] = tiles
                counts["tiles"] += len(tiles)

        # Bump ID counters above the highest restored IDs to prevent collisions
        _bump_id_counters(dashboard_store, look_store, query_store, tile_store)

    logger.info(
        f"Restored persisted state: {counts['dashboards']} dashboards, "
        f"{counts['looks']} looks, {counts['queries']} queries, "
        f"{counts['tiles']} tiles"
    )
    return counts


def _bump_id_counters(
    dashboard_store: dict,
    look_store: dict,
    query_store: dict,
    tile_store: dict,
) -> None:
    """Bump ID counters above the highest existing IDs to prevent collisions."""
    import sys

    singleton_key = "__looker_query_store_singleton__"
    singleton = sys.modules.get(singleton_key)
    if not singleton:
        return

    # Find max dashboard ID
    max_dash = 0
    for dash_id in dashboard_store:
        try:
            max_dash = max(max_dash, int(dash_id))
        except (ValueError, TypeError):
            pass

    # Find max look ID
    max_look = 0
    for look_id in look_store:
        try:
            max_look = max(max_look, int(look_id))
        except (ValueError, TypeError):
            pass

    # Find max query ID
    max_query = 0
    for query_id in query_store:
        try:
            max_query = max(max_query, int(query_id))
        except (ValueError, TypeError):
            pass

    # Find max tile ID
    max_tile = 0
    for tiles in tile_store.values():
        for tile in tiles:
            tile_id = tile.get("id", "")
            if isinstance(tile_id, str) and tile_id.startswith("elem_"):
                try:
                    max_tile = max(max_tile, int(tile_id[5:]))
                except (ValueError, TypeError):
                    pass

    # Only bump if restored IDs are higher than current counters
    if max_dash >= singleton.next_dashboard_id_counter[0]:
        singleton.next_dashboard_id_counter[0] = max_dash + 1
    if max_look >= singleton.next_look_id_counter[0]:
        singleton.next_look_id_counter[0] = max_look + 1
    if max_query >= singleton.next_query_id_counter[0]:
        singleton.next_query_id_counter[0] = max_query + 1
    if max_tile >= singleton.next_tile_id_counter[0]:
        singleton.next_tile_id_counter[0] = max_tile + 1
