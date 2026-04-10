"""Repository for WorkbookDatasource database operations.

Handles many-to-many relationship between workbooks and datasources.
Implements idempotent create operation with race condition protection.
"""

from uuid import uuid4

from db.models import Datasource, Project, User, Workbook, WorkbookDatasource
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


class WorkbookDatasourceRepository:
    """Repository for workbook-datasource connection operations."""

    async def create(
        self,
        session: AsyncSession,
        site_id: str,
        workbook_id: str,
        datasource_id: str,
    ) -> WorkbookDatasource:
        """Create a workbook-datasource connection (idempotent with race condition protection).

        If the connection already exists, returns the existing connection instead
        of creating a duplicate or raising an error. Handles concurrent creation
        attempts safely.

        Args:
            session: Database session
            site_id: Site UUID for multi-tenancy validation
            workbook_id: Workbook UUID (must exist in the specified site)
            datasource_id: Datasource UUID (must exist in the specified site)

        Returns:
            WorkbookDatasource instance (new or existing)

        Raises:
            ValueError: If workbook_id or datasource_id don't exist or belong to different site

        Note:
            Does not commit the transaction. Caller is responsible for committing
            via the context manager to ensure transactional integrity.

            Race condition handling: If a concurrent request creates the same connection
            between the existence check and insert, this will catch the IntegrityError
            and return the existing connection instead.
        """
        # Validate workbook exists and belongs to the specified site
        workbook_stmt = (
            select(Workbook, Project, User)
            .join(Project, Workbook.project_id == Project.id)
            .join(User, Project.owner_id == User.id)
            .where(Workbook.id == workbook_id)
        )
        workbook_result = await session.execute(workbook_stmt)
        workbook_row = workbook_result.first()

        if not workbook_row:
            raise ValueError(f"Workbook with id {workbook_id} does not exist")

        workbook, project, user = workbook_row
        if user.site_id != site_id:
            raise ValueError(
                f"Workbook {workbook_id} belongs to site {user.site_id}, not {site_id}"
            )

        # Validate datasource exists and belongs to the specified site
        datasource_stmt = (
            select(Datasource, Project, User)
            .join(Project, Datasource.project_id == Project.id)
            .join(User, Project.owner_id == User.id)
            .where(Datasource.id == datasource_id)
        )
        datasource_result = await session.execute(datasource_stmt)
        datasource_row = datasource_result.first()

        if not datasource_row:
            raise ValueError(f"Datasource with id {datasource_id} does not exist")

        datasource, ds_project, ds_user = datasource_row
        if ds_user.site_id != site_id:
            raise ValueError(
                f"Datasource {datasource_id} belongs to site {ds_user.site_id}, not {site_id}"
            )

        # Check if connection already exists (idempotency)
        existing_stmt = select(WorkbookDatasource).where(
            WorkbookDatasource.workbook_id == workbook_id,
            WorkbookDatasource.datasource_id == datasource_id,
        )
        existing_result = await session.execute(existing_stmt)
        existing_connection = existing_result.scalar_one_or_none()

        if existing_connection:
            # Return existing connection (idempotent behavior)
            return existing_connection

        # Create new connection
        connection = WorkbookDatasource(
            id=str(uuid4()),
            workbook_id=workbook_id,
            datasource_id=datasource_id,
        )
        session.add(connection)

        try:
            await session.flush()  # Flush to get generated values without committing
        except IntegrityError:
            # Race condition: Another request created this connection concurrently
            # Rollback and fetch the existing connection
            await session.rollback()
            existing_result = await session.execute(existing_stmt)
            existing_connection = existing_result.scalar_one_or_none()
            if existing_connection:
                return existing_connection
            # If still not found, re-raise the error (shouldn't happen)
            raise

        return connection

    async def list_by_workbook(
        self, session: AsyncSession, site_id: str, workbook_id: str
    ) -> list[WorkbookDatasource]:
        """List all datasource connections for a workbook.

        Validates multi-tenancy: workbook must belong to the specified site
        (through its project's owner).

        Args:
            session: Database session
            site_id: Site UUID for multi-tenancy validation
            workbook_id: Workbook UUID

        Returns:
            List of WorkbookDatasource instances (empty if none found)

        Raises:
            ValueError: If workbook doesn't exist or belongs to different site
        """
        # Validate workbook exists and belongs to the specified site
        workbook_stmt = (
            select(Workbook, Project, User)
            .join(Project, Workbook.project_id == Project.id)
            .join(User, Project.owner_id == User.id)
            .where(Workbook.id == workbook_id)
        )
        workbook_result = await session.execute(workbook_stmt)
        workbook_row = workbook_result.first()

        if not workbook_row:
            raise ValueError(f"Workbook with id {workbook_id} does not exist")

        workbook, project, user = workbook_row
        if user.site_id != site_id:
            raise ValueError(
                f"Workbook {workbook_id} belongs to site {user.site_id}, not {site_id}"
            )

        # List connections for validated workbook
        stmt = (
            select(WorkbookDatasource)
            .where(WorkbookDatasource.workbook_id == workbook_id)
            .order_by(WorkbookDatasource.created_at)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def delete(
        self, session: AsyncSession, site_id: str, workbook_id: str, connection_id: str
    ) -> bool:
        """Delete a workbook-datasource connection (idempotent).

        Validates multi-tenancy: connection must exist, belong to the specified
        workbook, and the workbook must belong to the specified site.

        Args:
            session: Database session
            site_id: Site UUID for multi-tenancy validation
            workbook_id: Workbook UUID to verify connection ownership
            connection_id: Connection UUID

        Returns:
            True if deleted, False if not found (idempotent behavior)

        Raises:
            ValueError: If connection exists but belongs to different workbook,
                       or workbook belongs to different site

        Note:
            Does not commit the transaction. Caller is responsible for committing
            via the context manager to ensure transactional integrity.
        """
        # Validate workbook exists and belongs to the specified site
        workbook_stmt = (
            select(Workbook, Project, User)
            .join(Project, Workbook.project_id == Project.id)
            .join(User, Project.owner_id == User.id)
            .where(Workbook.id == workbook_id)
        )
        workbook_result = await session.execute(workbook_stmt)
        workbook_row = workbook_result.first()

        if not workbook_row:
            raise ValueError(f"Workbook with id {workbook_id} does not exist")

        workbook, project, user = workbook_row
        if user.site_id != site_id:
            raise ValueError(
                f"Workbook {workbook_id} belongs to site {user.site_id}, not {site_id}"
            )

        # Fetch the connection to validate it belongs to the specified workbook
        stmt = select(WorkbookDatasource).where(WorkbookDatasource.id == connection_id)
        result = await session.execute(stmt)
        connection = result.scalar_one_or_none()

        if not connection:
            # Already deleted (idempotent behavior)
            return False

        # Validate connection belongs to the specified workbook
        if connection.workbook_id != workbook_id:
            raise ValueError(
                f"Connection {connection_id} belongs to workbook "
                f"{connection.workbook_id}, not {workbook_id}"
            )

        session.delete(connection)
        await session.flush()  # Flush without committing
        return True
