"""LocalDBGroupRepository for managing group CRUD operations using local database.

This repository handles all database operations for groups, following
Tableau REST API v3.x behavior patterns.
"""

from uuid import uuid4

from db.models import Group, GroupUser, User
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
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession


class LocalDBGroupRepository(GroupRepository):
    """Local database implementation of GroupRepository."""

    async def create(
        self,
        session: AsyncSession,
        request: TableauCreateGroupInput,
    ) -> TableauCreateGroupOutput:
        """Create a new group.

        Args:
            session: Database session
            request: Group creation request

        Returns:
            Created group details

        Raises:
            ValueError: If group name already exists

        Note:
            Does not commit the transaction. Caller is responsible for committing
            via the context manager to ensure transactional integrity.
        """
        # Check for duplicate name
        stmt = select(Group).where(Group.name == request.name)
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            raise ValueError(f"Group with name '{request.name}' already exists")

        # Create group
        group = Group(
            id=str(uuid4()),
            name=request.name,
            description=request.description,
        )
        session.add(group)
        await session.flush()

        return TableauCreateGroupOutput(
            id=group.id,
            name=group.name,
            description=group.description,
            created_at=group.created_at.isoformat(),
            updated_at=group.updated_at.isoformat(),
        )

    async def list_groups(
        self,
        session: AsyncSession,
        request: TableauListGroupsInput,
    ) -> TableauListGroupsOutput:
        """List groups with pagination.

        Args:
            session: Database session
            request: List groups request

        Returns:
            Paginated list of groups
        """
        # Get total count
        count_stmt = select(func.count(Group.id))
        total_result = await session.execute(count_stmt)
        total_count = total_result.scalar_one()

        # Get paginated results
        offset = (request.page_number - 1) * request.page_size
        stmt = (
            select(Group).order_by(Group.created_at.desc()).offset(offset).limit(request.page_size)
        )

        result = await session.execute(stmt)
        groups = list(result.scalars().all())

        group_outputs = [
            TableauCreateGroupOutput(
                id=g.id,
                name=g.name,
                description=g.description,
                created_at=g.created_at.isoformat(),
                updated_at=g.updated_at.isoformat(),
            )
            for g in groups
        ]

        return TableauListGroupsOutput(
            groups=group_outputs,
            total_count=total_count,
            page_number=request.page_number,
            page_size=request.page_size,
        )

    async def add_user_to_group(
        self,
        session: AsyncSession,
        request: TableauAddUserToGroupInput,
    ) -> TableauAddUserToGroupOutput:
        """Add a user to a group.

        Idempotent: If the user is already a member, returns the existing membership.

        Args:
            session: Database session
            request: Add user to group request

        Returns:
            Group membership details

        Raises:
            ValueError: If group or user not found

        Note:
            Does not commit the transaction. Caller is responsible for committing.
        """
        # Validate group exists
        group_stmt = select(Group).where(Group.id == request.group_id)
        group_result = await session.execute(group_stmt)
        group = group_result.scalar_one_or_none()

        if not group:
            raise ValueError(f"Group {request.group_id} not found")

        # Validate user exists
        user_stmt = select(User).where(User.id == request.user_id)
        user_result = await session.execute(user_stmt)
        user = user_result.scalar_one_or_none()

        if not user:
            raise ValueError(f"User {request.user_id} not found")

        # Check if membership already exists (idempotent)
        membership_stmt = select(GroupUser).where(
            and_(
                GroupUser.group_id == request.group_id,
                GroupUser.user_id == request.user_id,
            )
        )
        membership_result = await session.execute(membership_stmt)
        existing_membership = membership_result.scalar_one_or_none()

        if existing_membership:
            # Return existing membership (idempotent)
            return TableauAddUserToGroupOutput(
                id=existing_membership.id,
                group_id=existing_membership.group_id,
                user_id=existing_membership.user_id,
                created_at=existing_membership.created_at.isoformat(),
            )

        # Create new membership
        membership = GroupUser(
            id=str(uuid4()),
            group_id=request.group_id,
            user_id=request.user_id,
        )
        session.add(membership)
        await session.flush()

        return TableauAddUserToGroupOutput(
            id=membership.id,
            group_id=membership.group_id,
            user_id=membership.user_id,
            created_at=membership.created_at.isoformat(),
        )

    async def remove_user_from_group(
        self,
        session: AsyncSession,
        request: TableauRemoveUserFromGroupInput,
    ) -> TableauRemoveUserFromGroupOutput:
        """Remove a user from a group.

        Idempotent: If the user is not a member, returns success.

        Args:
            session: Database session
            request: Remove user from group request

        Returns:
            Success status

        Raises:
            ValueError: If group not found

        Note:
            Does not commit the transaction. Caller is responsible for committing.
        """
        # Validate group exists
        group_stmt = select(Group).where(Group.id == request.group_id)
        group_result = await session.execute(group_stmt)
        group = group_result.scalar_one_or_none()

        if not group:
            raise ValueError(f"Group {request.group_id} not found")

        # Find and delete membership (if exists)
        membership_stmt = select(GroupUser).where(
            and_(
                GroupUser.group_id == request.group_id,
                GroupUser.user_id == request.user_id,
            )
        )
        membership_result = await session.execute(membership_stmt)
        membership = membership_result.scalar_one_or_none()

        if membership:
            await session.delete(membership)
            await session.flush()

        # Return success regardless (idempotent)
        return TableauRemoveUserFromGroupOutput(success=True)
