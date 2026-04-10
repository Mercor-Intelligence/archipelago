"""Workbook CRUD tools matching Tableau REST API v3.x behavior.

Implements 5 workbook management tools:
- tableau_create_workbook
- tableau_list_workbooks
- tableau_get_workbook
- tableau_update_workbook
- tableau_delete_workbook

All tools follow Tableau API v3.x specifications.

Environment Variables:
- TABLEAU_TEST_MODE: "local" (default) or "http" for live Tableau Cloud
- TABLEAU_SERVER_URL: Tableau Server URL (required for HTTP mode)
- TABLEAU_SITE_ID: Site content URL (required for HTTP mode)
- TABLEAU_TOKEN_NAME: PAT name (required for HTTP mode)
- TABLEAU_TOKEN_SECRET: PAT secret (required for HTTP mode)
"""

import os

from db.repositories.workbook_repository import LocalDBWorkbookRepository
from db.session import get_session
from models import (
    TableauCreateWorkbookInput,
    TableauCreateWorkbookOutput,
    TableauDeleteWorkbookInput,
    TableauDeleteWorkbookOutput,
    TableauGetWorkbookInput,
    TableauGetWorkbookOutput,
    TableauListWorkbooksInput,
    TableauListWorkbooksOutput,
    TableauUpdateWorkbookInput,
    TableauUpdateWorkbookOutput,
)


def _get_repository():
    """Get WorkbookRepository based on environment configuration."""
    test_mode = os.environ.get("TABLEAU_TEST_MODE", "local").lower()

    if test_mode == "http":
        from db.repositories.http_workbook_repository import HTTPWorkbookRepository
        from tableau_http.tableau_client import TableauHTTPClient

        # Get credentials from environment
        server_url = os.environ.get("TABLEAU_SERVER_URL")
        site_id = os.environ.get("TABLEAU_SITE_ID")
        token_name = os.environ.get("TABLEAU_TOKEN_NAME")
        token_secret = os.environ.get("TABLEAU_TOKEN_SECRET")

        if not all([server_url, site_id, token_name, token_secret]):
            raise ValueError(
                "HTTP mode requires TABLEAU_SERVER_URL, TABLEAU_SITE_ID, "
                "TABLEAU_TOKEN_NAME, and TABLEAU_TOKEN_SECRET environment variables"
            )

        client = TableauHTTPClient(
            base_url=server_url,
            site_id=site_id,
            personal_access_token=f"{token_name}:{token_secret}",
        )
        return HTTPWorkbookRepository(client)

    return LocalDBWorkbookRepository()


async def tableau_create_workbook(
    request: TableauCreateWorkbookInput,
) -> TableauCreateWorkbookOutput:
    """Create a new workbook in a project."""
    # Validate name is not whitespace-only
    if request.name.strip() == "":
        raise ValueError("Name cannot be empty or whitespace")

    repository = _get_repository()

    # HTTP mode: sign in and call API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        await repository.client.sign_in()
        return await repository.create(session=None, request=request)

    # Local mode: use database session
    async with get_session() as session:
        return await repository.create(session=session, request=request)


async def tableau_list_workbooks(
    request: TableauListWorkbooksInput,
) -> TableauListWorkbooksOutput:
    """List workbooks with pagination and optional project/owner filtering."""
    repository = _get_repository()

    # HTTP mode: sign in and call API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        await repository.client.sign_in()
        return await repository.list_workbooks(session=None, request=request)

    # Local mode: use database session
    async with get_session() as session:
        return await repository.list_workbooks(session=session, request=request)


async def tableau_get_workbook(
    request: TableauGetWorkbookInput,
) -> TableauGetWorkbookOutput:
    """Get a specific workbook by ID."""
    repository = _get_repository()

    # HTTP mode: sign in and call API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        await repository.client.sign_in()
        workbook = await repository.get_by_id(session=None, request=request)
    else:
        # Local mode: use database session
        async with get_session() as session:
            workbook = await repository.get_by_id(session=session, request=request)

    if not workbook:
        raise ValueError(f"Workbook {request.workbook_id} not found")

    return workbook


async def tableau_update_workbook(
    request: TableauUpdateWorkbookInput,
) -> TableauUpdateWorkbookOutput:
    """Update workbook name and description."""
    # Validate name is not whitespace-only if provided
    if request.name is not None and request.name.strip() == "":
        raise ValueError("Name cannot be empty or whitespace")

    repository = _get_repository()

    # HTTP mode: sign in and call API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        await repository.client.sign_in()
        return await repository.update(session=None, request=request)

    # Local mode: use database session
    async with get_session() as session:
        return await repository.update(session=session, request=request)


async def tableau_delete_workbook(
    request: TableauDeleteWorkbookInput,
) -> TableauDeleteWorkbookOutput:
    """Delete a workbook."""
    repository = _get_repository()

    # HTTP mode: sign in and call API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        await repository.client.sign_in()
        return await repository.delete(session=None, request=request)

    # Local mode: use database session
    async with get_session() as session:
        return await repository.delete(session=session, request=request)
