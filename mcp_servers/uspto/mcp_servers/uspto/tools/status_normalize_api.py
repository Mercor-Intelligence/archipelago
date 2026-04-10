"""REST bridge compatible wrapper for status normalization tool."""

import asyncio
import os
import sys
from pathlib import Path

# Ensure we can import from the server directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from models import StatusNormalizeRequest, StatusNormalizeResponse  # noqa: E402
from utils.decorators import make_async_background  # noqa: E402


async def _ensure_db():
    """Initialize the database if not already initialized."""
    from db.session import init_db

    db_path = os.environ.get("USPTO_DB_PATH", "temp")
    await init_db(db_path)


@make_async_background
def status_normalize(
    request: StatusNormalizeRequest,
) -> StatusNormalizeResponse:
    """Normalize snapshot status codes using the USPTO reference table."""
    from tools.status_normalize import uspto_status_normalize

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_ensure_db())
        return loop.run_until_complete(uspto_status_normalize(**request.model_dump()))
    finally:
        loop.close()
