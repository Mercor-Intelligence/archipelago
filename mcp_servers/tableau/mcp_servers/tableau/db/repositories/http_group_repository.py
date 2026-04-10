"""HTTP-based GroupRepository implementation.

This repository makes HTTP requests to a Tableau Server REST API instead of
using a local database. Useful for integration tests against live servers.
"""

from __future__ import annotations

from datetime import datetime

from db.repositories.base_group_repository import GroupRepository
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
from sqlalchemy.ext.asyncio import AsyncSession
from tableau_http.tableau_client import TableauHTTPClient


class HTTPGroupRepository(GroupRepository):
    """HTTP-based implementation of GroupRepository using Tableau REST API."""

    def __init__(self, client: TableauHTTPClient):
        """Initialize HTTP repository with Tableau client.

        Args:
            client: Configured TableauHTTPClient instance
        """
        self.client = client

    async def create(
        self,
        session: AsyncSession,
        request: TableauCreateGroupInput,
    ) -> TableauCreateGroupOutput:
        """Create a new group via Tableau REST API.

        Args:
            session: Unused (kept for interface compatibility)
            request: Group creation request

        Returns:
            Created group details

        Raises:
            httpx.HTTPStatusError: If API request fails
        """
        # Build request payload for Tableau API
        payload = {
            "group": {
                "name": request.name,
            }
        }

        if request.description:
            payload["group"]["description"] = request.description

        # Make API request
        endpoint = self.client.get_group_endpoint()
        response_data = await self.client.post(endpoint, payload)

        # Parse response
        group_data = response_data.get("group", {})
        return TableauCreateGroupOutput(
            id=group_data.get("id"),
            name=group_data.get("name"),
            description=group_data.get("description", ""),
            created_at=group_data.get("createdAt", datetime.utcnow().isoformat()),
            updated_at=group_data.get("updatedAt", datetime.utcnow().isoformat()),
        )

    async def list_groups(
        self,
        session: AsyncSession,
        request: TableauListGroupsInput,
    ) -> TableauListGroupsOutput:
        """List groups via Tableau REST API.

        Args:
            session: Unused (kept for interface compatibility)
            request: List groups request

        Returns:
            Paginated list of groups

        Raises:
            httpx.HTTPStatusError: If API request fails
        """
        # Build query parameters
        params = {
            "pageSize": request.page_size,
            "pageNumber": request.page_number,
        }

        # Make API request
        endpoint = self.client.get_group_endpoint()
        response_data = await self.client.get(endpoint, params)

        # Parse response
        groups_data = response_data.get("groups", {}).get("group", [])
        pagination = response_data.get("pagination", {})

        group_outputs = [
            TableauCreateGroupOutput(
                id=g.get("id"),
                name=g.get("name"),
                description=g.get("description", ""),
                created_at=g.get("createdAt", datetime.utcnow().isoformat()),
                updated_at=g.get("updatedAt", datetime.utcnow().isoformat()),
            )
            for g in groups_data
        ]

        return TableauListGroupsOutput(
            groups=group_outputs,
            total_count=int(pagination.get("totalAvailable", len(group_outputs))),
            page_number=request.page_number,
            page_size=request.page_size,
        )

    async def add_user_to_group(
        self,
        session: AsyncSession,
        request: TableauAddUserToGroupInput,
    ) -> TableauAddUserToGroupOutput:
        """Add a user to a group via Tableau REST API.

        Args:
            session: Unused (kept for interface compatibility)
            request: Add user to group request

        Returns:
            Group membership details

        Raises:
            ValueError: If group or user not found
            httpx.HTTPStatusError: If API request fails
        """
        # Build request payload
        payload = {
            "user": {
                "id": request.user_id,
            }
        }

        # Make API request
        endpoint = self.client.get_group_user_endpoint(request.group_id)
        try:
            response_data = await self.client.post(endpoint, payload)

            # Parse response
            user_data = response_data.get("user", {})
            return TableauAddUserToGroupOutput(
                id=user_data.get("id", request.user_id),
                group_id=request.group_id,
                user_id=request.user_id,
                created_at=datetime.utcnow().isoformat(),
            )
        except Exception as e:
            if hasattr(e, "response") and e.response.status_code == 404:
                raise ValueError(f"Group {request.group_id} or user {request.user_id} not found")
            raise

    async def remove_user_from_group(
        self,
        session: AsyncSession,
        request: TableauRemoveUserFromGroupInput,
    ) -> TableauRemoveUserFromGroupOutput:
        """Remove a user from a group via Tableau REST API.

        Args:
            session: Unused (kept for interface compatibility)
            request: Remove user from group request

        Returns:
            Success status

        Raises:
            ValueError: If group not found
            httpx.HTTPStatusError: If API request fails
        """
        # Make API request
        endpoint = self.client.get_group_user_endpoint(request.group_id, request.user_id)
        try:
            await self.client.delete(endpoint)
            return TableauRemoveUserFromGroupOutput(success=True)
        except Exception as e:
            if hasattr(e, "response") and e.response.status_code == 404:
                raise ValueError(f"Group {request.group_id} not found")
            raise
