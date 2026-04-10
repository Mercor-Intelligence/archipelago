"""Health check utility tool.

Tool for verifying server status and reporting loaded resources.
"""

import sys
from pathlib import Path

# Add server to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import settings
from models import HealthCheckRequest, HealthCheckResponse
from query_store import get_query_store
from stores import (
    DASHBOARDS,
    EXPLORES,
    LOOKS,
    MODELS,
)


async def health_check(request: HealthCheckRequest) -> HealthCheckResponse:
    """Verify server status and configuration."""
    is_offline = settings.is_offline_mode()
    is_hybrid = settings.is_hybrid_mode()

    # Determine mode string
    if is_hybrid:
        mode = "hybrid"
    elif is_offline:
        mode = "offline"
    else:
        mode = "online"

    if is_offline:
        # Offline/Hybrid mode: count loaded resources
        schemas_loaded = len(MODELS)
        explores_loaded = len(EXPLORES)
        saved_queries = len(get_query_store())
        dashboards = len(DASHBOARDS)
        looks = len(LOOKS)
    else:
        # Online mode: resources are fetched dynamically from API
        # Return 0 to indicate no pre-loaded resources
        schemas_loaded = 0
        explores_loaded = 0
        saved_queries = 0
        dashboards = 0
        looks = 0

    return HealthCheckResponse(
        status="ok",
        mode=mode,
        mode_details=settings.get_mode_description(),
        schemas_loaded=schemas_loaded,
        explores_loaded=explores_loaded,
        saved_queries=saved_queries,
        dashboards=dashboards,
        looks=looks,
    )
