"""LocalDBWorkbookRepository for managing workbook CRUD operations using local database.

This repository handles all database operations for workbooks, following
Tableau REST API v3.x behavior patterns.
"""

from uuid import uuid4

from db.models import Workbook
from db.repositories.base_workbook_repository import WorkbookRepository
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
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession


class LocalDBWorkbookRepository(WorkbookRepository):
    """Local database implementation of WorkbookRepository."""

    async def create(
        self,
        session: AsyncSession,
        request: TableauCreateWorkbookInput,
    ) -> TableauCreateWorkbookOutput:
        """Create a new workbook.

        Args:
            session: Database session
            request: Workbook creation request

        Returns:
            Created workbook details

        Note:
            Does not commit the transaction. Caller is responsible for committing
            via the context manager to ensure transactional integrity.
        """
        workbook = Workbook(
            id=str(uuid4()),
            site_id=request.site_id,
            name=request.name,
            project_id=request.project_id,
            owner_id=request.owner_id,
            description=request.description,
            file_reference=request.file_reference,
        )
        session.add(workbook)
        await session.flush()

        return TableauCreateWorkbookOutput(
            id=workbook.id,
            name=workbook.name,
            project_id=workbook.project_id,
            owner_id=workbook.owner_id,
            file_reference=workbook.file_reference,
            description=workbook.description,
            created_at=workbook.created_at.isoformat(),
            updated_at=workbook.updated_at.isoformat(),
        )

    async def get_by_id(
        self, session: AsyncSession, request: TableauGetWorkbookInput
    ) -> TableauGetWorkbookOutput | None:
        """Get workbook by ID, scoped to a specific site.

        Args:
            session: Database session
            request: Get workbook request

        Returns:
            Workbook details if found, None otherwise
        """
        stmt = select(Workbook).where(
            and_(Workbook.id == request.workbook_id, Workbook.site_id == request.site_id)
        )
        result = await session.execute(stmt)
        workbook = result.scalar_one_or_none()

        if not workbook:
            return None

        return TableauGetWorkbookOutput(
            id=workbook.id,
            name=workbook.name,
            project_id=workbook.project_id,
            owner_id=workbook.owner_id,
            file_reference=workbook.file_reference,
            description=workbook.description,
            created_at=workbook.created_at.isoformat(),
            updated_at=workbook.updated_at.isoformat(),
        )

    async def list_workbooks(
        self,
        session: AsyncSession,
        request: TableauListWorkbooksInput,
    ) -> TableauListWorkbooksOutput:
        """List workbooks with pagination and optional filters, scoped to a site.

        Args:
            session: Database session
            request: List workbooks request

        Returns:
            Paginated list of workbooks
        """
        # Build filter conditions
        conditions = [Workbook.site_id == request.site_id]
        if request.project_id:
            conditions.append(Workbook.project_id == request.project_id)
        if request.owner_id:
            conditions.append(Workbook.owner_id == request.owner_id)

        # Get total count
        count_stmt = select(func.count(Workbook.id))
        if conditions:
            count_stmt = count_stmt.where(and_(*conditions))
        total_result = await session.execute(count_stmt)
        total_count = total_result.scalar_one()

        # Get paginated results
        offset = (request.page_number - 1) * request.page_size
        stmt = (
            select(Workbook)
            .order_by(Workbook.created_at.desc())
            .offset(offset)
            .limit(request.page_size)
        )
        if conditions:
            stmt = stmt.where(and_(*conditions))

        result = await session.execute(stmt)
        workbooks = list(result.scalars().all())

        workbook_outputs = [
            TableauCreateWorkbookOutput(
                id=wb.id,
                name=wb.name,
                project_id=wb.project_id,
                owner_id=wb.owner_id,
                file_reference=wb.file_reference,
                description=wb.description,
                created_at=wb.created_at.isoformat(),
                updated_at=wb.updated_at.isoformat(),
            )
            for wb in workbooks
        ]

        return TableauListWorkbooksOutput(
            workbooks=workbook_outputs,
            total_count=total_count,
            page_number=request.page_number,
            page_size=request.page_size,
        )

    async def update(
        self, session: AsyncSession, request: TableauUpdateWorkbookInput
    ) -> TableauUpdateWorkbookOutput:
        """Update workbook fields.

        Args:
            session: Database session
            request: Update workbook request

        Returns:
            Updated workbook details

        Raises:
            ValueError: If workbook not found

        Note:
            Does not commit the transaction. Caller is responsible for committing.
        """
        stmt = select(Workbook).where(Workbook.id == request.workbook_id)
        result = await session.execute(stmt)
        workbook = result.scalar_one_or_none()

        if not workbook:
            raise ValueError(f"Workbook {request.workbook_id} not found")

        # Update fields
        if request.name is not None:
            workbook.name = request.name
        if request.description is not None:
            workbook.description = request.description

        await session.flush()

        return TableauUpdateWorkbookOutput(
            id=workbook.id,
            name=workbook.name,
            project_id=workbook.project_id,
            owner_id=workbook.owner_id,
            file_reference=workbook.file_reference,
            description=workbook.description,
            created_at=workbook.created_at.isoformat(),
            updated_at=workbook.updated_at.isoformat(),
        )

    async def delete(
        self, session: AsyncSession, request: TableauDeleteWorkbookInput
    ) -> TableauDeleteWorkbookOutput:
        """Delete workbook.

        Args:
            session: Database session
            request: Delete workbook request

        Returns:
            Deletion result

        Raises:
            ValueError: If workbook not found

        Note:
            Does not commit the transaction. Caller is responsible for committing.
        """
        stmt = select(Workbook).where(Workbook.id == request.workbook_id)
        result = await session.execute(stmt)
        workbook = result.scalar_one_or_none()

        if not workbook:
            raise ValueError(f"Workbook {request.workbook_id} not found")

        await session.delete(workbook)
        await session.flush()

        return TableauDeleteWorkbookOutput(
            success=True,
            message=f"Workbook {request.workbook_id} deleted successfully.",
        )
