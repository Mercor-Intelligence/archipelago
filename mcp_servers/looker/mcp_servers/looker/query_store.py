"""Shared query store for dynamic query creation.

This module provides a singleton query store that is shared across
all repository instances, regardless of how modules are imported.

Uses sys.modules to ensure the same instance is used even when
imported via importlib with different module names.
"""

import sys
import threading

# Use a fixed key that won't conflict with actual module names
_SINGLETON_KEY = "__looker_query_store_singleton__"

# Check if query store has already been initialized in sys.modules
if _SINGLETON_KEY not in sys.modules:
    # First import - initialize the singleton
    _query_store_dict: dict[int, "Query"] = {}  # noqa: F821
    _look_store_dict: dict[str | int, "Look"] = {}  # noqa: F821
    _dashboard_store_dict: dict[str | int, dict] = {}  # dashboard_id -> dashboard data
    _dashboard_tile_store_dict: dict[str | int, list[dict]] = {}  # dashboard_id -> list of tiles
    _next_query_id_val = 2000  # Start above mock query IDs
    _next_dashboard_id_val = 100  # Start above mock dashboard IDs (1-6)
    _next_look_id_val = 200  # Start above mock look IDs (101-115)
    _next_tile_id_val = 1000  # Start at 1000 for tile IDs
    _next_render_task_id_val = 1  # Start at 1 for render task IDs
    _query_lock_obj = threading.Lock()

    # Store in sys.modules so all future imports get the same instance
    sys.modules[_SINGLETON_KEY] = type(
        "QueryStore",
        (),
        {
            "query_store": _query_store_dict,
            "look_store": _look_store_dict,
            "dashboard_store": _dashboard_store_dict,
            "dashboard_tile_store": _dashboard_tile_store_dict,
            "next_query_id_counter": [_next_query_id_val],  # Use list for mutability
            "next_dashboard_id_counter": [_next_dashboard_id_val],  # Use list for mutability
            "next_look_id_counter": [_next_look_id_val],  # Use list for mutability
            "next_tile_id_counter": [_next_tile_id_val],  # Use list for mutability
            "next_render_task_id_counter": [_next_render_task_id_val],  # Use list for mutability
            "lock": _query_lock_obj,
        },
    )()


def get_query_store() -> dict:
    """Get the global query store."""
    return sys.modules[_SINGLETON_KEY].query_store


def get_look_store() -> dict:
    """Get the global look store."""
    return sys.modules[_SINGLETON_KEY].look_store


def get_dashboard_store() -> dict:
    """Get the global dashboard store.

    Returns a dict mapping dashboard_id -> dashboard data dict for
    dynamically created dashboards.
    """
    return sys.modules[_SINGLETON_KEY].dashboard_store


def get_dashboard_tile_store() -> dict:
    """Get the global dashboard tile store.

    Returns a dict mapping dashboard_id -> list of dynamically added tiles.
    """
    return sys.modules[_SINGLETON_KEY].dashboard_tile_store


def get_next_query_id() -> int:
    """Get and increment the next query ID (thread-safe)."""
    singleton = sys.modules[_SINGLETON_KEY]
    with singleton.lock:
        query_id = singleton.next_query_id_counter[0]
        singleton.next_query_id_counter[0] += 1
    return query_id


def get_next_dashboard_id() -> int:
    """Get and increment the next dashboard ID (thread-safe)."""
    singleton = sys.modules[_SINGLETON_KEY]
    with singleton.lock:
        dashboard_id = singleton.next_dashboard_id_counter[0]
        singleton.next_dashboard_id_counter[0] += 1
    return dashboard_id


def get_next_look_id() -> int:
    """Get and increment the next look ID (thread-safe)."""
    singleton = sys.modules[_SINGLETON_KEY]
    with singleton.lock:
        look_id = singleton.next_look_id_counter[0]
        singleton.next_look_id_counter[0] += 1
    return look_id


def get_next_tile_id() -> int:
    """Get and increment the next tile ID (thread-safe)."""
    singleton = sys.modules[_SINGLETON_KEY]
    with singleton.lock:
        tile_id = singleton.next_tile_id_counter[0]
        singleton.next_tile_id_counter[0] += 1
    return tile_id


def get_next_render_task_id() -> int:
    """Get and increment the next render task ID (thread-safe)."""
    singleton = sys.modules[_SINGLETON_KEY]
    with singleton.lock:
        render_task_id = singleton.next_render_task_id_counter[0]
        singleton.next_render_task_id_counter[0] += 1
    return render_task_id


def get_query_lock():
    """Get the query lock for thread-safe operations."""
    return sys.modules[_SINGLETON_KEY].lock


def reset_query_store():
    """Reset the query store (for testing)."""
    singleton = sys.modules[_SINGLETON_KEY]
    with singleton.lock:
        singleton.query_store.clear()
        singleton.look_store.clear()
        singleton.dashboard_store.clear()
        singleton.dashboard_tile_store.clear()
        singleton.next_query_id_counter[0] = 2000
        singleton.next_dashboard_id_counter[0] = 100
        singleton.next_look_id_counter[0] = 200
        singleton.next_tile_id_counter[0] = 1000
        singleton.next_render_task_id_counter[0] = 1
