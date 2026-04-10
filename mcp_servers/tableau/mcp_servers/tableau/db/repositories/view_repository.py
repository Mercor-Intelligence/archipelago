"""LocalDBViewRepository for read-only view operations using local database.

This repository handles database operations for views, following
Tableau REST API v3.x behavior patterns.

Note: Views are read-only resources. They are created automatically when
workbooks are published to Tableau Server. This repository only provides
read and data export operations.

For data export operations (query_view_data, query_view_image), this
implementation provides offline/mock functionality using sample_data_json
and preview_image_path fields.
"""

import base64
import csv
import io
import json
from pathlib import Path

from db.models import View
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
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from utils.type_inference import extract_field_metadata

# Placeholder PNG image (1x1 transparent pixel) for when no preview is available
PLACEHOLDER_PNG = bytes(
    [
        0x89,
        0x50,
        0x4E,
        0x47,
        0x0D,
        0x0A,
        0x1A,
        0x0A,  # PNG signature
        0x00,
        0x00,
        0x00,
        0x0D,
        0x49,
        0x48,
        0x44,
        0x52,  # IHDR chunk
        0x00,
        0x00,
        0x00,
        0x01,
        0x00,
        0x00,
        0x00,
        0x01,  # 1x1 dimensions
        0x08,
        0x06,
        0x00,
        0x00,
        0x00,
        0x1F,
        0x15,
        0xC4,  # 8-bit RGBA
        0x89,
        0x00,
        0x00,
        0x00,
        0x0A,
        0x49,
        0x44,
        0x41,  # IDAT chunk
        0x54,
        0x78,
        0x9C,
        0x63,
        0x00,
        0x01,
        0x00,
        0x00,  # compressed data
        0x05,
        0x00,
        0x01,
        0x0D,
        0x0A,
        0x2D,
        0xB4,
        0x00,  # ...
        0x00,
        0x00,
        0x00,
        0x49,
        0x45,
        0x4E,
        0x44,
        0xAE,  # IEND chunk
        0x42,
        0x60,
        0x82,  # CRC
    ]
)


class LocalDBViewRepository(ViewRepository):
    """Local database implementation of ViewRepository.

    Provides read-only access to views and data export functionality.
    """

    def _row_matches_filters(self, row: dict, filters: dict) -> bool:
        """Check if a row matches all filter criteria."""
        return all(str(row.get(field, "")) == str(value) for field, value in filters.items())

    async def get_by_id(
        self, session: AsyncSession, request: TableauGetViewInput
    ) -> TableauGetViewOutput | None:
        """Get view by ID, scoped to a specific site.

        Args:
            session: Database session
            request: Get view request

        Returns:
            View details if found, None otherwise
        """
        stmt = select(View).where(and_(View.id == request.view_id, View.site_id == request.site_id))
        result = await session.execute(stmt)
        view = result.scalar_one_or_none()

        if not view:
            return None

        return TableauGetViewOutput(
            id=view.id,
            workbook_id=view.workbook_id,
            name=view.name,
            content_url=view.content_url,
            sheet_type=view.sheet_type,
            created_at=view.created_at.isoformat(),
            updated_at=view.updated_at.isoformat(),
        )

    async def list_views(
        self,
        session: AsyncSession,
        request: TableauListViewsInput,
    ) -> TableauListViewsOutput:
        """List views with pagination and optional filters, scoped to a site.

        Args:
            session: Database session
            request: List views request

        Returns:
            Paginated list of views
        """
        # Build filter conditions
        conditions = [View.site_id == request.site_id]
        if request.workbook_id:
            conditions.append(View.workbook_id == request.workbook_id)

        # Get total count
        count_stmt = select(func.count(View.id))
        if conditions:
            count_stmt = count_stmt.where(and_(*conditions))
        total_result = await session.execute(count_stmt)
        total_count = total_result.scalar_one()

        # Get paginated results
        offset = (request.page_number - 1) * request.page_size
        stmt = select(View).order_by(View.created_at.desc()).offset(offset).limit(request.page_size)
        if conditions:
            stmt = stmt.where(and_(*conditions))

        result = await session.execute(stmt)
        views = list(result.scalars().all())

        view_outputs = [
            TableauViewOutput(
                id=v.id,
                workbook_id=v.workbook_id,
                name=v.name,
                content_url=v.content_url,
                sheet_type=v.sheet_type,
                created_at=v.created_at.isoformat(),
                updated_at=v.updated_at.isoformat(),
            )
            for v in views
        ]

        return TableauListViewsOutput(
            views=view_outputs,
            total_count=total_count,
            page_number=request.page_number,
            page_size=request.page_size,
        )

    async def query_view_data(
        self, session: AsyncSession, request: TableauQueryViewDataInput
    ) -> TableauQueryViewDataOutput:
        """Query view data as CSV.

        In offline mode, returns mock data from sample_data_json field.
        If sample_data_json is empty, returns a minimal placeholder CSV.

        Args:
            session: Database session
            request: Query view data request

        Returns:
            CSV formatted view data

        Raises:
            ValueError: If view not found
        """
        # Get the view
        stmt = select(View).where(and_(View.id == request.view_id, View.site_id == request.site_id))
        result = await session.execute(stmt)
        view = result.scalar_one_or_none()

        if not view:
            raise ValueError(f"View {request.view_id} not found")

        # Parse sample data JSON and convert to CSV
        if view.sample_data_json:
            try:
                data = json.loads(view.sample_data_json)
                if isinstance(data, list) and len(data) > 0:
                    # Apply filters if provided
                    if request.filters:
                        data = [
                            row for row in data if self._row_matches_filters(row, request.filters)
                        ]

                    # Convert to CSV
                    output = io.StringIO()
                    if len(data) > 0:
                        fieldnames = list(data[0].keys())
                        writer = csv.DictWriter(output, fieldnames=fieldnames)
                        writer.writeheader()
                        writer.writerows(data)
                    csv_data = output.getvalue()
                    row_count = len(data)
                else:
                    csv_data = ""
                    row_count = 0
            except json.JSONDecodeError:
                # Invalid JSON, return empty
                csv_data = ""
                row_count = 0
        else:
            # No sample data, return placeholder
            csv_data = "column1,column2\nno_data,available\n"
            row_count = 1

        return TableauQueryViewDataOutput(
            view_id=view.id,
            csv_data=csv_data,
            row_count=row_count,
        )

    async def query_view_image(
        self, session: AsyncSession, request: TableauQueryViewImageInput
    ) -> TableauQueryViewImageOutput:
        """Query view image as PNG.

        In offline mode, returns image from preview_image_path if available,
        otherwise returns a placeholder image.

        Args:
            session: Database session
            request: Query view image request

        Returns:
            PNG image data

        Raises:
            ValueError: If view not found
        """
        # Get the view
        stmt = select(View).where(and_(View.id == request.view_id, View.site_id == request.site_id))
        result = await session.execute(stmt)
        view = result.scalar_one_or_none()

        if not view:
            raise ValueError(f"View {request.view_id} not found")

        # Try to load image from preview_image_path
        image_data = PLACEHOLDER_PNG
        if view.preview_image_path:
            try:
                image_path = Path(view.preview_image_path)
                if image_path.exists():
                    image_data = image_path.read_bytes()
            except OSError:
                # Fall back to placeholder
                pass

        return TableauQueryViewImageOutput(
            view_id=view.id,
            image_data_base64=base64.b64encode(image_data).decode("ascii"),
            content_type="image/png",
        )

    async def get_view_metadata(
        self, session: AsyncSession, request: TableauGetViewMetadataInput
    ) -> TableauGetViewMetadataOutput:
        """Get metadata for a view including field names, types, and roles.

        Parses sample_data_json to infer field metadata, mimicking
        Tableau's Metadata API behavior for offline mode.

        Args:
            session: Database session
            request: Get view metadata request

        Returns:
            View metadata with field information

        Raises:
            ValueError: If view not found
        """
        # Get the view
        stmt = select(View).where(and_(View.id == request.view_id, View.site_id == request.site_id))
        result = await session.execute(stmt)
        view = result.scalar_one_or_none()

        if not view:
            raise ValueError(f"View {request.view_id} not found")

        # Parse sample data and extract metadata
        data: list[dict] = []
        if view.sample_data_json:
            try:
                parsed = json.loads(view.sample_data_json)
                if isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed):
                    data = parsed
            except json.JSONDecodeError:
                pass

        # Extract field metadata using type inference
        fields = extract_field_metadata(
            data=data,
            include_sample_values=request.include_sample_values,
            sample_limit=request.sample_value_limit,
        )

        return TableauGetViewMetadataOutput(
            view_id=view.id,
            view_name=view.name,
            workbook_id=view.workbook_id,
            sheet_type=view.sheet_type,
            row_count=len(data),
            fields=fields,
        )

    async def query_view_data_to_file(
        self, session: AsyncSession, request: TableauQueryViewDataInput
    ) -> TableauQueryViewDataToFileOutput:
        """Query view data and write to CSV file.

        Writes the CSV data to a file in STATE_LOCATION and returns the file path.
        This is useful for large datasets to avoid sending large responses through MCP.

        Args:
            session: Database session
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
