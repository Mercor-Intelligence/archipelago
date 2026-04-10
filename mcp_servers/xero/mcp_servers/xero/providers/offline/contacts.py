"""Contacts resource implementation for offline provider."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from mcp_servers.xero.db.models import Contact
from mcp_servers.xero.db.session import async_session
from mcp_servers.xero.utils import apply_where_filter, validate_where_clause


async def get_contacts(
    self,
    ids: list[str] | None = None,
    where: str | None = None,
    include_archived: bool = False,
    page: int | None = None,
) -> dict[str, Any]:
    """
    Get contacts from database.

    Implements filtering, archival status handling, and pagination
    to match Xero's GetContacts behavior.

    Args:
        ids: Optional list of contact IDs to filter by
        where: Optional filter expression (e.g., 'IsCustomer==true')
        include_archived: Whether to include archived contacts (default: False)
        page: Optional page number for pagination (1-indexed, ~100 items per page)

    Returns:
        Dictionary containing filtered Contacts array and metadata
    """
    async with async_session() as session:
        # Build query
        query = select(Contact)

        # Filter by IDs if provided
        if ids:
            query = query.where(Contact.contact_id.in_(ids))

        # Filter archived contacts unless explicitly included
        if not include_archived:
            query = query.where(Contact.contact_status != "ARCHIVED")

        result = await session.execute(query)
        contacts = result.scalars().all()

        # Convert to dict format
        contacts_data = [contact.to_dict() for contact in contacts]

    # Validate and apply where clause filter
    if where:
        validate_where_clause(where)
        contacts_data = apply_where_filter(contacts_data, where)

    # Apply pagination (page size 100 aligns with Xero defaults)
    page_size = 100
    if page is not None and page > 0:
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        contacts_data = contacts_data[start_idx:end_idx]

    response = {"Contacts": contacts_data}
    return self._add_metadata(response, "xero-mock", "offline")
