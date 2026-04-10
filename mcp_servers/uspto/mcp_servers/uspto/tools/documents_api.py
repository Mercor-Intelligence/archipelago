"""REST bridge compatible wrapper for documents tool."""

import asyncio
import os
import sys
from pathlib import Path

# Ensure we can import from the server directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from models import (  # noqa: E402
    DocumentsListResponse,
    GetDownloadUrlRequest,
    GetDownloadUrlResponse,
    ListDocumentsRequest,
)
from utils.decorators import make_async_background  # noqa: E402


async def _ensure_db():
    """Initialize the database if not already initialized."""
    from mcp_servers.uspto.db.session import init_db

    db_path = os.environ.get("USPTO_DB_PATH", "temp")
    await init_db(db_path)


@make_async_background
def documents_list(request: ListDocumentsRequest) -> DocumentsListResponse:
    """List prosecution documents for a given application."""
    from tools.documents import uspto_documents_list

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_ensure_db())
        return loop.run_until_complete(uspto_documents_list(**request.model_dump()))
    finally:
        loop.close()


@make_async_background
def documents_get_download_url(
    request: GetDownloadUrlRequest,
) -> GetDownloadUrlResponse:
    """Select preferred download URL from cached document metadata based on MIME type preference."""
    from tools.documents import uspto_documents_get_download_url

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_ensure_db())
        return loop.run_until_complete(uspto_documents_get_download_url(**request.model_dump()))
    finally:
        loop.close()
