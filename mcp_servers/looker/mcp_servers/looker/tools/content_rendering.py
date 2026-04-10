"""Content rendering tools."""

import sys
from pathlib import Path

# Add server to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastmcp.utilities.types import File
from models import (
    RunDashboardPdfRequest,
    RunDashboardPdfResponse,
    RunLookPdfRequest,
    RunLookPdfResponse,
)
from repository_factory import create_repository


async def run_look_pdf(request: RunLookPdfRequest) -> File:
    """Execute a Look and return results as a PDF document."""
    import base64
    import os

    from loguru import logger

    repo = create_repository(RunLookPdfRequest, RunLookPdfResponse)
    result = await repo.get(request)

    # Determine file output location
    # Production uses APP_FS_ROOT env var
    # Local development uses ./looker_pdfs relative to current directory
    state_location = os.getenv("APP_FS_ROOT", "./looker_pdfs")

    # Create directory if it doesn't exist
    os.makedirs(state_location, exist_ok=True)

    # Write PDF to state location
    file_path = os.path.join(state_location, f"look_{request.look_id}.pdf")
    pdf_bytes = base64.b64decode(result.image_data)

    with open(file_path, "wb") as f:
        f.write(pdf_bytes)

    logger.info(f"PDF saved to {file_path} ({len(pdf_bytes)} bytes)")

    # Return FastMCP File type for PDF (application/pdf MIME type)
    return File(data=pdf_bytes, format="pdf")


async def run_dashboard_pdf(request: RunDashboardPdfRequest) -> File:
    """Execute all dashboard tiles and return as a PDF document."""
    import base64
    import os

    from loguru import logger

    repo = create_repository(RunDashboardPdfRequest, RunDashboardPdfResponse)
    result = await repo.get(request)

    # Determine file output location
    # Production uses APP_FS_ROOT env var
    # Local development uses ./looker_pdfs relative to current directory
    state_location = os.getenv("APP_FS_ROOT", "./looker_pdfs")

    # Create directory if it doesn't exist
    os.makedirs(state_location, exist_ok=True)

    # Write PDF to state location
    file_path = os.path.join(state_location, f"dashboard_{request.dashboard_id}.pdf")
    pdf_bytes = base64.b64decode(result.image_data)

    with open(file_path, "wb") as f:
        f.write(pdf_bytes)

    logger.info(f"PDF saved to {file_path} ({len(pdf_bytes)} bytes)")

    # Return FastMCP File type for PDF (application/pdf MIME type)
    return File(data=pdf_bytes, format="pdf")
