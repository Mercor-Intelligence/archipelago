"""LocalDBUserRepository for managing user CRUD operations using local database.

This repository handles all database operations for users, following
Tableau REST API v3.x behavior patterns.
"""

from uuid import uuid4

from db.models import Datasource, Project, User, Workbook
from db.repositories.base_user_repository import UserRepository
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
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession


class LocalDBUserRepository(UserRepository):
    """Local database implementation of UserRepository."""

    async def create(
        self,
        session: AsyncSession,
        request: TableauCreateUserInput,
    ) -> TableauCreateUserOutput:
        """Create a new user.

        Args:
            session: Database session
            request: User creation request

        Returns:
            Created user details

        Raises:
            ValueError: If username already exists for this site

        Note:
            Does not commit the transaction. Caller is responsible for committing
            via the context manager to ensure transactional integrity.
        """
        # Check for duplicate username in this site
        existing = await self._get_by_name(session, request.site_id, request.name)
        if existing:
            raise ValueError(f"User with name '{request.name}' already exists on this site")

        user = User(
            id=str(uuid4()),
            site_id=request.site_id,
            name=request.name,
            email=request.email,
            site_role=request.site_role,
        )
        session.add(user)
        await session.flush()  # Flush to get generated values without committing

        return TableauCreateUserOutput(
            id=user.id,
            name=user.name,
            email=user.email,
            site_role=user.site_role,
            created_at=user.created_at.isoformat(),
            updated_at=user.updated_at.isoformat(),
        )

    async def get_by_id(
        self, session: AsyncSession, request: TableauGetUserInput
    ) -> TableauGetUserOutput | None:
        """Get user by ID within a site.

        Args:
            session: Database session
            request: Get user request

        Returns:
            User details if found, None otherwise
        """
        stmt = select(User).where(and_(User.id == request.user_id, User.site_id == request.site_id))
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if not user:
            return None

        return TableauGetUserOutput(
            id=user.id,
            name=user.name,
            email=user.email,
            site_role=user.site_role,
            created_at=user.created_at.isoformat(),
            updated_at=user.updated_at.isoformat(),
        )

    async def list_users(
        self,
        session: AsyncSession,
        request: TableauListUsersInput,
    ) -> TableauListUsersOutput:
        """List users with pagination.

        Args:
            session: Database session
            request: List users request

        Returns:
            Paginated list of users
        """
        # Get total count
        count_stmt = select(func.count(User.id)).where(User.site_id == request.site_id)
        total_result = await session.execute(count_stmt)
        total_count = total_result.scalar_one()

        # Get paginated results
        offset = (request.page_number - 1) * request.page_size
        stmt = (
            select(User)
            .where(User.site_id == request.site_id)
            .order_by(User.created_at.desc())
            .offset(offset)
            .limit(request.page_size)
        )
        result = await session.execute(stmt)
        users = list(result.scalars().all())

        user_outputs = [
            TableauCreateUserOutput(
                id=user.id,
                name=user.name,
                email=user.email,
                site_role=user.site_role,
                created_at=user.created_at.isoformat(),
                updated_at=user.updated_at.isoformat(),
            )
            for user in users
        ]

        return TableauListUsersOutput(
            users=user_outputs,
            total_count=total_count,
            page_number=request.page_number,
            page_size=request.page_size,
        )

    async def update(
        self, session: AsyncSession, request: TableauUpdateUserInput
    ) -> TableauUpdateUserOutput:
        """Update user fields.

        Args:
            session: Database session
            request: Update user request

        Returns:
            Updated user details

        Raises:
            ValueError: If user not found or username conflict

        Note:
            Does not commit the transaction. Caller is responsible for committing.
        """
        get_request = TableauGetUserInput(site_id=request.site_id, user_id=request.user_id)
        user_output = await self.get_by_id(session, get_request)
        if not user_output:
            raise ValueError(f"User {request.user_id} not found on site {request.site_id}")

        # Get the actual ORM object for updating
        stmt = select(User).where(and_(User.id == request.user_id, User.site_id == request.site_id))
        result = await session.execute(stmt)
        user = result.scalar_one()

        # Check for username uniqueness if name is being changed
        if request.name is not None and request.name != user.name:
            existing = await self._get_by_name(session, request.site_id, request.name)
            if existing:
                raise ValueError(f"User with name '{request.name}' already exists on this site")

        # Update fields
        if request.name is not None:
            user.name = request.name
        if request.email is not None:
            user.email = request.email
        if request.site_role is not None:
            user.site_role = request.site_role

        await session.flush()  # Flush changes without committing

        return TableauUpdateUserOutput(
            id=user.id,
            name=user.name,
            email=user.email,
            site_role=user.site_role,
            created_at=user.created_at.isoformat(),
            updated_at=user.updated_at.isoformat(),
        )

    async def delete(
        self, session: AsyncSession, request: TableauDeleteUserInput
    ) -> TableauDeleteUserOutput:
        """Delete user (with Tableau's complex ownership behavior).

        Tableau API v3.x Behavior:
        1. If user owns content and map_assets_to NOT provided:
           - Deletion is BLOCKED
           - User's siteRole changed to "Unlicensed"
           - Returns success=False with role_changed_to="Unlicensed"

        2. If user owns content and map_assets_to IS provided:
           - Content ownership transferred to specified user
           - User is deleted
           - Returns success=True with content_transferred_to

        3. If user doesn't own content:
           - User is deleted
           - Returns success=True

        Args:
            session: Database session
            request: Delete user request

        Returns:
            Deletion result

        Note:
            Does not commit the transaction. Caller is responsible for committing.
        """
        # Check if user owns content
        owns_content = await self._user_owns_content(session, request.user_id)

        if owns_content:
            if request.map_assets_to is None:
                # Tableau behavior: Block deletion, change role to Unlicensed
                await self._change_role(
                    session=session,
                    site_id=request.site_id,
                    user_id=request.user_id,
                    new_role="Unlicensed",
                )

                return TableauDeleteUserOutput(
                    success=False,
                    message=f"User {request.user_id} owns content. "
                    f"Role changed to Unlicensed. "
                    f"Use map_assets_to parameter to transfer ownership and delete.",
                    role_changed_to="Unlicensed",
                    content_transferred_to=None,
                )
            else:
                # Validate that target user exists before transferring
                target_request = TableauGetUserInput(
                    site_id=request.site_id, user_id=request.map_assets_to
                )
                target_user = await self.get_by_id(session, target_request)
                if not target_user:
                    raise ValueError(
                        f"Target user {request.map_assets_to} not found. "
                        f"Cannot transfer ownership to non-existent user."
                    )

                # Transfer ownership and delete
                transferred_count = await self._transfer_ownership(
                    session=session,
                    from_user_id=request.user_id,
                    to_user_id=request.map_assets_to,
                )

                deleted = await self._delete_user(session, request.site_id, request.user_id)

                return TableauDeleteUserOutput(
                    success=True,
                    message=f"User {request.user_id} deleted. "
                    f"{transferred_count} items transferred to {request.map_assets_to}.",
                    role_changed_to=None,
                    content_transferred_to=request.map_assets_to,
                )
        else:
            # No content owned, delete user
            deleted = await self._delete_user(session, request.site_id, request.user_id)

            if not deleted:
                raise ValueError(f"User {request.user_id} not found")

            return TableauDeleteUserOutput(
                success=True,
                message=f"User {request.user_id} deleted successfully.",
                role_changed_to=None,
                content_transferred_to=None,
            )

    # ========================================================================
    # PRIVATE HELPER METHODS
    # ========================================================================

    async def _get_by_name(self, session: AsyncSession, site_id: str, name: str) -> User | None:
        """Get user by username within a site (internal helper).

        Args:
            session: Database session
            site_id: Site identifier
            name: Username

        Returns:
            User if found, None otherwise
        """
        stmt = select(User).where(and_(User.name == name, User.site_id == site_id))
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _delete_user(self, session: AsyncSession, site_id: str, user_id: str) -> bool:
        """Delete user (only if they don't own content) - internal helper.

        Args:
            session: Database session
            site_id: Site identifier
            user_id: User UUID

        Returns:
            True if deleted, False if user not found

        Note:
            Does not commit the transaction. Caller is responsible for committing.
        """
        stmt = select(User).where(and_(User.id == user_id, User.site_id == site_id))
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if not user:
            return False

        await session.delete(user)
        await session.flush()  # Flush deletion without committing
        return True

    async def _change_role(
        self, session: AsyncSession, site_id: str, user_id: str, new_role: str
    ) -> User:
        """Change user's site role (internal helper).

        Args:
            session: Database session
            site_id: Site identifier
            user_id: User UUID
            new_role: New site role

        Returns:
            Updated User instance

        Raises:
            ValueError: If user not found

        Note:
            Does not commit the transaction. Caller is responsible for committing.
        """
        stmt = select(User).where(and_(User.id == user_id, User.site_id == site_id))
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if not user:
            raise ValueError(f"User {user_id} not found on site {site_id}")

        user.site_role = new_role
        await session.flush()  # Flush changes without committing
        return user

    async def _user_owns_content(self, session: AsyncSession, user_id: str) -> bool:
        """Check if user owns any content (internal helper).

        Args:
            session: Database session
            user_id: User UUID

        Returns:
            True if user owns any content, False otherwise
        """
        # Check projects
        project_stmt = select(func.count(Project.id)).where(Project.owner_id == user_id)
        project_result = await session.execute(project_stmt)
        project_count = project_result.scalar_one()

        if project_count > 0:
            return True

        # Check workbooks
        workbook_stmt = select(func.count(Workbook.id)).where(Workbook.owner_id == user_id)
        workbook_result = await session.execute(workbook_stmt)
        workbook_count = workbook_result.scalar_one()

        if workbook_count > 0:
            return True

        # Check datasources
        datasource_stmt = select(func.count(Datasource.id)).where(Datasource.owner_id == user_id)
        datasource_result = await session.execute(datasource_stmt)
        datasource_count = datasource_result.scalar_one()

        return datasource_count > 0

    async def _transfer_ownership(
        self, session: AsyncSession, from_user_id: str, to_user_id: str
    ) -> int:
        """Transfer all owned content from one user to another (internal helper).

        Args:
            session: Database session
            from_user_id: Source user UUID
            to_user_id: Target user UUID

        Returns:
            Total number of items transferred

        Note:
            Does not commit the transaction. Caller is responsible for committing.
            All ownership changes are part of the same transaction for atomicity.
        """
        total_transferred = 0

        # Transfer projects
        project_stmt = select(Project).where(Project.owner_id == from_user_id)
        project_result = await session.execute(project_stmt)
        projects = list(project_result.scalars().all())
        for project in projects:
            project.owner_id = to_user_id
            total_transferred += 1

        # Transfer workbooks
        workbook_stmt = select(Workbook).where(Workbook.owner_id == from_user_id)
        workbook_result = await session.execute(workbook_stmt)
        workbooks = list(workbook_result.scalars().all())
        for workbook in workbooks:
            workbook.owner_id = to_user_id
            total_transferred += 1

        # Transfer datasources
        datasource_stmt = select(Datasource).where(Datasource.owner_id == from_user_id)
        datasource_result = await session.execute(datasource_stmt)
        datasources = list(datasource_result.scalars().all())
        for datasource in datasources:
            datasource.owner_id = to_user_id
            total_transferred += 1

        await session.flush()  # Flush ownership changes without committing
        return total_transferred
