"""Pagination utilities for Greenhouse MCP server tools."""

from math import ceil


def build_pagination_links(
    base_path: str, page: int, per_page: int, total: int | None
) -> dict[str, str | None] | None:
    """Build Link-style pagination references for list responses.

    Args:
        base_path: The base URL path (e.g., "/candidates", "/applications").
        page: Current page number (1-indexed).
        per_page: Number of results per page.
        total: Total number of results, or None if count was skipped.

    Returns:
        Dictionary with first, prev, self, next, last links, or None if no total.
    """
    if total is None or total <= 0:
        return None

    last_page = max(1, ceil(total / per_page))

    def build_link(target_page: int) -> str:
        return f"{base_path}?per_page={per_page}&page={target_page}"

    prev_page = page - 1 if page > 1 else None
    next_page = page + 1 if page < last_page else None

    return {
        "first": build_link(1),
        "prev": build_link(prev_page) if prev_page is not None else None,
        "self": build_link(page if page >= 1 else 1),
        "next": build_link(next_page) if next_page is not None else None,
        "last": build_link(last_page),
    }
