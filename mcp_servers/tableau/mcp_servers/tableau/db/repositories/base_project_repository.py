"""Abstract base class for ProjectRepository implementations.

This module defines the interface that all ProjectRepository implementations
must follow, whether they use local database storage or HTTP API calls.
"""

from abc import ABC, abstractmethod

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


class ProjectRepository(ABC):
    """Abstract base class for project management repositories."""

    @abstractmethod
    async def create(
        self,
        session: AsyncSession,
        request: TableauCreateProjectInput,
    ) -> TableauCreateProjectOutput:
        """Create a new project.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: Project creation request

        Returns:
            Created project details

        Raises:
            ValueError: If parent project doesn't exist or validation fails
        """
        pass

    @abstractmethod
    async def get_by_id(
        self, session: AsyncSession, request: TableauGetProjectInput
    ) -> TableauGetProjectOutput | None:
        """Get project by ID.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: Get project request

        Returns:
            Project details if found, None otherwise
        """
        pass

    @abstractmethod
    async def list_projects(
        self,
        session: AsyncSession,
        request: TableauListProjectsInput,
    ) -> TableauListProjectsOutput:
        """List projects with pagination and optional parent filter.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: List projects request

        Returns:
            Paginated list of projects
        """
        pass

    @abstractmethod
    async def update(
        self, session: AsyncSession, request: TableauUpdateProjectInput
    ) -> TableauUpdateProjectOutput:
        """Update project fields.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: Update project request

        Returns:
            Updated project details

        Raises:
            ValueError: If project not found
        """
        pass

    @abstractmethod
    async def delete(
        self, session: AsyncSession, request: TableauDeleteProjectInput
    ) -> TableauDeleteProjectOutput:
        """Delete project.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: Delete project request

        Returns:
            Deletion result

        Raises:
            ValueError: If project not found or has child projects
        """
        pass
