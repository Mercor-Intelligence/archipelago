"""User CRUD tools matching Tableau REST API v3.x behavior.

Implements 5 user management tools:
- tableau_create_user
- tableau_list_users
- tableau_get_user
- tableau_update_user
- tableau_delete_user

All tools follow Tableau API v3.x specifications validated against official docs.

Environment Variables:
- TABLEAU_TEST_MODE: "local" (default) or "http" for live Tableau Cloud
- TABLEAU_SERVER_URL: Tableau Server URL (required for HTTP mode)
- TABLEAU_SITE_ID: Site content URL (required for HTTP mode)
- TABLEAU_TOKEN_NAME: PAT name (required for HTTP mode)
- TABLEAU_TOKEN_SECRET: PAT secret (required for HTTP mode)
"""

import os

from db.repositories.user_repository import LocalDBUserRepository
from db.session import get_session
from models import (
    VALID_SITE_ROLES,
    TableauCreateUserInput,
    TableauCreateUserOutput,
    TableauDeleteUserInput,
    TableauDeleteUserOutput,
    TableauGetUserInput,
    TableauGetUserOutput,
    TableauListUsersInput,
    TableauListUsersOutput,
    TableauUpdateUserInput,
    TableauUpdateUserOutput,
)


def _get_repository():
    """Get UserRepository based on environment configuration."""
    test_mode = os.environ.get("TABLEAU_TEST_MODE", "local").lower()

    if test_mode == "http":
        from db.repositories.http_user_repository import HTTPUserRepository
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
        return HTTPUserRepository(client)

    return LocalDBUserRepository()


async def tableau_create_user(
    request: TableauCreateUserInput,
) -> TableauCreateUserOutput:
    """Create a new user on a site."""
    # Validate site_role
    if request.site_role not in VALID_SITE_ROLES:
        valid_roles_str = ", ".join(VALID_SITE_ROLES)
        raise ValueError(
            f"Invalid site role '{request.site_role}'. Must be one of: {valid_roles_str}"
        )

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


async def tableau_list_users(
    request: TableauListUsersInput,
) -> TableauListUsersOutput:
    """List users on a site with pagination."""
    repository = _get_repository()

    # HTTP mode: sign in and call API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        await repository.client.sign_in()
        return await repository.list_users(session=None, request=request)

    # Local mode: use database session
    async with get_session() as session:
        return await repository.list_users(session=session, request=request)


async def tableau_get_user(request: TableauGetUserInput) -> TableauGetUserOutput:
    """Get a specific user by ID."""
    repository = _get_repository()

    # HTTP mode: sign in and call API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        await repository.client.sign_in()
        user = await repository.get_by_id(session=None, request=request)
    else:
        # Local mode: use database session
        async with get_session() as session:
            user = await repository.get_by_id(session=session, request=request)

    if not user:
        raise ValueError(f"User {request.user_id} not found")

    return user


async def tableau_update_user(
    request: TableauUpdateUserInput,
) -> TableauUpdateUserOutput:
    """Update user details including name, email, and site role."""
    # Validate site_role if provided
    if request.site_role is not None and request.site_role not in VALID_SITE_ROLES:
        valid_roles_str = ", ".join(VALID_SITE_ROLES)
        raise ValueError(
            f"Invalid site role '{request.site_role}'. Must be one of: {valid_roles_str}"
        )

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


async def tableau_delete_user(
    request: TableauDeleteUserInput,
) -> TableauDeleteUserOutput:
    """Delete a user from the site."""
    #     Tableau API v3.x Behavior:
    # 1. If user owns content and map_assets_to NOT provided:
    #    - Deletion is BLOCKED
    #    - User's siteRole changed to "Unlicensed"
    #    - Returns success=False with role_changed_to="Unlicensed"

    # 2. If user owns content and map_assets_to IS provided:
    #    - Content ownership transferred to specified user
    #    - User is deleted
    #    - Returns success=True with content_transferred_to

    # 3. If user doesn't own content:
    #    - User is deleted
    #    - Returns success=True
    repository = _get_repository()

    # HTTP mode: sign in and call API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        await repository.client.sign_in()
        return await repository.delete(session=None, request=request)

    # Local mode: use database session
    async with get_session() as session:
        return await repository.delete(session=session, request=request)
