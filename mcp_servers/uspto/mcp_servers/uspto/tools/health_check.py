"""Health check tool for the USPTO MCP server."""

from __future__ import annotations

from datetime import UTC, datetime

from loguru import logger

from mcp_servers.uspto import __version__
from mcp_servers.uspto.api.factory import get_uspto_client
from mcp_servers.uspto.config import get_settings, is_online_mode
from mcp_servers.uspto.db.session import check_db_connection, current_db_path
from mcp_servers.uspto.models import (
    DatabaseStatus,
    HealthCheckResponse,
    UpstreamAPIStatus,
)


async def uspto_health_check() -> HealthCheckResponse:
    """Check server health status, database connectivity, and upstream USPTO API availability.

    Returns overall status, server version, mode (online/offline), database connection state,
    and upstream API availability.

    STATUS VALUES:
    - 'healthy': Database connected AND (upstream available OR offline mode)
    - 'degraded': Database connected but upstream unavailable (online mode only)
    - 'unhealthy': Database connection failed

    USE CASE: Call this to verify the server is operational before starting a workflow.
    No authentication or rate limiting applied to this endpoint.
    """
    # 1. Get server version and mode
    version = __version__
    mode = "online" if is_online_mode() else "offline"

    # 2. Check database connectivity
    db_status = DatabaseStatus(
        connected=False,
        path=None,
    )

    try:
        db_status.connected = await check_db_connection()
        # Only report path if actually connected
        if db_status.connected:
            db_status.path = current_db_path()
    except Exception as e:
        logger.warning(f"Database health check failed: {e}")
        db_status.connected = False

    # 3. Check upstream USPTO API availability (if online mode)
    upstream_status = UpstreamAPIStatus(
        available=False,
        reason=None,
    )

    settings = get_settings()
    if mode == "online":
        try:
            # Lightweight ping to USPTO API (no actual data fetch)
            client = get_uspto_client(api_key=settings.api_key)
            try:
                upstream_status.available = await client.ping()
                if not upstream_status.available:
                    upstream_status.reason = "API not responding"
            finally:
                # Cleanup errors should not overwrite successful ping result
                try:
                    await client.aclose()
                except Exception as cleanup_err:
                    logger.warning(f"Client cleanup failed: {cleanup_err}")
        except Exception as e:
            logger.warning(f"Upstream API health check failed: {e}")
            upstream_status.available = False
            upstream_status.reason = str(e)
    else:
        upstream_status.available = False
        upstream_status.reason = "Offline mode active"

    # 4. Determine overall status
    if db_status.connected and (upstream_status.available or mode == "offline"):
        overall_status = "healthy"
    elif db_status.connected:
        overall_status = "degraded"  # DB works but upstream down
    else:
        overall_status = "unhealthy"  # DB connection failed

    # 5. Return response
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    return HealthCheckResponse(
        status=overall_status,
        version=version,
        mode=mode,
        database=db_status,
        upstream_api=upstream_status,
        timestamp=timestamp,
    )
