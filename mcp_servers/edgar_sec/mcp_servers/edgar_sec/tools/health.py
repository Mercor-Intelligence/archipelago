"""Health check tool for EDGAR SEC MCP server."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import EDGAR_OFFLINE_DATA_DIR, EDGAR_OFFLINE_MODE
from mcp_schema import OutputBaseModel
from pydantic import Field


class HealthCheckOutput(OutputBaseModel):
    """Health check response."""

    status: str = Field(description="Server status: 'healthy' or 'unhealthy'")
    mode: str = Field(description="Data mode: 'online' or 'offline'")
    data_dir: str | None = Field(
        description="Path to offline data directory (only in offline mode)"
    )
    companies_available: int | None = Field(
        description="Number of companies available in offline data (only in offline mode)"
    )
    message: str = Field(description="Human-readable status message")


async def health_check() -> HealthCheckOutput:
    """Return server readiness and data-mode details for connectivity checks."""
    if not EDGAR_OFFLINE_MODE:
        return HealthCheckOutput(
            status="healthy",
            mode="online",
            data_dir=None,
            companies_available=None,
            message="Running in ONLINE mode. Fetching data from SEC API (data.sec.gov)",
        )

    # Check offline data availability
    data_dir = Path(EDGAR_OFFLINE_DATA_DIR)
    companies_count = None
    status = "healthy"
    message_parts = ["Running in OFFLINE mode"]

    if data_dir.exists():
        # Count available companies
        submissions_dir = data_dir / "submissions"
        companyfacts_dir = data_dir / "companyfacts"

        if submissions_dir.exists():
            submissions_count = len(list(submissions_dir.glob("CIK*.json")))
            message_parts.append(f"{submissions_count} submissions files")

        if companyfacts_dir.exists():
            facts_count = len(list(companyfacts_dir.glob("CIK*.json")))
            companies_count = facts_count
            message_parts.append(f"{facts_count} company facts files")
    else:
        status = "unhealthy"
        message_parts.append(f"Data directory not found: {data_dir}")

    return HealthCheckOutput(
        status=status,
        mode="offline",
        data_dir=str(data_dir),
        companies_available=companies_count,
        message=". ".join(message_parts),
    )
