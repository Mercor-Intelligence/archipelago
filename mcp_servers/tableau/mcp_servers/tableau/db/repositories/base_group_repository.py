"""Abstract base class for GroupRepository implementations.

This module defines the interface that all GroupRepository implementations
must follow, whether they use local database storage or HTTP API calls.
"""

from abc import ABC, abstractmethod

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


class GroupRepository(ABC):
    """Abstract base class for group management repositories."""

    @abstractmethod
    async def create(
        self,
        session: AsyncSession,
        request: TableauCreateGroupInput,
    ) -> TableauCreateGroupOutput:
        """Create a new group.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: Group creation request

        Returns:
            Created group details

        Raises:
            ValueError: If group name already exists or validation fails
        """
        pass

    @abstractmethod
    async def list_groups(
        self,
        session: AsyncSession,
        request: TableauListGroupsInput,
    ) -> TableauListGroupsOutput:
        """List groups with pagination.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: List groups request

        Returns:
            Paginated list of groups
        """
        pass

    @abstractmethod
    async def add_user_to_group(
        self,
        session: AsyncSession,
        request: TableauAddUserToGroupInput,
    ) -> TableauAddUserToGroupOutput:
        """Add a user to a group.

        Idempotent: If the user is already a member, returns existing membership.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: Add user to group request

        Returns:
            Group membership details

        Raises:
            ValueError: If group or user not found
        """
        pass

    @abstractmethod
    async def remove_user_from_group(
        self,
        session: AsyncSession,
        request: TableauRemoveUserFromGroupInput,
    ) -> TableauRemoveUserFromGroupOutput:
        """Remove a user from a group.

        Idempotent: If the user is not a member, returns success.

        Args:
            session: Database session (may be unused for HTTP implementations)
            request: Remove user from group request

        Returns:
            Success status

        Raises:
            ValueError: If group not found
        """
        pass
