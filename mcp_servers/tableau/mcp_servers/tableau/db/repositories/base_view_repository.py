"""Abstract base class for ViewRepository implementations.

This module defines the interface that all ViewRepository implementations
must follow, whether they use local database storage or HTTP API calls.

Note: Views are read-only resources. They are created automatically when
workbooks are published to Tableau Server. This repository only provides
read and data export operations.
"""

from abc import ABC, abstractmethod

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
)
from sqlalchemy.ext.asyncio import AsyncSession


class ViewRepository(ABC):
    """Abstract base class for view repositories.

    Provides read-only access to views and data export functionality.
    Views are created automatically when workbooks are published.
    """

    @abstractmethod
    async def get_by_id(
        self, session: AsyncSession, request: TableauGetViewInput
    ) -> TableauGetViewOutput | None:
        """Get view by ID.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: Get view request

        Returns:
            View details if found, None otherwise
        """
        pass

    @abstractmethod
    async def list_views(
        self,
        session: AsyncSession,
        request: TableauListViewsInput,
    ) -> TableauListViewsOutput:
        """List views with pagination and optional filters.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: List views request

        Returns:
            Paginated list of views
        """
        pass

    @abstractmethod
    async def query_view_data(
        self, session: AsyncSession, request: TableauQueryViewDataInput
    ) -> TableauQueryViewDataOutput:
        """Query view data as CSV.

        In offline mode, returns mock data from sample_data_json.
        In live mode, fetches real data from Tableau Server.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: Query view data request

        Returns:
            CSV formatted view data

        Raises:
            ValueError: If view not found
        """
        pass

    @abstractmethod
    async def query_view_image(
        self, session: AsyncSession, request: TableauQueryViewImageInput
    ) -> TableauQueryViewImageOutput:
        """Query view image as PNG.

        In offline mode, returns image from preview_image_path or generates placeholder.
        In live mode, fetches real image from Tableau Server.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: Query view image request

        Returns:
            PNG image data

        Raises:
            ValueError: If view not found
        """
        pass

    @abstractmethod
    async def get_view_metadata(
        self, session: AsyncSession, request: TableauGetViewMetadataInput
    ) -> TableauGetViewMetadataOutput:
        """Get metadata for a view including field names, types, and roles.

        In offline mode, infers metadata from sample_data_json.
        In live mode, could call Tableau's Metadata API (future implementation).

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: Get view metadata request

        Returns:
            View metadata including fields, types, roles

        Raises:
            ValueError: If view not found
        """
        pass

    @abstractmethod
    async def query_view_data_to_file(
        self, session: AsyncSession, request: TableauQueryViewDataInput
    ) -> TableauQueryViewDataToFileOutput:
        """Query view data and write to CSV file.

        Writes the CSV data to a file in STATE_LOCATION and returns the file path.
        This is useful for large datasets to avoid sending large responses through MCP.

        In offline mode, writes mock data from sample_data_json to file.
        In live mode, fetches real data from Tableau Server and writes to file.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: Query view data request

        Returns:
            File path and row count

        Raises:
            ValueError: If view not found
        """
        pass
