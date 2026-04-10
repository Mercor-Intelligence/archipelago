"""Site tools for listing sites.

Implements site listing functionality to help frontend teams
discover available site IDs for testing.

Environment Variables:
- TABLEAU_TEST_MODE: "local" (default) or "http" for live Tableau Cloud
- TABLEAU_SERVER_URL: Tableau Server URL (required for HTTP mode)
- TABLEAU_SITE_ID: Site content URL (required for HTTP mode)
- TABLEAU_TOKEN_NAME: PAT name (required for HTTP mode)
- TABLEAU_TOKEN_SECRET: PAT secret (required for HTTP mode)
"""

import os
from datetime import datetime

import httpx
from db.models import Site
from db.session import get_session
from models import TableauListSitesInput, TableauListSitesOutput, TableauSiteOutput
from sqlalchemy import func, select


async def _list_sites_http(request: TableauListSitesInput) -> TableauListSitesOutput:
    """List sites via Tableau REST API."""
    from tableau_http.tableau_client import TableauHTTPClient

    # Get credentials from environment
    server_url = os.environ.get("TABLEAU_SERVER_URL")
    site_id = os.environ.get("TABLEAU_SITE_ID")
    token_name = os.environ.get("TABLEAU_TOKEN_NAME")
    token_secret = os.environ.get("TABLEAU_TOKEN_SECRET")

    if not all([server_url, site_id, token_name, token_secret]):
        raise ValueError(
            "HTTP mode requires TABLEAU_SERVER_URL, TABLEAU_SITE_ID, "
            "TABLEAU_TOKEN_NAME, and TABLEAU_TOKEN_SECRET environment variables"
        )

    client = TableauHTTPClient(
        base_url=server_url,
        site_id=site_id,
        personal_access_token=f"{token_name}:{token_secret}",
    )

    # Sign in to get auth token
    await client.sign_in()

    # Query sites endpoint
    params = {
        "pageSize": request.page_size,
        "pageNumber": request.page_number,
    }

    try:
        response_data = await client.get("sites", params)

        # Parse response
        sites_data = response_data.get("sites", {}).get("site", [])
        pagination = response_data.get("pagination", {})

        site_outputs = [
            TableauSiteOutput(
                id=site.get("id"),
                name=site.get("name"),
                content_url=site.get("contentUrl", ""),
                created_at=site.get("createdAt", datetime.utcnow().isoformat()),
                updated_at=site.get("updatedAt", datetime.utcnow().isoformat()),
            )
            for site in sites_data
        ]

        return TableauListSitesOutput(
            sites=site_outputs,
            total_count=int(pagination.get("totalAvailable", len(site_outputs))),
            page_number=request.page_number,
            page_size=request.page_size,
        )

    except httpx.HTTPStatusError as e:
        # 403 Forbidden - user lacks Server Administrator privileges
        # Fall back to returning the current authenticated site
        if e.response.status_code == 403:
            current_site = TableauSiteOutput(
                id=client.site_id,
                name=client.site_name or site_id,
                content_url=client.site_content_url or site_id,
                created_at=datetime.utcnow().isoformat(),
                updated_at=datetime.utcnow().isoformat(),
            )
            return TableauListSitesOutput(
                sites=[current_site],
                total_count=1,
                page_number=1,
                page_size=request.page_size,
            )
        # Re-raise other HTTP errors
        raise


async def tableau_list_sites(request: TableauListSitesInput) -> TableauListSitesOutput:
    """List all sites with pagination, following the Tableau API pattern (1-indexed page number and page size)."""
    # HTTP mode: call Tableau REST API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        return await _list_sites_http(request)

    # Local mode: use database session
    async with get_session() as session:
        # Get total count
        count_stmt = select(func.count()).select_from(Site)
        count_result = await session.execute(count_stmt)
        total_count = count_result.scalar() or 0

        # Get paginated sites
        offset = (request.page_number - 1) * request.page_size
        stmt = (
            select(Site)
            .order_by(Site.created_at.desc())  # Most recent first
            .limit(request.page_size)
            .offset(offset)
        )
        result = await session.execute(stmt)
        sites = result.scalars().all()

        # Convert to output models
        site_outputs = [
            TableauSiteOutput(
                id=site.id,
                name=site.name,
                content_url=site.content_url,
                created_at=site.created_at.isoformat(),
                updated_at=site.updated_at.isoformat(),
            )
            for site in sites
        ]

        return TableauListSitesOutput(
            sites=site_outputs,
            total_count=total_count,
            page_number=request.page_number,
            page_size=request.page_size,
        )
