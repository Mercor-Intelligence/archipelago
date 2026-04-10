"""Projects resource implementation for offline provider."""

from typing import Any

from loguru import logger
from sqlalchemy import select

from mcp_servers.xero.db.models import Project
from mcp_servers.xero.db.session import async_session


async def get_projects(
    self,
    page: int | None = None,
    page_size: int | None = None,
    contact_id: str | None = None,
    states: list[str] | None = None,
) -> dict[str, Any]:
    """
    Get projects from database with filtering and pagination.

    Args:
        page: Page number (1-indexed, defaults to 1)
        page_size: Items per page (defaults to 50)
        contact_id: Filter by contact UUID
        states: Filter by status list (INPROGRESS, CLOSED)

    Returns:
        Dictionary containing pagination metadata, items array, and metadata
    """
    logger.info(
        f"Getting projects from database (page={page}, page_size={page_size}, "
        f"contact_id={contact_id}, states={states})"
    )

    # Default values
    effective_page = page if page is not None else 1
    effective_page_size = page_size if page_size is not None else 50

    # Validate page
    if effective_page < 1:
        raise ValueError("Page number must be >= 1")

    # Validate page size
    if effective_page_size < 1:
        raise ValueError("Page size must be >= 1")

    async with async_session() as session:
        # Build query
        query = select(Project)

        # Apply contact_id filter
        if contact_id:
            query = query.where(Project.contact_id == contact_id)

        # Apply states filter
        if states:
            # Normalize states to uppercase
            normalized_states = [s.upper() for s in states if s]
            if normalized_states:
                query = query.where(Project.status.in_(normalized_states))

        # Execute query
        result = await session.execute(query)
        projects = result.scalars().all()

        # Convert to dict format
        projects_data = [project.to_dict() for project in projects]

    # Calculate pagination metadata
    total_count = len(projects_data)
    total_pages = (
        (total_count + effective_page_size - 1) // effective_page_size if total_count > 0 else 0
    )

    # Apply pagination
    start_idx = (effective_page - 1) * effective_page_size
    end_idx = start_idx + effective_page_size
    paginated_projects = projects_data[start_idx:end_idx]

    # Build pagination metadata
    pagination = {
        "page": effective_page,
        "pageSize": effective_page_size,
        "pageCount": total_pages,
        "itemCount": total_count,
    }

    # Build response
    response = {
        "pagination": pagination,
        "items": paginated_projects,
    }

    logger.info(
        f"Returning {len(paginated_projects)} projects (page {effective_page} of {total_pages}, "
        f"total {total_count} items)"
    )

    return self._add_metadata(response, "xero-mock", "offline")
