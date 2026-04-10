"""HTTP-based WorkbookRepository implementation.

This repository makes HTTP requests to a Tableau Server REST API instead of
using a local database. Useful for integration tests against live servers.
"""

from __future__ import annotations

from datetime import datetime

from db.repositories.base_workbook_repository import WorkbookRepository
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
from sqlalchemy.ext.asyncio import AsyncSession
from tableau_http.tableau_client import TableauHTTPClient


class HTTPWorkbookRepository(WorkbookRepository):
    """HTTP-based implementation of WorkbookRepository using Tableau REST API."""

    def __init__(self, client: TableauHTTPClient):
        """Initialize HTTP repository with Tableau client.

        Args:
            client: Configured TableauHTTPClient instance
        """
        self.client = client

    async def create(
        self,
        session: AsyncSession,
        request: TableauCreateWorkbookInput,
    ) -> TableauCreateWorkbookOutput:
        """Create a new workbook via Tableau REST API.

        Args:
            session: Unused (kept for interface compatibility)
            request: Workbook creation request

        Returns:
            Created workbook details

        Raises:
            httpx.HTTPStatusError: If API request fails
        """
        # Build request payload for Tableau API
        payload = {
            "workbook": {
                "name": request.name,
                "project": {"id": request.project_id},
            }
        }

        if request.description:
            payload["workbook"]["description"] = request.description

        # Make API request
        endpoint = self.client.get_workbook_endpoint()
        response_data = await self.client.post(endpoint, payload)

        # Parse response
        wb_data = response_data.get("workbook", {})
        return TableauCreateWorkbookOutput(
            id=wb_data.get("id"),
            name=wb_data.get("name"),
            project_id=wb_data.get("project", {}).get("id"),
            owner_id=wb_data.get("owner", {}).get("id"),
            file_reference=wb_data.get("webpageUrl"),
            description=wb_data.get("description", ""),
            created_at=wb_data.get("createdAt", datetime.utcnow().isoformat()),
            updated_at=wb_data.get("updatedAt", datetime.utcnow().isoformat()),
        )

    async def get_by_id(
        self, session: AsyncSession, request: TableauGetWorkbookInput
    ) -> TableauGetWorkbookOutput | None:
        """Get workbook by ID via Tableau REST API.

        Args:
            session: Unused (kept for interface compatibility)
            request: Get workbook request

        Returns:
            Workbook details if found, None otherwise

        Raises:
            httpx.HTTPStatusError: If API request fails (404 returns None)
        """
        try:
            endpoint = self.client.get_workbook_endpoint(request.workbook_id)
            response_data = await self.client.get(endpoint)

            # Parse response
            wb_data = response_data.get("workbook", {})
            return TableauGetWorkbookOutput(
                id=wb_data.get("id"),
                name=wb_data.get("name"),
                project_id=wb_data.get("project", {}).get("id"),
                owner_id=wb_data.get("owner", {}).get("id"),
                file_reference=wb_data.get("webpageUrl"),
                description=wb_data.get("description", ""),
                created_at=wb_data.get("createdAt", datetime.utcnow().isoformat()),
                updated_at=wb_data.get("updatedAt", datetime.utcnow().isoformat()),
            )
        except Exception as e:
            # Return None for 404 (workbook not found)
            if hasattr(e, "response") and e.response.status_code == 404:
                return None
            raise

    async def list_workbooks(
        self,
        session: AsyncSession,
        request: TableauListWorkbooksInput,
    ) -> TableauListWorkbooksOutput:
        """List workbooks via Tableau REST API.

        Args:
            session: Unused (kept for interface compatibility)
            request: List workbooks request

        Returns:
            Paginated list of workbooks

        Raises:
            httpx.HTTPStatusError: If API request fails
        """
        # Build query parameters
        params = {
            "pageSize": request.page_size,
            "pageNumber": request.page_number,
        }

        # Add filters
        filters = []
        if request.project_id:
            filters.append(f"projectId:eq:{request.project_id}")
        if request.owner_id:
            filters.append(f"ownerId:eq:{request.owner_id}")
        if filters:
            params["filter"] = ",".join(filters)

        # Make API request
        endpoint = self.client.get_workbook_endpoint()
        response_data = await self.client.get(endpoint, params)

        # Parse response
        workbooks_data = response_data.get("workbooks", {}).get("workbook", [])
        pagination = response_data.get("pagination", {})

        workbook_outputs = [
            TableauCreateWorkbookOutput(
                id=wb.get("id"),
                name=wb.get("name"),
                project_id=wb.get("project", {}).get("id"),
                owner_id=wb.get("owner", {}).get("id"),
                file_reference=wb.get("webpageUrl"),
                description=wb.get("description", ""),
                created_at=wb.get("createdAt", datetime.utcnow().isoformat()),
                updated_at=wb.get("updatedAt", datetime.utcnow().isoformat()),
            )
            for wb in workbooks_data
        ]

        return TableauListWorkbooksOutput(
            workbooks=workbook_outputs,
            total_count=int(pagination.get("totalAvailable", len(workbook_outputs))),
            page_number=request.page_number,
            page_size=request.page_size,
        )

    async def update(
        self, session: AsyncSession, request: TableauUpdateWorkbookInput
    ) -> TableauUpdateWorkbookOutput:
        """Update workbook via Tableau REST API.

        Args:
            session: Unused (kept for interface compatibility)
            request: Update workbook request

        Returns:
            Updated workbook details

        Raises:
            ValueError: If workbook not found
            httpx.HTTPStatusError: If API request fails
        """
        # Build request payload
        payload = {"workbook": {}}

        if request.name is not None:
            payload["workbook"]["name"] = request.name
        if request.description is not None:
            payload["workbook"]["description"] = request.description

        # Make API request
        endpoint = self.client.get_workbook_endpoint(request.workbook_id)
        try:
            response_data = await self.client.put(endpoint, payload)

            # Parse response
            wb_data = response_data.get("workbook", {})
            return TableauUpdateWorkbookOutput(
                id=wb_data.get("id"),
                name=wb_data.get("name"),
                project_id=wb_data.get("project", {}).get("id"),
                owner_id=wb_data.get("owner", {}).get("id"),
                file_reference=wb_data.get("webpageUrl"),
                description=wb_data.get("description", ""),
                created_at=wb_data.get("createdAt", datetime.utcnow().isoformat()),
                updated_at=wb_data.get("updatedAt", datetime.utcnow().isoformat()),
            )
        except Exception as e:
            if hasattr(e, "response") and e.response.status_code == 404:
                raise ValueError(f"Workbook {request.workbook_id} not found")
            raise

    async def delete(
        self, session: AsyncSession, request: TableauDeleteWorkbookInput
    ) -> TableauDeleteWorkbookOutput:
        """Delete workbook via Tableau REST API.

        Args:
            session: Unused (kept for interface compatibility)
            request: Delete workbook request

        Returns:
            Deletion result

        Raises:
            ValueError: If workbook not found
            httpx.HTTPStatusError: If API request fails
        """
        endpoint = self.client.get_workbook_endpoint(request.workbook_id)
        try:
            await self.client.delete(endpoint)
            return TableauDeleteWorkbookOutput(
                success=True,
                message=f"Workbook {request.workbook_id} deleted successfully.",
            )
        except Exception as e:
            if hasattr(e, "response") and e.response.status_code == 404:
                raise ValueError(f"Workbook {request.workbook_id} not found")
            raise
