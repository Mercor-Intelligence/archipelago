"""Group CRUD tools matching Tableau REST API v3.x behavior.

Implements 4 group management tools:
- tableau_create_group
- tableau_list_groups
- tableau_add_user_to_group (idempotent)
- tableau_remove_user_from_group

All tools follow Tableau API v3.x specifications.

Environment Variables:
- TABLEAU_TEST_MODE: "local" (default) or "http" for live Tableau Cloud
- TABLEAU_SERVER_URL: Tableau Server URL (required for HTTP mode)
- TABLEAU_SITE_ID: Site content URL (required for HTTP mode)
- TABLEAU_TOKEN_NAME: PAT name (required for HTTP mode)
- TABLEAU_TOKEN_SECRET: PAT secret (required for HTTP mode)
"""

import os

from db.repositories.group_repository import LocalDBGroupRepository
from db.session import get_session
from models import (
    TableauAddUserToGroupInput,
    TableauAddUserToGroupOutput,
    TableauCreateGroupInput,
    TableauCreateGroupOutput,
    TableauListGroupsInput,
    TableauListGroupsOutput,
    TableauRemoveUserFromGroupInput,
    TableauRemoveUserFromGroupOutput,
)


def _get_repository():
    """Get GroupRepository based on environment configuration."""
    test_mode = os.environ.get("TABLEAU_TEST_MODE", "local").lower()

    if test_mode == "http":
        from db.repositories.http_group_repository import HTTPGroupRepository
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
        return HTTPGroupRepository(client)

    return LocalDBGroupRepository()


async def tableau_create_group(
    request: TableauCreateGroupInput,
) -> TableauCreateGroupOutput:
    """Create a new group on the site."""
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


async def tableau_list_groups(
    request: TableauListGroupsInput,
) -> TableauListGroupsOutput:
    """List groups with pagination."""
    repository = _get_repository()

    # HTTP mode: sign in and call API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        await repository.client.sign_in()
        return await repository.list_groups(session=None, request=request)

    # Local mode: use database session
    async with get_session() as session:
        return await repository.list_groups(session=session, request=request)


async def tableau_add_user_to_group(
    request: TableauAddUserToGroupInput,
) -> TableauAddUserToGroupOutput:
    """Add a user to a group. Returns existing membership if already a member."""
    repository = _get_repository()

    # HTTP mode: sign in and call API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        await repository.client.sign_in()
        return await repository.add_user_to_group(session=None, request=request)

    # Local mode: use database session
    async with get_session() as session:
        return await repository.add_user_to_group(session=session, request=request)


async def tableau_remove_user_from_group(
    request: TableauRemoveUserFromGroupInput,
) -> TableauRemoveUserFromGroupOutput:
    """Remove a user from a group."""
    repository = _get_repository()

    # HTTP mode: sign in and call API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        await repository.client.sign_in()
        return await repository.remove_user_from_group(session=None, request=request)

    # Local mode: use database session
    async with get_session() as session:
        return await repository.remove_user_from_group(session=session, request=request)
