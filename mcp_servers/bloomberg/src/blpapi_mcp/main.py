import asyncio
import logging
import os
import sys
import threading
from datetime import UTC, datetime

from fastmcp import FastMCP

from fastapi_app.app import app

from .services.stream_consumer import StreamConsumer
from .tools.data_management import (
    data_status,
    download_symbol,
    list_symbols,
)
from .tools.fields import fetch_field_data
from .tools.hello import hello

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create consumer instance
consumer = StreamConsumer()
# Create MCP server from FastAPI app
mcp = FastMCP.from_fastapi(app=app, name="blpapi-mcp")
# Register tools
mcp.tool(hello, name="hello")

# Data management tools
mcp.tool(list_symbols, name="list_symbols")
mcp.tool(data_status, name="data_status")
mcp.tool(download_symbol, name="download_symbol")


@mcp.resource("resource://greeting")
def get_greeting() -> str:
    """Provides a simple greeting message."""
    return "Hello from FastMCP Resources!"


@mcp.resource("resource://fields")
def get_all_fields() -> dict:
    data = fetch_field_data()
    return data


@mcp.tool()
def stream_status() -> dict:
    """Check stream connection status."""
    return {
        "connected": consumer.connected,
        "last_update": consumer.timestamp.isoformat() if consumer.timestamp else None,
        "total_values": len(consumer.history),
    }


@mcp.tool()
def stream_latest() -> dict:
    """Get latest stream value."""
    if consumer.value is None or consumer.timestamp is None:
        return {"value": None, "timestamp": None, "stale": True}

    age = (datetime.now(UTC) - consumer.timestamp).total_seconds()
    return {
        "value": consumer.value,
        "timestamp": consumer.timestamp.isoformat(),
        "age_seconds": round(age, 2),
        "stale": age > 5.0,
    }


@mcp.tool()
def stream_history(count: int = 10) -> list[dict]:
    """Get recent stream history (newest first)."""
    items = list(consumer.history)[-count:]
    items.reverse()
    return [{"value": v, "timestamp": t.isoformat()} for v, t in items]


@mcp.tool()
async def query_bloomberg_data(request_data: dict) -> dict:
    """Query Bloomberg data with any supported request type.

    Args:
        request_data: Dictionary containing:
            - requestType: Type of request (e.g., "HistoricalDataRequest", "ReferenceDataRequest", "IntradayBarRequest")
            - Additional fields depending on request type

    Example request_data:
        {
            "requestType": "HistoricalDataRequest",
            "request_id": "test",
            "securities": ["AAPL US Equity"],
            "fields": ["PX_LAST"],
            "start_date": "2025-11-01T00:00:00Z",
            "end_date": "2025-11-07T00:00:00Z"
        }
    """
    from fastapi_app.services.service_manager import get_service_manager

    manager = get_service_manager()
    manager.initialize()

    # Collect all responses
    responses = []
    stop_event = asyncio.Event()

    try:
        async for envelope in manager.dispatcher.dispatch_async(
            request_data, stop_event=stop_event
        ):
            responses.append(envelope.to_dict())
    except Exception as e:
        return {"error": str(e), "request": request_data}

    return {"responses": responses, "count": len(responses)}


def run_consumer():
    asyncio.run(consumer.start())


def main():
    """Run MCP server with configurable transport.

    Transport is determined by MCP_TRANSPORT environment variable:
    - "stdio" (default): For MCP Inspector and CLI tools
    - "sse": For HTTP/SSE server on port 8001
    """
    consumer_thread = threading.Thread(target=run_consumer, daemon=True)
    consumer_thread.start()

    # Get transport from environment, default to stdio for MCP Inspector compatibility
    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
    logger.info(f"Starting MCP server with {transport.upper()}")
    if transport == "stdio":
        mcp.run(transport="stdio")
        return  # stop execution after server runs

    elif transport in ("streamable-http", "sse"):
        logger.info("local server running on http://0.0.0.0:8001")
        mcp.run(transport=transport, host="0.0.0.0", port=8001)  # type: ignore
        return  # stop execution after server runs

    else:
        logger.error(
            f"Failed to start MCP server, invalid transport: {transport}. "
            "Valid transports: sse, stdio, streamable-http"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
