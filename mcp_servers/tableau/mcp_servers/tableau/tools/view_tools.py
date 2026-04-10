"""View tools for data science/analyst use cases.

Implements read-only view tools:
- tableau_list_views: Discover available views
- tableau_get_view: Get view details

And data export tools:
- tableau_query_view_data (returns CSV)
- tableau_query_view_image (returns PNG)

Note: Views are created automatically when workbooks are published.
Create/Update/Delete operations are not exposed as they are admin-level
operations typically handled by Tableau Server during workbook publishing.

All tools follow Tableau API v3.x specifications.

Environment Variables:
- TABLEAU_TEST_MODE: "local" (default) or "http" for live Tableau Cloud
- TABLEAU_SERVER_URL: Tableau Server URL (required for HTTP mode)
- TABLEAU_SITE_ID: Site content URL (required for HTTP mode)
- TABLEAU_TOKEN_NAME: PAT name (required for HTTP mode)
- TABLEAU_TOKEN_SECRET: PAT secret (required for HTTP mode)
"""

import os

from db.repositories.view_repository import LocalDBViewRepository
from db.session import get_session
from fastmcp.utilities.types import Image
from models import (
    TableauGetViewInput,
    TableauGetViewMetadataInput,
    TableauGetViewMetadataOutput,
    TableauGetViewOutput,
    TableauListViewsInput,
    TableauListViewsOutput,
    TableauQueryViewDataInput,
    TableauQueryViewDataOutput,
    TableauQueryViewDataToFileOutput,
    TableauQueryViewImageInput,
)


def _get_repository():
    """Get ViewRepository based on environment configuration."""
    test_mode = os.environ.get("TABLEAU_TEST_MODE", "local").lower()

    if test_mode == "http":
        from db.repositories.http_view_repository import HTTPViewRepository
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
        return HTTPViewRepository(client)

    return LocalDBViewRepository()


async def tableau_list_views(
    request: TableauListViewsInput,
) -> TableauListViewsOutput:
    """List views (dashboards and worksheets) with pagination and optional workbook filtering."""
    repository = _get_repository()

    # HTTP mode: sign in and call API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        await repository.client.sign_in()
        return await repository.list_views(session=None, request=request)

    # Local mode: use database session
    async with get_session() as session:
        return await repository.list_views(session=session, request=request)


async def tableau_get_view(
    request: TableauGetViewInput,
) -> TableauGetViewOutput:
    """Get a specific view by ID including name, type, and content URL."""
    repository = _get_repository()

    # HTTP mode: sign in and call API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        await repository.client.sign_in()
        view = await repository.get_by_id(session=None, request=request)
    else:
        # Local mode: use database session
        async with get_session() as session:
            view = await repository.get_by_id(session=session, request=request)

    if not view:
        raise ValueError(f"View {request.view_id} not found")

    return view


# ============================================================================
# DATA EXPORT TOOLS
# ============================================================================


async def _tableau_query_view_data(
    request: TableauQueryViewDataInput,
) -> TableauQueryViewDataOutput:
    """Query view data as CSV for data analysis. Supports optional field filters.

    NOTE: This function is prefixed with _ to exclude it from REST bridge discovery.
    Use tableau_query_view_data_to_file instead.
    """
    repository = _get_repository()

    # HTTP mode: sign in and call API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        await repository.client.sign_in()
        return await repository.query_view_data(session=None, request=request)

    # Local mode: use database session
    async with get_session() as session:
        return await repository.query_view_data(session=session, request=request)


async def tableau_query_view_image(
    request: TableauQueryViewImageInput,
) -> Image:
    """Query view as a PNG image snapshot for embedding in reports or visual analysis."""
    import base64

    from loguru import logger

    repository = _get_repository()

    # HTTP mode: sign in and call API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        await repository.client.sign_in()
        result = await repository.query_view_image(session=None, request=request)
    else:
        # Local mode: use database session
        async with get_session() as session:
            result = await repository.query_view_image(session=session, request=request)

    # Determine file output location
    # Production uses APP_FS_ROOT env var
    # Local development uses ./tableau_images relative to current directory
    state_location = os.getenv("APP_FS_ROOT", "./tableau_images")

    # Decode image bytes
    image_bytes = base64.b64decode(result.image_data_base64)

    # Try to save PNG to state location (optional, for convenience)
    try:
        os.makedirs(state_location, exist_ok=True)
        file_path = os.path.join(state_location, f"view_{request.view_id}.png")
        with open(file_path, "wb") as f:
            f.write(image_bytes)
        logger.info(f"PNG saved to {file_path} ({len(image_bytes)} bytes)")
    except (OSError, PermissionError) as e:
        logger.warning(
            f"Could not save PNG to {state_location}: {e}. Image still returned in response."
        )

    # Return FastMCP Image type (raw bytes, not base64)
    return Image(data=image_bytes, format="png")


# ============================================================================
# VIEW METADATA TOOLS
# ============================================================================


async def tableau_get_view_metadata(
    request: TableauGetViewMetadataInput,
) -> TableauGetViewMetadataOutput:
    """Get field metadata for a view including names, types, and dimension/measure roles."""
    repository = _get_repository()

    # HTTP mode: sign in and call API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        await repository.client.sign_in()
        return await repository.get_view_metadata(session=None, request=request)

    # Local mode: use database session
    async with get_session() as session:
        return await repository.get_view_metadata(session=session, request=request)


async def tableau_query_view_data_to_file(
    request: TableauQueryViewDataInput,
) -> TableauQueryViewDataToFileOutput:
    """Query view data and write to a CSV file. Useful for large datasets."""
    repository = _get_repository()

    # HTTP mode: sign in and call API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        await repository.client.sign_in()
        return await repository.query_view_data_to_file(session=None, request=request)

    # Local mode: use database session
    async with get_session() as session:
        return await repository.query_view_data_to_file(session=session, request=request)
