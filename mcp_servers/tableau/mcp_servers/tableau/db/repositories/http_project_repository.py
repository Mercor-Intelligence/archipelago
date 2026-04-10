"""HTTP-based ProjectRepository implementation.

This repository makes HTTP requests to a Tableau Server REST API instead of
using a local database. Useful for integration tests against live servers.
"""

from __future__ import annotations

from datetime import datetime

# Python 3.11+ has datetime.UTC, earlier versions need timezone.utc
try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc

from db.repositories.base_project_repository import ProjectRepository
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
from sqlalchemy.ext.asyncio import AsyncSession
from tableau_http.tableau_client import TableauHTTPClient


class HTTPProjectRepository(ProjectRepository):
    """HTTP-based implementation of ProjectRepository using Tableau REST API."""

    def __init__(self, client: TableauHTTPClient):
        """Initialize HTTP repository with Tableau client.

        Args:
            client: Configured TableauHTTPClient instance
        """
        self.client = client

    async def create(
        self,
        session: AsyncSession,
        request: TableauCreateProjectInput,
    ) -> TableauCreateProjectOutput:
        """Create a new project via Tableau REST API.

        Args:
            session: Unused (kept for interface compatibility)
            request: Project creation request

        Returns:
            Created project details

        Raises:
            ValueError: If parent project doesn't exist
            httpx.HTTPStatusError: If API request fails
        """
        payload = {
            "project": {
                "name": request.name,
                "description": request.description,
                "contentPermissions": "ManagedByOwner",
            }
        }

        if request.parent_project_id:
            payload["project"]["parentProjectId"] = request.parent_project_id

        endpoint = self.client.get_project_endpoint()
        response_data = await self.client.post(endpoint, payload)

        project_data = response_data.get("project", {})
        owner_data = project_data.get("owner") or {}
        return TableauCreateProjectOutput(
            id=project_data.get("id"),
            name=project_data.get("name"),
            description=project_data.get("description", ""),
            parent_project_id=project_data.get("parentProjectId"),
            owner_id=owner_data.get("id"),
            created_at=project_data.get("createdAt", datetime.now(UTC).isoformat()),
            updated_at=project_data.get("updatedAt", datetime.now(UTC).isoformat()),
        )

    async def get_by_id(
        self, session: AsyncSession, request: TableauGetProjectInput
    ) -> TableauGetProjectOutput | None:
        """Get project by ID via Tableau REST API.

        Note: Tableau REST API doesn't have a GET single project endpoint.
        We use the list endpoint and search for the project by ID.

        Args:
            session: Unused (kept for interface compatibility)
            request: Get project request

        Returns:
            Project details if found, None otherwise

        Raises:
            httpx.HTTPStatusError: If API request fails
        """
        # Tableau REST API doesn't have GET /projects/{id} endpoint
        # We need to list all projects and find the one with matching ID
        # Use pagination to search through all pages
        endpoint = self.client.get_project_endpoint()
        page_size = 1000
        page_number = 1

        while True:
            params = {"pageSize": page_size, "pageNumber": page_number}
            response_data = await self.client.get(endpoint, params)
            projects_data = response_data.get("projects", {}).get("project", [])

            # Find project by ID in current page
            for proj in projects_data:
                if proj.get("id") == request.project_id:
                    owner_data = proj.get("owner") or {}
                    return TableauGetProjectOutput(
                        id=proj.get("id"),
                        name=proj.get("name"),
                        description=proj.get("description", ""),
                        parent_project_id=proj.get("parentProjectId"),
                        owner_id=owner_data.get("id"),
                        created_at=proj.get("createdAt", datetime.now(UTC).isoformat()),
                        updated_at=proj.get("updatedAt", datetime.now(UTC).isoformat()),
                    )

            # Check if there are more pages
            pagination = response_data.get("pagination", {})
            total_available = int(pagination.get("totalAvailable", 0))
            fetched_so_far = page_number * page_size

            if fetched_so_far >= total_available or not projects_data:
                # No more pages to search
                break

            page_number += 1

        return None

    async def list_projects(
        self,
        session: AsyncSession,
        request: TableauListProjectsInput,
    ) -> TableauListProjectsOutput:
        """List projects via Tableau REST API.

        Args:
            session: Unused (kept for interface compatibility)
            request: List projects request

        Returns:
            Paginated list of projects

        Raises:
            httpx.HTTPStatusError: If API request fails
        """
        params = {
            "pageSize": request.page_size,
            "pageNumber": request.page_number,
        }

        if request.parent_project_id is not None:
            params["filter"] = f"parentProjectId:eq:{request.parent_project_id}"
        # Note: Tableau REST API doesn't support topLevelProjects filter
        # Just return all projects when no parent filter specified

        endpoint = self.client.get_project_endpoint()
        response_data = await self.client.get(endpoint, params)

        projects_data = response_data.get("projects", {}).get("project", [])
        pagination = response_data.get("pagination", {})

        project_outputs = []
        for proj in projects_data:
            owner_data = proj.get("owner") or {}
            project_outputs.append(
                TableauCreateProjectOutput(
                    id=proj.get("id"),
                    name=proj.get("name"),
                    description=proj.get("description", ""),
                    parent_project_id=proj.get("parentProjectId"),
                    owner_id=owner_data.get("id"),
                    created_at=proj.get("createdAt", datetime.now(UTC).isoformat()),
                    updated_at=proj.get("updatedAt", datetime.now(UTC).isoformat()),
                )
            )

        return TableauListProjectsOutput(
            projects=project_outputs,
            total_count=int(pagination.get("totalAvailable", len(project_outputs))),
            page_number=request.page_number,
            page_size=request.page_size,
        )

    async def update(
        self, session: AsyncSession, request: TableauUpdateProjectInput
    ) -> TableauUpdateProjectOutput:
        """Update project via Tableau REST API.

        Args:
            session: Unused (kept for interface compatibility)
            request: Update project request

        Returns:
            Updated project details

        Raises:
            ValueError: If project not found
            httpx.HTTPStatusError: If API request fails
        """
        payload = {"project": {}}

        if request.name is not None:
            payload["project"]["name"] = request.name
        if request.description is not None:
            payload["project"]["description"] = request.description

        endpoint = self.client.get_project_endpoint(request.project_id)
        try:
            response_data = await self.client.put(endpoint, payload)

            project_data = response_data.get("project", {})
            owner_data = project_data.get("owner") or {}
            return TableauUpdateProjectOutput(
                id=project_data.get("id"),
                name=project_data.get("name"),
                description=project_data.get("description", ""),
                parent_project_id=project_data.get("parentProjectId"),
                owner_id=owner_data.get("id"),
                created_at=project_data.get("createdAt", datetime.now(UTC).isoformat()),
                updated_at=project_data.get("updatedAt", datetime.now(UTC).isoformat()),
            )
        except Exception as e:
            if hasattr(e, "response") and e.response.status_code == 404:
                raise ValueError(f"Project {request.project_id} not found")
            raise

    async def delete(
        self, session: AsyncSession, request: TableauDeleteProjectInput
    ) -> TableauDeleteProjectOutput:
        """Delete project via Tableau REST API.

        Args:
            session: Unused (kept for interface compatibility)
            request: Delete project request

        Returns:
            Deletion result

        Raises:
            ValueError: If project not found or has child projects
            httpx.HTTPStatusError: If API request fails
        """
        endpoint = self.client.get_project_endpoint(request.project_id)
        try:
            await self.client.delete(endpoint)
            return TableauDeleteProjectOutput(
                success=True, message=f"Project {request.project_id} deleted successfully."
            )
        except Exception as e:
            if hasattr(e, "response"):
                if e.response.status_code == 404:
                    raise ValueError(f"Project {request.project_id} not found")
                elif e.response.status_code == 400:
                    raise ValueError(
                        f"Cannot delete project {request.project_id}: project has child projects. "
                        "Delete or move child projects first."
                    )
            raise
