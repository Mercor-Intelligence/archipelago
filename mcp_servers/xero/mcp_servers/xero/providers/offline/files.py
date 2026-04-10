"""Files resource implementation for offline provider."""

from typing import Any

from sqlalchemy import select

from mcp_servers.xero.db.models import File
from mcp_servers.xero.db.session import async_session


async def get_files(
    self,
    page: int | None = None,
    page_size: int | None = None,
    sort: str | None = None,
) -> dict[str, Any]:
    """Get file metadata from database.

    Args:
        page: Optional page number for pagination (1-indexed)
        page_size: Optional number of items per page (max 100, default 50)
        sort: Optional sort field (NAME, SIZE, CREATEDDATEUTC)

    Returns:
        Dictionary containing files array with pagination and metadata
    """
    async with async_session() as session:
        result = await session.execute(select(File))
        files = result.scalars().all()

        # Convert to dict format
        files_data = [file.to_dict() for file in files]

    # Apply sorting if specified
    if sort:
        sort_upper = sort.upper()
        if sort_upper == "NAME":
            files_data.sort(key=lambda x: x.get("Name", "") or "")
        elif sort_upper == "SIZE":
            files_data.sort(key=lambda x: x.get("Size", 0) or 0)
        elif sort_upper == "CREATEDDATEUTC":
            files_data.sort(key=lambda x: x.get("CreatedDateUtc", "") or "")
        else:
            raise ValueError(
                f"Invalid sort value: {sort}. Valid sort fields are: Name, Size, CreatedDateUtc"
            )

    # Apply pagination (default page_size 50, max 100 per spec)
    effective_page_size = page_size if page_size is not None else 50
    current_page = page if page is not None else 1

    # Validate page and page_size
    if current_page < 1:
        raise ValueError("Page number must be >= 1")
    if effective_page_size < 1 or effective_page_size > 100:
        raise ValueError("Page size must be between 1 and 100")
    total_count = len(files_data)
    start_idx = (current_page - 1) * effective_page_size
    end_idx = start_idx + effective_page_size
    files_data = files_data[start_idx:end_idx]

    # Build response matching Xero Files API format
    response: dict[str, Any] = {
        "TotalCount": total_count,
        "Page": current_page,
        "PerPage": effective_page_size,
        "Items": files_data,
    }

    return self._add_metadata(response, "xero-mock", "offline")
