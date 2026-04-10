"""Files resource implementation for online provider."""

from typing import Any

from loguru import logger


async def get_files(
    self,
    page: int | None = None,
    page_size: int | None = None,
    sort: str | None = None,
) -> dict[str, Any]:
    """Get file metadata from the live Xero Files API.

    Args:
        page: Optional page number for pagination (1-indexed)
        page_size: Optional number of items per page (max 100, default 50)
        sort: Optional sort field (NAME, SIZE, CREATEDDATEUTC)

    Returns:
        Dictionary containing files array with pagination and metadata

    Reference:
        https://developer.xero.com/documentation/api/files/files
    """
    # Build query parameters
    params: dict[str, Any] = {}

    if page is not None:
        if page < 1:
            raise ValueError("Page number must be >= 1")
        params["page"] = str(page)

    if page_size is not None:
        if page_size < 1 or page_size > 100:
            raise ValueError("Page size must be between 1 and 100")
        params["pageSize"] = str(page_size)

    if sort is not None:
        # Files API accepts: Name, Size, CreatedDateUtc
        sort_upper = sort.upper()
        if sort_upper == "NAME":
            params["sort"] = "Name"
        elif sort_upper == "SIZE":
            params["sort"] = "Size"
        elif sort_upper == "CREATEDDATEUTC":
            params["sort"] = "CreatedDateUtc"
        else:
            raise ValueError(
                f"Invalid sort value: {sort}. Valid sort fields are: Name, Size, CreatedDateUtc"
            )

    logger.debug(f"Fetching files with params: {params}")

    # Files API uses a different base URL: files.xro/1.0
    response = await self._make_request(
        "/Files",
        params=params,
        base_url=self.config.xero_files_api_base_url,
    )

    # Files API returns: TotalCount, Page, PerPage, Items
    # Ensure consistent format
    current_page = page if page is not None else 1
    effective_page_size = page_size if page_size is not None else 50

    # Response already has the correct structure from Xero API
    # Just ensure defaults if not present
    if "Page" not in response:
        response["Page"] = current_page
    if "PerPage" not in response:
        response["PerPage"] = effective_page_size
    if "TotalCount" not in response:
        response["TotalCount"] = len(response.get("Items", []))
    if "Items" not in response:
        response["Items"] = []

    return self._add_metadata(response, "xero-api", "online")
