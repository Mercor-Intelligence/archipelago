"""REST bridge compatible wrappers for saved query tools."""

import asyncio
import os
import sys
from pathlib import Path

# Ensure we can import from the server directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from models import (  # noqa: E402
    GetQueryRequest,
    RunQueryRequest,
    SavedQueryResponse,
    SaveQueryRequest,
    SearchResultsResponse,
)
from utils.decorators import make_async_background  # noqa: E402


async def _ensure_db():
    """Initialize the database if not already initialized."""
    from mcp_servers.uspto.db.session import init_db

    db_path = os.environ.get("USPTO_DB_PATH", "temp")
    await init_db(db_path)


@make_async_background
def queries_save(request: SaveQueryRequest) -> SavedQueryResponse:
    """Save search query to workspace for repeatable execution."""
    from tools.queries import uspto_queries_save

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_ensure_db())
        return loop.run_until_complete(uspto_queries_save(**request.model_dump()))
    finally:
        loop.close()


@make_async_background
def queries_get(request: GetQueryRequest) -> SavedQueryResponse:
    """Retrieve saved query definition and execution metadata."""
    from tools.queries import uspto_queries_get

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_ensure_db())
        return loop.run_until_complete(uspto_queries_get(**request.model_dump()))
    finally:
        loop.close()


@make_async_background
def queries_run(request: RunQueryRequest) -> SearchResultsResponse:
    """Execute saved query and return results with provenance."""
    from tools.queries import uspto_queries_run

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_ensure_db())
        return loop.run_until_complete(uspto_queries_run(**request.model_dump()))
    finally:
        loop.close()
