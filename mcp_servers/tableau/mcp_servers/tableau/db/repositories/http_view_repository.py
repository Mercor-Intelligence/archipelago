"""HTTP-based ViewRepository implementation.

This repository makes HTTP requests to a Tableau Server REST API instead of
using a local database. Used for live integration with Tableau Server/Cloud.
"""

from __future__ import annotations

import base64
from datetime import datetime

from db.repositories.base_view_repository import ViewRepository
from models import (
    TableauGetViewInput,
    TableauGetViewMetadataInput,
    TableauGetViewMetadataOutput,
    TableauGetViewOutput,
    TableauListViewsInput,
    TableauListViewsOutput,
    TableauQueryViewDataInput,
    TableauQueryViewDataOutput,
    TableauQueryViewDataToFileOutput,
    TableauQueryViewImageInput,
    TableauQueryViewImageOutput,
    TableauViewOutput,
)
from sqlalchemy.ext.asyncio import AsyncSession
from tableau_http.tableau_client import TableauHTTPClient


class HTTPViewRepository(ViewRepository):
    """HTTP-based implementation of ViewRepository using Tableau REST API."""

    def __init__(self, client: TableauHTTPClient):
        """Initialize HTTP repository with Tableau client.

        Args:
            client: Configured TableauHTTPClient instance (must be signed in)
        """
        self.client = client

    def _parse_view(self, view_data: dict) -> TableauViewOutput:
        """Parse view data from Tableau API response.

        Args:
            view_data: View data dictionary from API

        Returns:
            TableauViewOutput instance
        """
        return TableauViewOutput(
            id=view_data.get("id", ""),
            workbook_id=view_data.get("workbook", {}).get("id", ""),
            name=view_data.get("name", ""),
            content_url=view_data.get("contentUrl"),
            sheet_type=view_data.get("sheetType", "worksheet"),
            created_at=view_data.get("createdAt", datetime.utcnow().isoformat()),
            updated_at=view_data.get("updatedAt", datetime.utcnow().isoformat()),
        )

    async def get_by_id(
        self, session: AsyncSession, request: TableauGetViewInput
    ) -> TableauGetViewOutput | None:
        """Get view by ID via Tableau REST API.

        Args:
            session: Unused (kept for interface compatibility)
            request: Get view request

        Returns:
            View details if found, None otherwise
        """
        try:
            endpoint = self.client.get_view_endpoint(request.view_id)
            response_data = await self.client.get(endpoint)

            view_data = response_data.get("view", {})
            return TableauGetViewOutput(
                id=view_data.get("id", ""),
                workbook_id=view_data.get("workbook", {}).get("id", ""),
                name=view_data.get("name", ""),
                content_url=view_data.get("contentUrl"),
                sheet_type=view_data.get("sheetType", "worksheet"),
                created_at=view_data.get("createdAt", datetime.utcnow().isoformat()),
                updated_at=view_data.get("updatedAt", datetime.utcnow().isoformat()),
            )
        except Exception as e:
            if hasattr(e, "response") and e.response.status_code == 404:
                return None
            raise

    async def list_views(
        self,
        session: AsyncSession,
        request: TableauListViewsInput,
    ) -> TableauListViewsOutput:
        """List views via Tableau REST API.

        Args:
            session: Unused (kept for interface compatibility)
            request: List views request

        Returns:
            Paginated list of views
        """
        # Build query parameters
        params = {
            "pageSize": request.page_size,
            "pageNumber": request.page_number,
        }

        # Use workbook-specific endpoint if filtering by workbook
        # This is more reliable than using filter parameter
        if request.workbook_id:
            endpoint = self.client.get_workbook_views_endpoint(request.workbook_id)
        else:
            endpoint = self.client.get_view_endpoint()

        # Make API request
        response_data = await self.client.get(endpoint, params)

        # Parse response
        views_data = response_data.get("views", {}).get("view", [])
        pagination = response_data.get("pagination", {})

        view_outputs = [self._parse_view(v) for v in views_data]

        return TableauListViewsOutput(
            views=view_outputs,
            total_count=int(pagination.get("totalAvailable", len(view_outputs))),
            page_number=request.page_number,
            page_size=request.page_size,
        )

    async def query_view_data(
        self, session: AsyncSession, request: TableauQueryViewDataInput
    ) -> TableauQueryViewDataOutput:
        """Query view data as CSV via Tableau REST API.

        Filters are applied via the vf_<fieldname>=value URL parameters.

        IMPORTANT: Filters only work on raw dimensions/measures from the
        datasource. Calculated fields (like YEAR([Date])), parameters, and
        derived date parts CANNOT be filtered via the API. The field name
        must exactly match the datasource field name.

        Args:
            session: Unused (kept for interface compatibility)
            request: Query view data request

        Returns:
            CSV formatted view data

        Raises:
            ValueError: If view not found
        """
        # Build query parameters for filters
        params = {}
        if request.filters:
            for field, value in request.filters.items():
                params[f"vf_{field}"] = value
        if request.max_age:
            params["maxAge"] = request.max_age

        try:
            endpoint = self.client.get_view_data_endpoint(request.view_id)
            csv_data = await self.client.get_text(endpoint, params)

            # Count rows (excluding header)
            lines = csv_data.strip().split("\n")
            row_count = max(0, len(lines) - 1)  # Subtract 1 for header

            return TableauQueryViewDataOutput(
                view_id=request.view_id,
                csv_data=csv_data,
                row_count=row_count,
            )
        except Exception as e:
            if hasattr(e, "response") and e.response.status_code == 404:
                raise ValueError(f"View {request.view_id} not found")
            raise

    async def query_view_image(
        self, session: AsyncSession, request: TableauQueryViewImageInput
    ) -> TableauQueryViewImageOutput:
        """Query view image as PNG via Tableau REST API.

        Args:
            session: Unused (kept for interface compatibility)
            request: Query view image request

        Returns:
            PNG image data (base64 encoded)

        Raises:
            ValueError: If view not found
        """
        # Build query parameters
        params = {}
        if request.resolution == "high":
            params["resolution"] = "high"
        if request.filters:
            for field, value in request.filters.items():
                params[f"vf_{field}"] = value
        if request.max_age:
            params["maxAge"] = request.max_age

        try:
            endpoint = self.client.get_view_image_endpoint(request.view_id)
            image_data = await self.client.get_raw(endpoint, params)

            return TableauQueryViewImageOutput(
                view_id=request.view_id,
                image_data_base64=base64.b64encode(image_data).decode("ascii"),
                content_type="image/png",
            )
        except Exception as e:
            if hasattr(e, "response") and e.response.status_code == 404:
                raise ValueError(f"View {request.view_id} not found")
            raise

    async def get_view_metadata(
        self, session: AsyncSession, request: TableauGetViewMetadataInput
    ) -> TableauGetViewMetadataOutput:
        """Get metadata for a view via Tableau REST API.

        In HTTP mode, this returns view metadata without detailed field information
        since the Tableau REST API doesn't provide direct metadata API access.

        Args:
            session: Unused (kept for interface compatibility)
            request: Get view metadata request

        Returns:
            View metadata with basic information

        Raises:
            ValueError: If view not found
        """
        try:
            endpoint = self.client.get_view_endpoint(request.view_id)
            response_data = await self.client.get(endpoint)

            view_data = response_data.get("view", {})
            return TableauGetViewMetadataOutput(
                view_id=view_data.get("id", ""),
                view_name=view_data.get("name", ""),
                workbook_id=view_data.get("workbook", {}).get("id", ""),
                sheet_type=view_data.get("sheetType", "worksheet"),
                row_count=0,  # Not available from REST API
                fields=[],  # Field metadata not available from REST API
            )
        except Exception as e:
            if hasattr(e, "response") and e.response.status_code == 404:
                raise ValueError(f"View {request.view_id} not found")
            raise

    async def query_view_data_to_file(
        self, session: AsyncSession, request: TableauQueryViewDataInput
    ) -> TableauQueryViewDataToFileOutput:
        """Query view data and write to CSV file via Tableau REST API.

        Writes the CSV data to a file in STATE_LOCATION and returns the file path.
        This is useful for large datasets to avoid sending large responses through MCP.

        Args:
            session: Unused (kept for interface compatibility)
            request: Query view data request

        Returns:
            File path and row count

        Raises:
            ValueError: If view not found
        """
        import os

        from loguru import logger

        # Get the CSV data using existing method
        data_output = await self.query_view_data(session=session, request=request)

        # Determine file output location
        state_location = os.getenv("APP_FS_ROOT", "./tableau_data")

        # Create directory if it doesn't exist
        os.makedirs(state_location, exist_ok=True)

        # Write CSV to file
        file_path = os.path.join(state_location, f"view_{request.view_id}_data.csv")
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(data_output.csv_data)
            logger.info(
                f"CSV data saved to {file_path} ({data_output.row_count} rows, {len(data_output.csv_data)} bytes)"
            )
        except (OSError, PermissionError) as e:
            logger.error(f"Could not save CSV to {file_path}: {e}")
            raise ValueError(f"Failed to write CSV file: {e}")

        return TableauQueryViewDataToFileOutput(
            view_id=request.view_id,
            file_path=file_path,
            row_count=data_output.row_count,
        )
