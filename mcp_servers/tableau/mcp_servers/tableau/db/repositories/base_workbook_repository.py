"""Abstract base class for WorkbookRepository implementations.

This module defines the interface that all WorkbookRepository implementations
must follow, whether they use local database storage or HTTP API calls.
"""

from abc import ABC, abstractmethod

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


class WorkbookRepository(ABC):
    """Abstract base class for workbook management repositories."""

    @abstractmethod
    async def create(
        self,
        session: AsyncSession,
        request: TableauCreateWorkbookInput,
    ) -> TableauCreateWorkbookOutput:
        """Create a new workbook.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: Workbook creation request

        Returns:
            Created workbook details

        Raises:
            ValueError: If validation fails
        """
        pass

    @abstractmethod
    async def get_by_id(
        self, session: AsyncSession, request: TableauGetWorkbookInput
    ) -> TableauGetWorkbookOutput | None:
        """Get workbook by ID.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: Get workbook request

        Returns:
            Workbook details if found, None otherwise
        """
        pass

    @abstractmethod
    async def list_workbooks(
        self,
        session: AsyncSession,
        request: TableauListWorkbooksInput,
    ) -> TableauListWorkbooksOutput:
        """List workbooks with pagination and optional filters.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: List workbooks request

        Returns:
            Paginated list of workbooks
        """
        pass

    @abstractmethod
    async def update(
        self, session: AsyncSession, request: TableauUpdateWorkbookInput
    ) -> TableauUpdateWorkbookOutput:
        """Update workbook fields.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: Update workbook request

        Returns:
            Updated workbook details

        Raises:
            ValueError: If workbook not found or validation fails
        """
        pass

    @abstractmethod
    async def delete(
        self, session: AsyncSession, request: TableauDeleteWorkbookInput
    ) -> TableauDeleteWorkbookOutput:
        """Delete workbook.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: Delete workbook request

        Returns:
            Deletion result

        Raises:
            ValueError: If workbook not found
        """
        pass
