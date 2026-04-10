"""Project time entries resource implementation for offline provider."""

from typing import Any

from loguru import logger
from sqlalchemy import desc, select

from mcp_servers.xero.db.models import TimeEntry
from mcp_servers.xero.db.session import async_session


async def get_project_time(
    self,
    project_id: str,
    page: int | None = None,
    page_size: int | None = None,
) -> dict[str, Any]:
    """
    Get time entries for a specific project from database with pagination.

    Args:
        project_id: Project UUID to get time entries for (required)
        page: Page number (1-indexed, defaults to 1)
        page_size: Items per page (defaults to 50)

    Returns:
        Dictionary containing pagination metadata, items array, and metadata
    """
    logger.info(
        f"Getting project time entries from database (project_id={project_id}, "
        f"page={page}, page_size={page_size})"
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
        # Build query - filter by project_id, order by date descending for deterministic pagination
        query = (
            select(TimeEntry)
            .where(TimeEntry.project_id == project_id)
            .order_by(desc(TimeEntry.date_utc), TimeEntry.time_entry_id)
        )

        # Execute query
        result = await session.execute(query)
        time_entries = result.scalars().all()

        # Convert to dict format
        time_entries_data = [entry.to_dict() for entry in time_entries]

    # Calculate pagination metadata
    total_count = len(time_entries_data)
    total_pages = (
        (total_count + effective_page_size - 1) // effective_page_size if total_count > 0 else 0
    )

    # Apply pagination
    start_idx = (effective_page - 1) * effective_page_size
    end_idx = start_idx + effective_page_size
    paginated_entries = time_entries_data[start_idx:end_idx]

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
        "items": paginated_entries,
    }

    logger.info(
        f"Returning {len(paginated_entries)} time entries for project {project_id} "
        f"(page {effective_page} of {total_pages}, total {total_count} items)"
    )

    return self._add_metadata(response, "xero-mock", "offline")
