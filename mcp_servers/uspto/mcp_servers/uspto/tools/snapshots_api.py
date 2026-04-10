"""REST bridge compatible wrappers for snapshot tools."""

import asyncio
import os
import sys
from pathlib import Path

# Ensure we can import from the server directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from models import (  # noqa: E402
    CreateSnapshotRequest,
    GetSnapshotRequest,
    ListSnapshotsRequest,
    ListSnapshotsResponse,
    SnapshotResponse,
)
from utils.decorators import make_async_background  # noqa: E402


async def _ensure_db():
    """Initialize the database if not already initialized."""
    from mcp_servers.uspto.db.session import init_db

    db_path = os.environ.get("USPTO_DB_PATH", "temp")
    await init_db(db_path)


@make_async_background
def snapshots_create(request: CreateSnapshotRequest) -> SnapshotResponse:
    """Capture a versioned snapshot of a patent application."""
    from tools.snapshots import uspto_snapshots_create

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_ensure_db())
        return loop.run_until_complete(uspto_snapshots_create(**request.model_dump()))
    finally:
        loop.close()


@make_async_background
def snapshots_get(request: GetSnapshotRequest) -> SnapshotResponse:
    """Retrieve a specific snapshot by application number and version."""
    from tools.snapshots import uspto_snapshots_get

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_ensure_db())
        return loop.run_until_complete(uspto_snapshots_get(**request.model_dump()))
    finally:
        loop.close()


@make_async_background
def snapshots_list(request: ListSnapshotsRequest) -> ListSnapshotsResponse:
    """List all snapshots in a workspace with pagination."""
    from tools.snapshots import uspto_snapshots_list

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_ensure_db())
        return loop.run_until_complete(uspto_snapshots_list(**request.model_dump()))
    finally:
        loop.close()
