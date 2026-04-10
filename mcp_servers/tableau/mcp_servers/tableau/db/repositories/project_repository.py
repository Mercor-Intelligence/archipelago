"""LocalDBProjectRepository for managing project CRUD operations using local database.

This repository handles all database operations for projects, following
Tableau REST API v3.x behavior patterns including hierarchical project support.
"""

from uuid import uuid4

from db.models import Project
from db.repositories.base_project_repository import ProjectRepository
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
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession


class LocalDBProjectRepository(ProjectRepository):
    """Local database implementation of ProjectRepository."""

    async def create(
        self,
        session: AsyncSession,
        request: TableauCreateProjectInput,
    ) -> TableauCreateProjectOutput:
        """Create a new project.

        Args:
            session: Database session
            request: Project creation request

        Returns:
            Created project details

        Raises:
            ValueError: If parent project doesn't exist

        Note:
            Does not commit the transaction. Caller is responsible for committing
            via the context manager to ensure transactional integrity.
        """
        if request.parent_project_id is not None:
            parent_request = TableauGetProjectInput(
                site_id=request.site_id, project_id=request.parent_project_id
            )
            parent = await self.get_by_id(session, parent_request)
            if not parent:
                raise ValueError(f"Parent project {request.parent_project_id} not found")

        project = Project(
            id=str(uuid4()),
            site_id=request.site_id,
            name=request.name,
            description=request.description,
            owner_id=request.owner_id,
            parent_project_id=request.parent_project_id,
        )
        session.add(project)
        await session.flush()

        return TableauCreateProjectOutput(
            id=project.id,
            name=project.name,
            description=project.description,
            parent_project_id=project.parent_project_id,
            owner_id=project.owner_id,
            created_at=project.created_at.isoformat(),
            updated_at=project.updated_at.isoformat(),
        )

    async def get_by_id(
        self, session: AsyncSession, request: TableauGetProjectInput
    ) -> TableauGetProjectOutput | None:
        """Get project by ID, scoped to a specific site.

        Args:
            session: Database session
            request: Get project request

        Returns:
            Project details if found, None otherwise
        """
        stmt = select(Project).where(
            and_(Project.id == request.project_id, Project.site_id == request.site_id)
        )
        result = await session.execute(stmt)
        project = result.scalar_one_or_none()

        if not project:
            return None

        return TableauGetProjectOutput(
            id=project.id,
            name=project.name,
            description=project.description,
            parent_project_id=project.parent_project_id,
            owner_id=project.owner_id,
            created_at=project.created_at.isoformat(),
            updated_at=project.updated_at.isoformat(),
        )

    async def list_projects(
        self,
        session: AsyncSession,
        request: TableauListProjectsInput,
    ) -> TableauListProjectsOutput:
        """List projects with pagination and optional parent filter, scoped to a site.

        Args:
            session: Database session
            request: List projects request

        Returns:
            Paginated list of projects

        Note:
            When parent_project_id is None, returns only root projects (no parent).
            To get ALL projects, use a separate query without filtering.
        """
        base_query = select(Project).where(Project.site_id == request.site_id)
        if request.parent_project_id is None:
            base_query = base_query.where(Project.parent_project_id.is_(None))
        else:
            base_query = base_query.where(Project.parent_project_id == request.parent_project_id)

        count_stmt = select(func.count()).select_from(base_query.subquery())
        total_result = await session.execute(count_stmt)
        total_count = total_result.scalar_one()

        offset = (request.page_number - 1) * request.page_size
        stmt = (
            base_query.order_by(Project.created_at.desc()).offset(offset).limit(request.page_size)
        )
        result = await session.execute(stmt)
        projects = list(result.scalars().all())

        project_outputs = [
            TableauCreateProjectOutput(
                id=project.id,
                name=project.name,
                description=project.description,
                parent_project_id=project.parent_project_id,
                owner_id=project.owner_id,
                created_at=project.created_at.isoformat(),
                updated_at=project.updated_at.isoformat(),
            )
            for project in projects
        ]

        return TableauListProjectsOutput(
            projects=project_outputs,
            total_count=total_count,
            page_number=request.page_number,
            page_size=request.page_size,
        )

    async def update(
        self, session: AsyncSession, request: TableauUpdateProjectInput
    ) -> TableauUpdateProjectOutput:
        """Update project fields.

        Args:
            session: Database session
            request: Update project request

        Returns:
            Updated project details

        Raises:
            ValueError: If project not found

        Note:
            Does not commit the transaction. Caller is responsible for committing.
            Parent project cannot be changed after creation (Tableau behavior).
        """
        stmt = select(Project).where(Project.id == request.project_id)
        result = await session.execute(stmt)
        project = result.scalar_one_or_none()

        if not project:
            raise ValueError(f"Project {request.project_id} not found")

        if request.name is not None:
            project.name = request.name
        if request.description is not None:
            project.description = request.description

        await session.flush()

        return TableauUpdateProjectOutput(
            id=project.id,
            name=project.name,
            description=project.description,
            parent_project_id=project.parent_project_id,
            owner_id=project.owner_id,
            created_at=project.created_at.isoformat(),
            updated_at=project.updated_at.isoformat(),
        )

    async def delete(
        self, session: AsyncSession, request: TableauDeleteProjectInput
    ) -> TableauDeleteProjectOutput:
        """Delete project.

        Args:
            session: Database session
            request: Delete project request

        Returns:
            Deletion result

        Raises:
            ValueError: If project not found or has child projects

        Note:
            Does not commit the transaction. Caller is responsible for committing.
            Tableau may have additional checks for workbooks/datasources in the project.
        """
        stmt = select(Project).where(Project.id == request.project_id)
        result = await session.execute(stmt)
        project = result.scalar_one_or_none()

        if not project:
            raise ValueError(f"Project {request.project_id} not found")

        if await self.has_children(session, request.project_id):
            raise ValueError(
                f"Cannot delete project {request.project_id}: project has child projects. "
                "Delete or move child projects first."
            )

        await session.delete(project)
        await session.flush()

        return TableauDeleteProjectOutput(
            success=True, message=f"Project {request.project_id} deleted successfully."
        )

    async def has_children(self, session: AsyncSession, project_id: str) -> bool:
        """Check if project has child projects.

        Args:
            session: Database session
            project_id: Project UUID

        Returns:
            True if project has children, False otherwise
        """
        stmt = select(func.count(Project.id)).where(Project.parent_project_id == project_id)
        result = await session.execute(stmt)
        count = result.scalar_one()
        return count > 0
