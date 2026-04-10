"""REST bridge compatible wrappers for workspace tools."""

import asyncio
import os
import sys
from pathlib import Path

# Ensure we can import from the server directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from models import (  # noqa: E402
    CreateWorkspaceRequest,
    GetWorkspaceRequest,
    ListWorkspacesRequest,
    ListWorkspacesResponse,
    WorkspaceResponse,
)
from utils.decorators import make_async_background  # noqa: E402


async def _ensure_db():
    """Initialize the database if not already initialized."""
    from mcp_servers.uspto.db.session import init_db

    db_path = os.environ.get("USPTO_DB_PATH", "temp")
    await init_db(db_path)


@make_async_background
def workspaces_create(request: CreateWorkspaceRequest) -> WorkspaceResponse:
    """Create a new research workspace for the current session."""
    from tools.workspace import uspto_workspaces_create

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_ensure_db())
        return loop.run_until_complete(uspto_workspaces_create(**request.model_dump()))
    finally:
        loop.close()


@make_async_background
def workspaces_get(request: GetWorkspaceRequest) -> WorkspaceResponse:
    """Retrieve workspace details and recent activity."""
    from tools.workspace import uspto_workspaces_get

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_ensure_db())
        return loop.run_until_complete(uspto_workspaces_get(**request.model_dump()))
    finally:
        loop.close()


@make_async_background
def workspaces_list(request: ListWorkspacesRequest) -> ListWorkspacesResponse:
    """List all workspaces in the current session."""
    from tools.workspace import uspto_workspaces_list

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_ensure_db())
        return loop.run_until_complete(uspto_workspaces_list(**request.model_dump()))
    finally:
        loop.close()
