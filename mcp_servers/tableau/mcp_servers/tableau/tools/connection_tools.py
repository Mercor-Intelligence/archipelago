"""Workbook-Datasource Connection tools implementation.

This module implements all 3 connection tools:
- Create (idempotent)
- List
- Delete (idempotent)

These tools manage the many-to-many relationship between workbooks and datasources.

Environment Variables:
- TABLEAU_TEST_MODE: "local" (default) or "http" for live Tableau Cloud
- TABLEAU_SERVER_URL: Tableau Server URL (required for HTTP mode)
- TABLEAU_SITE_ID: Site content URL (required for HTTP mode)
- TABLEAU_TOKEN_NAME: PAT name (required for HTTP mode)
- TABLEAU_TOKEN_SECRET: PAT secret (required for HTTP mode)

Note: In HTTP mode, only list_connections is supported via Tableau REST API.
Create and delete operations are not supported via the REST API since connections
are embedded in workbooks during publish.
"""

import os
from datetime import datetime, timezone

from db.session import get_session
from models import (
    TableauCreateWorkbookConnectionInput,
    TableauCreateWorkbookConnectionOutput,
    TableauDeleteWorkbookConnectionInput,
    TableauDeleteWorkbookConnectionOutput,
    TableauListWorkbookConnectionsInput,
    TableauListWorkbookConnectionsOutput,
)
from repositories.workbook_datasource_repository import WorkbookDatasourceRepository


async def _list_workbook_connections_http(
    request: TableauListWorkbookConnectionsInput,
) -> TableauListWorkbookConnectionsOutput:
    """List workbook connections via Tableau REST API."""
    from tableau_http.tableau_client import TableauHTTPClient

    # Get credentials from environment
    server_url = os.environ.get("TABLEAU_SERVER_URL")
    site_content_url = os.environ.get("TABLEAU_SITE_ID")
    token_name = os.environ.get("TABLEAU_TOKEN_NAME")
    token_secret = os.environ.get("TABLEAU_TOKEN_SECRET")

    if not all([server_url, site_content_url, token_name, token_secret]):
        raise ValueError(
            "HTTP mode requires TABLEAU_SERVER_URL, TABLEAU_SITE_ID, "
            "TABLEAU_TOKEN_NAME, and TABLEAU_TOKEN_SECRET environment variables"
        )

    client = TableauHTTPClient(
        base_url=server_url,
        site_id=site_content_url,
        personal_access_token=f"{token_name}:{token_secret}",
    )
    await client.sign_in()

    endpoint = f"sites/{client.site_id}/workbooks/{request.workbook_id}/connections"
    response_data = await client.get(endpoint)

    # Parse connections from response
    connections_data = response_data.get("connections", {}).get("connection", [])
    now = datetime.now(timezone.utc).isoformat()

    connection_outputs = []
    for conn in connections_data:
        # Tableau API returns connection details, map to our output format
        # Safely handle null datasource object
        datasource_data = conn.get("datasource") or {}
        connection_outputs.append(
            TableauCreateWorkbookConnectionOutput(
                success=True,
                id=conn.get("id"),
                workbook_id=request.workbook_id,
                datasource_id=datasource_data.get("id") or conn.get("id"),
                created_at=now,
            )
        )

    return TableauListWorkbookConnectionsOutput(connections=connection_outputs)


async def tableau_create_workbook_connection(
    request: TableauCreateWorkbookConnectionInput,
) -> TableauCreateWorkbookConnectionOutput:
    """Link a workbook to a datasource. Returns existing connection if already linked."""
    # HTTP mode: not supported - return graceful no-op response
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        return TableauCreateWorkbookConnectionOutput(
            success=False,
            id=None,
            workbook_id=request.workbook_id,
            datasource_id=request.datasource_id,
            created_at=None,
            message="Connection management is not supported via Tableau REST API. "
            "Connections are embedded in workbooks during publish.",
        )

    # Local mode: use database
    repository = WorkbookDatasourceRepository()
    async with get_session() as session:
        connection = await repository.create(
            session=session,
            site_id=request.site_id,
            workbook_id=request.workbook_id,
            datasource_id=request.datasource_id,
        )

        return TableauCreateWorkbookConnectionOutput(
            success=True,
            id=connection.id,
            workbook_id=connection.workbook_id,
            datasource_id=connection.datasource_id,
            created_at=connection.created_at.isoformat(),
        )


async def tableau_list_workbook_connections(
    request: TableauListWorkbookConnectionsInput,
) -> TableauListWorkbookConnectionsOutput:
    """List all datasource connections for a workbook."""
    # HTTP mode: call Tableau REST API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        return await _list_workbook_connections_http(request)

    # Local mode: use database
    repository = WorkbookDatasourceRepository()
    async with get_session() as session:
        connections = await repository.list_by_workbook(
            session=session,
            site_id=request.site_id,
            workbook_id=request.workbook_id,
        )

        connection_outputs = [
            TableauCreateWorkbookConnectionOutput(
                success=True,
                id=conn.id,
                workbook_id=conn.workbook_id,
                datasource_id=conn.datasource_id,
                created_at=conn.created_at.isoformat(),
            )
            for conn in connections
        ]

        return TableauListWorkbookConnectionsOutput(connections=connection_outputs)


async def tableau_delete_workbook_connection(
    request: TableauDeleteWorkbookConnectionInput,
) -> TableauDeleteWorkbookConnectionOutput:
    """Remove a datasource connection from a workbook."""
    # HTTP mode: not supported - return graceful no-op response
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        return TableauDeleteWorkbookConnectionOutput(
            success=False,
            message="Connection management is not supported via Tableau REST API. "
            "Connections are embedded in workbooks during publish.",
        )

    # Local mode: use database
    repository = WorkbookDatasourceRepository()
    async with get_session() as session:
        # Delete returns False if not found, but we treat it as success for idempotency
        await repository.delete(
            session=session,
            site_id=request.site_id,
            workbook_id=request.workbook_id,
            connection_id=request.connection_id,
        )

        return TableauDeleteWorkbookConnectionOutput(success=True)
