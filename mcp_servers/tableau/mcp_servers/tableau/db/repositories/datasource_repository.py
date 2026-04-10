"""Repository for Datasource database operations.

Handles all CRUD operations for datasources with proper validation.
"""

from uuid import uuid4

from db.models import Datasource, Project, User, WorkbookDatasource
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession


class DatasourceRepository:
    """Repository for datasource CRUD operations."""

    def __init__(self, session: AsyncSession):
        """Initialize repository with database session.

        Args:
            session: SQLAlchemy async session
        """
        self.session = session

    async def create(
        self,
        site_id: str,
        name: str,
        project_id: str,
        owner_id: str,
        connection_type: str,
        description: str = "",
    ) -> Datasource:
        """Create a new datasource.

        Args:
            site_id: Site UUID (for multi-tenancy)
            name: Datasource name (required, non-empty)
            project_id: Project UUID (must exist)
            owner_id: Owner user UUID (must exist)
            connection_type: Connection type (required, non-empty)
            description: Optional description

        Returns:
            Created Datasource instance

        Raises:
            ValueError: If name or connection_type are empty or whitespace-only
            ValueError: If project_id or owner_id don't exist
        """
        # Repository-level validation (defense in depth)
        if not name or not name.strip():
            raise ValueError("Name cannot be empty")
        if not connection_type or not connection_type.strip():
            raise ValueError("Connection type cannot be empty")

        # Validate that project exists
        project_stmt = select(Project).where(Project.id == project_id)
        project_result = await self.session.execute(project_stmt)
        if not project_result.scalar_one_or_none():
            raise ValueError(f"Project with id {project_id} does not exist")

        # Validate that owner exists
        user_stmt = select(User).where(User.id == owner_id)
        user_result = await self.session.execute(user_stmt)
        if not user_result.scalar_one_or_none():
            raise ValueError(f"User with id {owner_id} does not exist")

        # Create datasource
        datasource = Datasource(
            id=str(uuid4()),
            site_id=site_id,
            name=name,
            project_id=project_id,
            owner_id=owner_id,
            connection_type=connection_type,
            description=description,
        )
        self.session.add(datasource)
        await self.session.flush()  # Flush to get generated values without committing
        return datasource

    async def get_by_id(self, datasource_id: str, site_id: str) -> Datasource | None:
        """Get datasource by ID, scoped to a specific site.

        Args:
            datasource_id: Datasource UUID
            site_id: Site UUID (for multi-tenancy)

        Returns:
            Datasource instance or None if not found
        """
        stmt = select(Datasource).where(
            (Datasource.id == datasource_id) & (Datasource.site_id == site_id)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_datasources(
        self,
        site_id: str,
        project_id: str | None = None,
        page_number: int = 1,
        page_size: int = 100,
    ) -> tuple[list[Datasource], int]:
        """List datasources with optional filtering and pagination, scoped to a site.

        Args:
            site_id: Site UUID (for multi-tenancy)
            project_id: Optional project ID filter
            page_number: Page number (1-indexed)
            page_size: Number of items per page

        Returns:
            Tuple of (list of datasources, total count)
        """
        # Build base query with site_id filter
        stmt = select(Datasource).where(Datasource.site_id == site_id)
        count_stmt = (
            select(func.count()).select_from(Datasource).where(Datasource.site_id == site_id)
        )

        # Apply project filter if provided
        if project_id:
            stmt = stmt.where(Datasource.project_id == project_id)
            count_stmt = count_stmt.where(Datasource.project_id == project_id)

        # Get total count
        total_result = await self.session.execute(count_stmt)
        total_count = int(total_result.scalar() or 0)

        # Apply deterministic ordering for stable pagination (created_at + id for tiebreaker)
        stmt = stmt.order_by(Datasource.created_at, Datasource.id)

        # Apply pagination
        offset = (page_number - 1) * page_size
        stmt = stmt.offset(offset).limit(page_size)

        # Execute query
        result = await self.session.execute(stmt)
        datasources = list(result.scalars().all())

        return datasources, total_count

    async def update(
        self,
        datasource_id: str,
        site_id: str,
        name: str | None = None,
        description: str | None = None,
        connection_type: str | None = None,
    ) -> Datasource | None:
        """Update datasource fields.

        Only updates provided fields (partial update).

        Args:
            datasource_id: Datasource UUID
            site_id: Site UUID (for multi-tenancy)
            name: Optional new name
            description: Optional new description
            connection_type: Optional new connection type

        Returns:
            Updated Datasource instance or None if not found

        Raises:
            ValueError: If name is empty string
        """
        datasource = await self.get_by_id(datasource_id, site_id)
        if not datasource:
            return None

        # Validate name if provided
        if name is not None:
            if not name or len(name.strip()) == 0:
                raise ValueError("Name cannot be empty")
            datasource.name = name

        # Update description if provided
        if description is not None:
            datasource.description = description

        # Update connection_type if provided
        if connection_type is not None:
            if not connection_type or len(connection_type.strip()) == 0:
                raise ValueError("Connection type cannot be empty")
            datasource.connection_type = connection_type

        await self.session.flush()  # Flush changes without committing
        return datasource

    async def delete(self, datasource_id: str, site_id: str) -> bool:
        """Delete datasource and cascade to workbook connections.

        Args:
            datasource_id: Datasource UUID
            site_id: Site UUID (for multi-tenancy)

        Returns:
            True if deleted, False if not found

        Raises:
            Exception: If deletion fails
        """
        datasource = await self.get_by_id(datasource_id, site_id)
        if not datasource:
            return False

        # Delete workbook-datasource connections first (cascade)
        delete_connections_stmt = delete(WorkbookDatasource).where(
            WorkbookDatasource.datasource_id == datasource_id
        )
        await self.session.execute(delete_connections_stmt)

        # Delete the datasource
        await self.session.delete(datasource)
        await self.session.flush()  # Flush deletions without committing
        return True
