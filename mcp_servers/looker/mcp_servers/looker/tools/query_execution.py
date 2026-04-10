"""Query execution tools.

Tools for creating and running Looker queries against the semantic layer.
"""

import sys
from pathlib import Path

# Add server to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastmcp.utilities.types import Image
from models import (
    CreateQueryRequest,
    CreateQueryResponse,
    ExportQueryRequest,
    ExportQueryResponse,
    QueryResult,
    RunQueryByIdRequest,
    RunQueryPngRequest,
    RunQueryPngResponse,
    RunQueryRequest,
)
from repository_factory import create_repository


async def create_query(request: CreateQueryRequest) -> CreateQueryResponse:
    """Create a reusable query definition."""
    repo = create_repository(CreateQueryRequest, CreateQueryResponse)
    return await repo.get(request)


async def run_query_inline(request: RunQueryRequest) -> QueryResult:
    """Execute a query inline without saving it."""
    repo = create_repository(RunQueryRequest, QueryResult)
    return await repo.get(request)


async def run_query_by_id(request: RunQueryByIdRequest) -> QueryResult:
    """Execute a saved query by its ID."""
    repo = create_repository(RunQueryByIdRequest, QueryResult)
    return await repo.get(request)


async def export_query(request: ExportQueryRequest) -> ExportQueryResponse:
    """Export query results in JSON or CSV format."""
    # Step 1: Get the query results by running the query
    result = await run_query_by_id(RunQueryByIdRequest(query_id=request.query_id))

    # Step 2: Apply the export limit
    limit = request.limit or 5000
    data = result.data[:limit]

    # Step 3: Format based on the requested format
    if request.format == "csv":
        import csv
        import io

        output = io.StringIO()
        # Use result.fields for headers (works even with empty data)
        writer = csv.DictWriter(output, fieldnames=result.fields)
        writer.writeheader()
        if data:
            writer.writerows(data)

        return ExportQueryResponse(format="csv", data=output.getvalue(), row_count=len(data))
    else:  # format == "json" (validated by Pydantic Literal type)
        return ExportQueryResponse(format="json", data=data, row_count=len(data))


async def run_query_png(request: RunQueryPngRequest) -> Image:
    """Execute a query and return results as a PNG chart visualization."""
    import base64
    import os

    from loguru import logger

    repo = create_repository(RunQueryPngRequest, RunQueryPngResponse)
    result = await repo.get(request)

    # Determine file output location
    # Production uses APP_FS_ROOT env var
    # Local development uses ./looker_charts relative to current directory
    state_location = os.getenv("APP_FS_ROOT", "./looker_charts")

    # Create directory if it doesn't exist
    os.makedirs(state_location, exist_ok=True)

    # Write PNG to state location
    file_path = os.path.join(state_location, f"query_{request.query_id}.png")
    png_bytes = base64.b64decode(result.image_data)

    with open(file_path, "wb") as f:
        f.write(png_bytes)

    logger.info(f"PNG chart saved to {file_path} ({len(png_bytes)} bytes)")

    # Return FastMCP Image type (raw bytes, not base64)
    return Image(data=png_bytes, format="png")
