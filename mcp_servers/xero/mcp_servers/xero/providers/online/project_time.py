"""Project time entries resource implementation for online provider."""

import math
from typing import Any

import httpx
from loguru import logger

from mcp_servers.xero.providers.exceptions import XeroApiAuthenticationError


async def get_project_time(
    self,
    project_id: str,
    page: int | None = None,
    page_size: int | None = None,
) -> dict[str, Any]:
    """
    Get time entries for a specific project from Xero Projects API.

    Retrieves time entries for tracking hours spent on a project.

    Args:
        project_id: Project UUID to get time entries for (required)
        page: Page number (1-indexed, defaults to 1)
        page_size: Items per page (defaults to 50)

    Returns:
        Dictionary containing pagination metadata, items array, and metadata

    Reference: https://developer.xero.com/documentation/api/projects/time
    """
    tenant_id = self.config.xero_tenant_id
    if not tenant_id:
        raise ValueError("No tenant selected. Please configure XERO_TENANT_ID.")

    access_token = await self.oauth_manager.get_valid_access_token()
    if not access_token:
        raise XeroApiAuthenticationError("Failed to obtain valid access token.")

    # Projects API uses a different base URL
    projects_base_url = "https://api.xero.com/projects.xro/2.0"
    endpoint = f"/Projects/{project_id}/Time"

    # Build query parameters
    params: dict[str, Any] = {}

    if page is not None:
        if page < 1:
            raise ValueError("Page number must be >= 1")
        params["page"] = str(page)

    if page_size is not None:
        if page_size < 1:
            raise ValueError("Page size must be >= 1")
        params["pageSize"] = str(page_size)

    # Build URL
    url = httpx.URL(f"{projects_base_url.rstrip('/')}/{endpoint.lstrip('/')}")

    # Build headers using base class method
    headers = self._build_headers(access_token, tenant_id, {})

    logger.debug(f"Fetching project time entries for project {project_id} with params: {params}")

    # Enforce rate limiting
    await self._enforce_client_rate_limit(tenant_id)

    # Make request
    try:
        response = await self.client.get(str(url), headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as e:
        logger.error(
            f"HTTP error fetching project time entries: {e.response.status_code} - {e.response.text}"
        )
        # Map HTTP errors using base class method
        raise self._map_http_error(e.response) from e
    except httpx.RequestError as e:
        logger.error(f"Request error fetching project time entries: {e}")
        from mcp_servers.xero.providers.exceptions import XeroApiNetworkError

        raise XeroApiNetworkError(
            "Network error while calling Xero Projects API.",
            details={"endpoint": endpoint},
        ) from e

    # Projects API returns pagination and items structure
    # Ensure response has the expected structure
    if "pagination" not in data:
        # If API returns items directly, wrap it
        if "items" in data:
            items = data.get("items", [])
            effective_page = page if page is not None else 1
            effective_page_size = page_size if page_size is not None else 50

            def _parse_int(value: str | None) -> int | None:
                try:
                    return int(value) if value is not None else None
                except (TypeError, ValueError):
                    return None

            # Prefer pagination hints from headers when the API omits pagination body
            header_total_count = _parse_int(response.headers.get("x-pagination-item-count"))
            header_page_count = _parse_int(response.headers.get("x-pagination-page-count"))
            header_page_size = _parse_int(response.headers.get("x-pagination-page-size"))

            if header_page_size:
                effective_page_size = header_page_size

            total_count = header_total_count if header_total_count is not None else len(items)
            if header_page_count is not None:
                total_pages = header_page_count
            else:
                total_pages = math.ceil(total_count / effective_page_size) if total_count > 0 else 0

            data["pagination"] = {
                "page": effective_page,
                "pageSize": effective_page_size,
                "pageCount": total_pages,
                "itemCount": total_count,
            }
        else:
            # Wrap in expected structure
            effective_page = page if page is not None else 1
            effective_page_size = page_size if page_size is not None else 50
            total_count = 0
            total_pages = 0
            data = {
                "pagination": {
                    "page": effective_page,
                    "pageSize": effective_page_size,
                    "pageCount": total_pages,
                    "itemCount": total_count,
                },
                "items": [],
            }

    logger.info(f"Retrieved {len(data.get('items', []))} time entries for project {project_id}")

    return self._add_metadata(data, "xero-api", "online")
