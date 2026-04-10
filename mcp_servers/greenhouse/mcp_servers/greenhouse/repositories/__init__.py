"""Repository layer for Greenhouse MCP Server.

Implements the repository pattern for clean separation between
business logic and data persistence.

Usage:
    from repositories import get_repository

    # Get a repository instance with a database session
    async with get_session() as session:
        user_repo = get_repository("users", session)
        users = await user_repo.list()

    # Or use specific repository classes directly
    from repositories import UserRepository

    async with get_session() as session:
        repo = UserRepository(session)
        user = await repo.get(123)
"""

from typing import Literal

from repositories.activity import ActivityRepository
from repositories.applications import ApplicationRepository
from repositories.base import BaseRepository
from repositories.candidates import CandidateRepository
from repositories.exceptions import (
    AccessDeniedError,
    BadRequestError,
    ConflictError,
    NotFoundError,
    RepositoryError,
    ValidationError,
)
from repositories.jobboard import JobBoardRepository
from repositories.jobs import JobRepository
from repositories.scorecards import ScorecardRepository
from repositories.users import UserRepository
from sqlalchemy.ext.asyncio import AsyncSession

# Type for repository names
RepositoryName = Literal[
    "users",
    "jobs",
    "candidates",
    "applications",
    "scorecards",
    "activity",
    "jobboard",
]

# Registry mapping entity names to repository classes
REPOSITORIES: dict[str, type[BaseRepository]] = {
    "users": UserRepository,
    "jobs": JobRepository,
    "candidates": CandidateRepository,
    "applications": ApplicationRepository,
    "scorecards": ScorecardRepository,
    "activity": ActivityRepository,
    "jobboard": JobBoardRepository,
}


def get_repository(entity: RepositoryName, session: AsyncSession) -> BaseRepository:
    """Factory function to get repository instance.

    Args:
        entity: Entity name (users, jobs, candidates, applications,
                scorecards, activity, jobboard)
        session: Async SQLAlchemy session

    Returns:
        Repository instance for the specified entity

    Raises:
        ValueError: If entity name is not recognized

    Example:
        >>> async with get_session() as session:
        ...     user_repo = get_repository("users", session)
        ...     users = await user_repo.list()
    """
    if entity not in REPOSITORIES:
        raise ValueError(
            f"Unknown repository: {entity}. "
            f"Available repositories: {', '.join(REPOSITORIES.keys())}"
        )

    return REPOSITORIES[entity](session)


__all__ = [
    # Factory function
    "get_repository",
    # Base class
    "BaseRepository",
    # Domain repositories
    "UserRepository",
    "JobRepository",
    "CandidateRepository",
    "ApplicationRepository",
    "ScorecardRepository",
    "ActivityRepository",
    "JobBoardRepository",
    # Exceptions
    "RepositoryError",
    "NotFoundError",
    "ValidationError",
    "AccessDeniedError",
    "ConflictError",
    "BadRequestError",
    # Type alias
    "RepositoryName",
]
