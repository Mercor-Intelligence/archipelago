"""Base repository for Greenhouse MCP Server.

Provides abstract base class for all domain repositories with standard
CRUD operations and common utilities for database access.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from db.models import Base
from repositories.exceptions import NotFoundError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


class BaseRepository[ModelT: Base](ABC):
    """Abstract base repository with standard CRUD operations.

    All domain repositories should inherit from this class and implement
    the abstract methods for their specific entity type.

    Type Parameters:
        ModelT: The SQLAlchemy model class this repository manages

    Attributes:
        model: The SQLAlchemy model class
        session: The async database session

    Example:
        >>> class UserRepository(BaseRepository[User]):
        ...     model = User
        ...
        ...     async def get(self, id: int) -> dict | None:
        ...         return await self._get_by_id(id)
    """

    model: type[ModelT]

    def __init__(self, session: AsyncSession):
        """Initialize repository with database session.

        Args:
            session: Async SQLAlchemy session for database operations
        """
        self.session = session

    @abstractmethod
    async def get(self, id: int) -> dict | None:
        """Get a single entity by ID.

        Args:
            id: The entity's unique identifier

        Returns:
            Entity data as dict if found, None otherwise
        """
        ...

    @abstractmethod
    async def list(
        self,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict]:
        """List entities with optional filters and pagination.

        Args:
            filters: Optional filter criteria
            page: Page number (1-indexed)
            per_page: Number of results per page

        Returns:
            List of entity data dicts
        """
        ...

    @abstractmethod
    async def create(self, data: dict[str, Any]) -> dict:
        """Create a new entity.

        Args:
            data: Entity data to create

        Returns:
            Created entity data as dict
        """
        ...

    @abstractmethod
    async def update(self, id: int, data: dict[str, Any]) -> dict:
        """Update an existing entity.

        Args:
            id: The entity's unique identifier
            data: Updated entity data

        Returns:
            Updated entity data as dict

        Raises:
            NotFoundError: If entity with given ID doesn't exist
        """
        ...

    @abstractmethod
    async def delete(self, id: int) -> bool:
        """Delete an entity by ID.

        Args:
            id: The entity's unique identifier

        Returns:
            True if deleted, False if not found
        """
        ...

    # =========================================================================
    # Common helper methods for subclasses
    # =========================================================================

    async def _get_by_id(self, id: int) -> ModelT | None:
        """Get model instance by ID.

        Args:
            id: The entity's unique identifier

        Returns:
            Model instance if found, None otherwise
        """
        query = select(self.model).where(self.model.id == id)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def _get_by_id_or_raise(self, id: int, entity_name: str | None = None) -> ModelT:
        """Get model instance by ID or raise NotFoundError.

        Args:
            id: The entity's unique identifier
            entity_name: Name for error message (defaults to model class name)

        Returns:
            Model instance

        Raises:
            NotFoundError: If entity with given ID doesn't exist
        """
        instance = await self._get_by_id(id)
        if instance is None:
            name = entity_name or self.model.__name__
            raise NotFoundError(f"{name} with id {id} does not exist")
        return instance

    async def _count(self, filters: list[Any] | None = None) -> int:
        """Count entities matching filters.

        Args:
            filters: SQLAlchemy filter clauses

        Returns:
            Number of matching entities
        """
        query = select(func.count()).select_from(self.model)
        if filters:
            query = query.where(*filters)
        result = await self.session.scalar(query)
        return result or 0

    async def _paginate(
        self,
        query,
        page: int = 1,
        per_page: int = 100,
    ):
        """Apply pagination to a query.

        Args:
            query: SQLAlchemy select query
            page: Page number (1-indexed)
            per_page: Number of results per page

        Returns:
            Paginated query
        """
        offset = (page - 1) * per_page
        return query.offset(offset).limit(per_page)

    @abstractmethod
    def _serialize(self, instance: ModelT) -> dict[str, Any]:
        """Serialize model instance to Harvest API format.

        Args:
            instance: SQLAlchemy model instance

        Returns:
            Dictionary matching Greenhouse Harvest API response format
        """
        ...
