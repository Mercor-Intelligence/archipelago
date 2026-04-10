"""Health check MCP tools for Workday HCM.

Implements:
- workday_health_check: Server health check
"""

import time
from datetime import UTC, datetime

from db.session import get_engine
from mcp_auth import public_tool
from models import HealthCheckOutput
from sqlalchemy import text
from utils.decorators import make_async_background

# Server start time for uptime calculation
_server_start_time = time.time()

# Server version
_SERVER_VERSION = "2.0.0"

# Database latency threshold for degraded status (in seconds)
_DB_LATENCY_THRESHOLD = 1.0


@make_async_background
@public_tool
def workday_health_check() -> HealthCheckOutput:
    """Check server health status including database connectivity and uptime."""
    # Check database connectivity and latency
    database_connected = False
    db_latency = 0.0
    try:
        engine = get_engine()
        start = time.time()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_latency = time.time() - start
        database_connected = True
    except Exception:
        database_connected = False

    # Determine status based on connectivity and latency
    if not database_connected:
        status = "unhealthy"
    elif db_latency > _DB_LATENCY_THRESHOLD:
        status = "degraded"
    else:
        status = "healthy"

    # Calculate uptime
    uptime_seconds = int(time.time() - _server_start_time)

    # Current timestamp
    timestamp = datetime.now(UTC).isoformat()

    return HealthCheckOutput(
        status=status,
        database_connected=database_connected,
        version=_SERVER_VERSION,
        uptime_seconds=uptime_seconds,
        timestamp=timestamp,
    )
