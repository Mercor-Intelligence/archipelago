"""HTTP-based UserRepository implementation.

This repository makes HTTP requests to a Tableau Server REST API instead of
using a local database. Useful for integration tests against live servers.
"""

from __future__ import annotations

from datetime import datetime

from db.repositories.base_user_repository import UserRepository
from models import (
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
from sqlalchemy.ext.asyncio import AsyncSession
from tableau_http.tableau_client import TableauHTTPClient


class HTTPUserRepository(UserRepository):
    """HTTP-based implementation of UserRepository using Tableau REST API."""

    def __init__(self, client: TableauHTTPClient):
        """Initialize HTTP repository with Tableau client.

        Args:
            client: Configured TableauHTTPClient instance
        """
        self.client = client

    async def create(
        self,
        session: AsyncSession,
        request: TableauCreateUserInput,
    ) -> TableauCreateUserOutput:
        """Create a new user via Tableau REST API.

        Args:
            session: Unused (kept for interface compatibility)
            request: User creation request

        Returns:
            Created user details

        Raises:
            ValueError: If username already exists
            httpx.HTTPStatusError: If API request fails
        """
        # Build request payload for Tableau API
        payload = {
            "user": {
                "name": request.name,
                "siteRole": request.site_role,
            }
        }

        # Add email if provided
        if request.email:
            payload["user"]["email"] = request.email

        # Make API request
        endpoint = self.client.get_user_endpoint()
        try:
            response_data = await self.client.post(endpoint, payload)

            # Parse response
            user_data = response_data.get("user", {})
            return TableauCreateUserOutput(
                id=user_data.get("id"),
                name=user_data.get("name"),
                email=user_data.get("email"),
                site_role=user_data.get("siteRole"),
                created_at=user_data.get("createdAt", datetime.utcnow().isoformat()),
                updated_at=user_data.get("updatedAt", datetime.utcnow().isoformat()),
            )
        except Exception as e:
            # Tableau returns 409 for duplicate username
            if hasattr(e, "response") and e.response.status_code == 409:
                raise ValueError(f"User with name '{request.name}' already exists on this site")
            raise

    async def get_by_id(
        self, session: AsyncSession, request: TableauGetUserInput
    ) -> TableauGetUserOutput | None:
        """Get user by ID via Tableau REST API.

        Args:
            session: Unused (kept for interface compatibility)
            request: Get user request

        Returns:
            User details if found, None otherwise

        Raises:
            httpx.HTTPStatusError: If API request fails (404 returns None)
        """
        try:
            endpoint = self.client.get_user_endpoint(request.user_id)
            response_data = await self.client.get(endpoint)

            # Parse response
            user_data = response_data.get("user", {})
            return TableauGetUserOutput(
                id=user_data.get("id"),
                name=user_data.get("name"),
                email=user_data.get("email"),
                site_role=user_data.get("siteRole"),
                created_at=user_data.get("createdAt", datetime.utcnow().isoformat()),
                updated_at=user_data.get("updatedAt", datetime.utcnow().isoformat()),
            )
        except Exception as e:
            # Return None for 404 (user not found)
            if hasattr(e, "response") and e.response.status_code == 404:
                return None
            raise

    async def list_users(
        self,
        session: AsyncSession,
        request: TableauListUsersInput,
    ) -> TableauListUsersOutput:
        """List users via Tableau REST API.

        Args:
            session: Unused (kept for interface compatibility)
            request: List users request

        Returns:
            Paginated list of users

        Raises:
            httpx.HTTPStatusError: If API request fails
        """
        # Build query parameters
        params = {
            "pageSize": request.page_size,
            "pageNumber": request.page_number,
        }

        # Make API request
        endpoint = self.client.get_user_endpoint()
        response_data = await self.client.get(endpoint, params)

        # Parse response
        users_data = response_data.get("users", {}).get("user", [])
        pagination = response_data.get("pagination", {})

        user_outputs = [
            TableauCreateUserOutput(
                id=user.get("id"),
                name=user.get("name"),
                email=user.get("email"),
                site_role=user.get("siteRole"),
                created_at=user.get("createdAt", datetime.utcnow().isoformat()),
                updated_at=user.get("updatedAt", datetime.utcnow().isoformat()),
            )
            for user in users_data
        ]

        return TableauListUsersOutput(
            users=user_outputs,
            total_count=int(pagination.get("totalAvailable", len(user_outputs))),
            page_number=request.page_number,
            page_size=request.page_size,
        )

    async def update(
        self, session: AsyncSession, request: TableauUpdateUserInput
    ) -> TableauUpdateUserOutput:
        """Update user via Tableau REST API.

        Args:
            session: Unused (kept for interface compatibility)
            request: Update user request

        Returns:
            Updated user details

        Raises:
            ValueError: If user not found
            httpx.HTTPStatusError: If API request fails
        """
        # Build request payload
        payload = {"user": {}}

        if request.name is not None:
            payload["user"]["name"] = request.name
        if request.email is not None:
            payload["user"]["email"] = request.email
        if request.site_role is not None:
            payload["user"]["siteRole"] = request.site_role

        # Make API request
        endpoint = self.client.get_user_endpoint(request.user_id)
        try:
            response_data = await self.client.put(endpoint, payload)

            # Parse response
            user_data = response_data.get("user", {})
            return TableauUpdateUserOutput(
                id=user_data.get("id"),
                name=user_data.get("name"),
                email=user_data.get("email"),
                site_role=user_data.get("siteRole"),
                created_at=user_data.get("createdAt", datetime.utcnow().isoformat()),
                updated_at=user_data.get("updatedAt", datetime.utcnow().isoformat()),
            )
        except Exception as e:
            if hasattr(e, "response") and e.response.status_code == 404:
                raise ValueError(f"User {request.user_id} not found")
            elif hasattr(e, "response") and e.response.status_code == 409:
                raise ValueError(f"User with name '{request.name}' already exists on this site")
            raise

    async def delete(
        self, session: AsyncSession, request: TableauDeleteUserInput
    ) -> TableauDeleteUserOutput:
        """Delete user via Tableau REST API.

        Tableau API v3.x Behavior:
        - If user owns content, deletion may be blocked or require mapAssetsTo parameter
        - Tableau API handles the complexity internally

        Args:
            session: Unused (kept for interface compatibility)
            request: Delete user request

        Returns:
            Deletion result

        Raises:
            ValueError: If user not found or owns content without transfer target
            httpx.HTTPStatusError: If API request fails
        """
        # Build query parameters for mapAssetsTo if provided
        params = {}
        if request.map_assets_to:
            params["mapAssetsTo"] = request.map_assets_to

        endpoint = self.client.get_user_endpoint(request.user_id)
        try:
            await self.client.delete(endpoint, params if params else None)

            # Successful deletion
            message = f"User {request.user_id} deleted successfully."
            if request.map_assets_to:
                message = (
                    f"User {request.user_id} deleted. "
                    f"Content transferred to {request.map_assets_to}."
                )

            return TableauDeleteUserOutput(
                success=True,
                message=message,
                role_changed_to=None,
                content_transferred_to=request.map_assets_to,
            )
        except Exception as e:
            if hasattr(e, "response"):
                if e.response.status_code == 404:
                    raise ValueError(f"User {request.user_id} not found")
                elif e.response.status_code == 400:
                    # Tableau returns 400 if user owns content and mapAssetsTo not provided
                    return TableauDeleteUserOutput(
                        success=False,
                        message=f"User {request.user_id} owns content. "
                        f"Use map_assets_to parameter to transfer ownership and delete.",
                        role_changed_to="Unlicensed",
                        content_transferred_to=None,
                    )
            raise
