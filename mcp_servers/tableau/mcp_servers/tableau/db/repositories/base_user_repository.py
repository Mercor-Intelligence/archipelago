"""Abstract base class for UserRepository implementations.

This module defines the interface that all UserRepository implementations
must follow, whether they use local database storage or HTTP API calls.
"""

from abc import ABC, abstractmethod

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


class UserRepository(ABC):
    """Abstract base class for user management repositories."""

    @abstractmethod
    async def create(
        self,
        session: AsyncSession,
        request: TableauCreateUserInput,
    ) -> TableauCreateUserOutput:
        """Create a new user.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: User creation request

        Returns:
            Created user details

        Raises:
            ValueError: If username already exists or validation fails
        """
        pass

    @abstractmethod
    async def get_by_id(
        self, session: AsyncSession, request: TableauGetUserInput
    ) -> TableauGetUserOutput | None:
        """Get user by ID.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: Get user request

        Returns:
            User details if found, None otherwise
        """
        pass

    @abstractmethod
    async def list_users(
        self,
        session: AsyncSession,
        request: TableauListUsersInput,
    ) -> TableauListUsersOutput:
        """List users with pagination.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: List users request

        Returns:
            Paginated list of users
        """
        pass

    @abstractmethod
    async def update(
        self, session: AsyncSession, request: TableauUpdateUserInput
    ) -> TableauUpdateUserOutput:
        """Update user fields.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: Update user request

        Returns:
            Updated user details

        Raises:
            ValueError: If user not found or validation fails
        """
        pass

    @abstractmethod
    async def delete(
        self, session: AsyncSession, request: TableauDeleteUserInput
    ) -> TableauDeleteUserOutput:
        """Delete user (with Tableau's complex ownership behavior).

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: Delete user request

        Returns:
            Deletion result

        Raises:
            ValueError: If user not found
        """
        pass
