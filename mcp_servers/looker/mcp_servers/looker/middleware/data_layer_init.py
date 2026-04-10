"""Middleware to ensure the data layer is initialized before tool calls that need it.

The data layer (DuckDB + LookML) may take several seconds to initialize.
This middleware defers initialization and only blocks tools that actually
need the data layer (explore, query, LookML tools). Listing tools that
read from in-memory stores (dashboards, looks, folders) pass through
immediately so the home page loads fast.
"""

import asyncio

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from loguru import logger

# Tools that work without the data layer (they read from in-memory stores
# populated by restore_persisted_state, not from DuckDB/LookML).
_SKIP_INIT_TOOLS = frozenset(
    {
        "list_dashboards",
        "list_looks",
        "list_folders",
        "get_dashboard",
        "get_look",
        "health_check",
        "create_dashboard",
        "create_look",
        "add_tile_to_dashboard",
        "run_look",
        "run_dashboard",
    }
)


class DataLayerInitMiddleware(Middleware):
    """Initializes the data layer on first tool call that needs it."""

    def __init__(self):
        self._init_event: asyncio.Event | None = None

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        tool_name = getattr(context.message, "name", "")

        if tool_name in _SKIP_INIT_TOOLS:
            return await call_next(context)

        # Tool needs data layer — ensure it's initialized
        if self._init_event is None:
            # First request that needs init — run it in a thread
            self._init_event = asyncio.Event()
            try:
                from data_layer import initialize_data_layer

                logger.info(f"Tool '{tool_name}' needs data layer — initializing now")
                await asyncio.to_thread(initialize_data_layer)
                logger.info("Data layer initialized")
            finally:
                self._init_event.set()
        elif not self._init_event.is_set():
            # Another request arrived while init is still running — wait
            await self._init_event.wait()

        return await call_next(context)
