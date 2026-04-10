"""Query tools for V2 Looker API."""

import csv
import hashlib
import io
import sys
from pathlib import Path

# Add server to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from repository_factory import create_repository

from .models import (
    GetQueryRequest,
    GetQueryResponse,
    RunQueryCsvResponse,
    RunQueryJsonResponse,
    RunQueryRequest,
)


async def looker_create_query(
    model: str,
    view: str,
    fields: list[str],
    filters: dict[str, str] | None = None,
    sorts: list[str] | None = None,
    limit: int = 500,
) -> dict:
    """Create a query definition and return its ID."""
    from models import CreateQueryRequest, CreateQueryResponse, QueryFilter

    # Convert dict filters to QueryFilter objects
    query_filters = []
    if filters:
        for field, value in filters.items():
            query_filters.append(QueryFilter(field=field, value=value))

    request = CreateQueryRequest(
        model=model,
        view=view,
        fields=fields,
        filters=query_filters,
        sorts=sorts or [],
        limit=limit,
    )

    repo = create_repository(CreateQueryRequest, CreateQueryResponse)
    response = await repo.get(request)

    if response is None:
        # Generate a mock query ID
        query_hash = hashlib.md5(f"{model}:{view}:{fields}".encode()).hexdigest()[:8]
        return {
            "query_id": f"q_{query_hash}",
            "model": model,
            "view": view,
            "fields": fields,
            "filters": filters or {},
            "limit": limit,
        }

    return {
        "query_id": str(response.query.id),
        "model": model,
        "view": view,
        "fields": fields,
        "filters": filters or {},
        "limit": limit,
    }


async def looker_run_query_json(request: RunQueryRequest) -> RunQueryJsonResponse:
    """Run a query and return JSON results."""
    from models import QueryResult, RunQueryByIdRequest

    # Try to extract integer query ID (e.g., "q_12345" -> 12345, "2001" -> 2001)
    query_id_str = request.query_id
    try:
        # Handle "q_" prefix from mock IDs
        if query_id_str.startswith("q_"):
            # Mock ID - return empty result
            return RunQueryJsonResponse(
                query_id=request.query_id,
                data=[],
                row_count=0,
            )
        # Try parsing as integer
        query_id_int = int(query_id_str)
    except ValueError:
        # Non-numeric query ID - return empty result
        return RunQueryJsonResponse(
            query_id=request.query_id,
            data=[],
            row_count=0,
        )

    run_request = RunQueryByIdRequest(query_id=query_id_int)
    repo = create_repository(RunQueryByIdRequest, QueryResult)
    result = await repo.get(run_request)

    if result is None:
        return RunQueryJsonResponse(
            query_id=request.query_id,
            data=[],
            row_count=0,
        )

    return RunQueryJsonResponse(
        query_id=request.query_id,
        data=result.data,
        row_count=result.row_count,
        sql=result.sql,
    )


async def looker_run_query_csv(request: RunQueryRequest) -> RunQueryCsvResponse:
    """Run a query and return CSV results."""
    # First get JSON results
    json_response = await looker_run_query_json(request)

    # Convert to CSV
    if not json_response.data:
        return RunQueryCsvResponse(
            query_id=request.query_id,
            csv_data="",
            row_count=0,
        )

    output = io.StringIO()
    if json_response.data:
        # Get headers from first row
        headers = list(json_response.data[0].keys())
        writer = csv.DictWriter(output, fieldnames=headers)
        writer.writeheader()
        writer.writerows(json_response.data)

    return RunQueryCsvResponse(
        query_id=request.query_id,
        csv_data=output.getvalue(),
        row_count=json_response.row_count,
    )


async def looker_get_query(request: GetQueryRequest) -> GetQueryResponse:
    """Get a saved query definition by ID."""
    from query_store import get_query_store

    # Try to extract integer query ID
    query_id_str = request.query_id
    try:
        if query_id_str.startswith("q_"):
            # Mock ID - return empty response
            return GetQueryResponse(query_id=request.query_id)
        query_id_int = int(query_id_str)
    except ValueError:
        return GetQueryResponse(query_id=request.query_id)

    # Look up in query store
    query_store = get_query_store()
    query = query_store.get(query_id_int)

    if query is None:
        return GetQueryResponse(query_id=request.query_id)

    # Convert Query object to response
    # Preserve all filter values (multi-value filters are supported)
    return GetQueryResponse(
        query_id=request.query_id,
        model=query.model,
        view=query.view,
        fields=query.fields,
        filters=query.filters,
        sorts=query.sorts,
        limit=query.limit,
    )
