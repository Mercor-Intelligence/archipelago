"""Project CRUD tools matching Tableau REST API v3.x behavior.

Implements 5 project management tools:
- tableau_create_project
- tableau_list_projects
- tableau_get_project
- tableau_update_project
- tableau_delete_project

All tools follow Tableau API v3.x specifications with support for hierarchical projects.

Environment Variables:
- TABLEAU_TEST_MODE: "local" (default) or "http" for live Tableau Cloud
- TABLEAU_SERVER_URL: Tableau Server URL (required for HTTP mode)
- TABLEAU_SITE_ID: Site content URL (required for HTTP mode)
- TABLEAU_TOKEN_NAME: PAT name (required for HTTP mode)
- TABLEAU_TOKEN_SECRET: PAT secret (required for HTTP mode)
"""

import os

from db.repositories.project_repository import LocalDBProjectRepository
from db.session import get_session
from models import (
    TableauCreateProjectInput,
    TableauCreateProjectOutput,
    TableauDeleteProjectInput,
    TableauDeleteProjectOutput,
    TableauGetProjectInput,
    TableauGetProjectOutput,
    TableauListProjectsInput,
    TableauListProjectsOutput,
    TableauUpdateProjectInput,
    TableauUpdateProjectOutput,
)


def _get_repository():
    """Get ProjectRepository based on TABLEAU_TEST_MODE environment variable.

    Returns:
        LocalDBProjectRepository for local mode, HTTPProjectRepository for http mode
    """
    test_mode = os.environ.get("TABLEAU_TEST_MODE", "local").lower()

    if test_mode == "http":
        from db.repositories.http_project_repository import HTTPProjectRepository
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
        return HTTPProjectRepository(client)

    return LocalDBProjectRepository()


async def tableau_create_project(
    request: TableauCreateProjectInput,
) -> TableauCreateProjectOutput:
    """Create a new project on a site."""
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


async def tableau_list_projects(
    request: TableauListProjectsInput,
) -> TableauListProjectsOutput:
    """List projects on a site with pagination."""
    repository = _get_repository()

    # HTTP mode: sign in and call API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        await repository.client.sign_in()
        return await repository.list_projects(session=None, request=request)

    # Local mode: use database session
    async with get_session() as session:
        return await repository.list_projects(session=session, request=request)


async def tableau_get_project(
    request: TableauGetProjectInput,
) -> TableauGetProjectOutput:
    """Get a specific project by ID."""
    repository = _get_repository()

    # HTTP mode: sign in and call API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        await repository.client.sign_in()
        project = await repository.get_by_id(session=None, request=request)
    else:
        # Local mode: use database session
        async with get_session() as session:
            project = await repository.get_by_id(session=session, request=request)

    if not project:
        raise ValueError(f"Project {request.project_id} not found")

    return project


async def tableau_update_project(
    request: TableauUpdateProjectInput,
) -> TableauUpdateProjectOutput:
    """Update project details."""
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


async def tableau_delete_project(
    request: TableauDeleteProjectInput,
) -> TableauDeleteProjectOutput:
    """Delete a project."""
    repository = _get_repository()

    # HTTP mode: sign in and call API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        await repository.client.sign_in()
        return await repository.delete(session=None, request=request)

    # Local mode: use database session
    async with get_session() as session:
        return await repository.delete(session=session, request=request)
