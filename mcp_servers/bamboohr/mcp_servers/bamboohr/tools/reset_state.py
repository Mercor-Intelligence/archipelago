"""Reset database state tool for BambooHR MCP server.

Implements:
- reset_state: Reset in-memory database to empty state (#utility)

This is a utility tool for testing, not part of the real BambooHR API.
HR Admin only - requires confirm=true to prevent accidental resets.
"""

from datetime import UTC, datetime

from db import reset_db
from loguru import logger
from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field, field_validator


class ResetStateInput(BaseModel):
    """Input model for reset_state tool."""

    confirm: bool = Field(
        ...,
        description="Must be true to confirm database reset",
    )

    @field_validator("confirm", mode="before")
    @classmethod
    def validate_confirm_is_bool(cls, v):
        """Accept boolean true or string 'true' (case-insensitive)."""
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            if v.lower() == "true":
                return True
            if v.lower() == "false":
                return False
            raise ValueError(f"confirm must be 'true' or 'false', got '{v}'")
        raise ValueError("confirm must be a boolean (true or false), not a number")


class ResetStateOutput(BaseModel):
    """Output model for reset_state tool."""

    success: bool = Field(..., description="Whether reset was successful")
    message: str = Field(..., description="Success message")
    timestamp: str = Field(..., description="ISO 8601 timestamp of reset")


async def reset_state(request: ResetStateInput) -> ResetStateOutput:
    """Reset database to empty state for testing purposes."""
    # Validate confirm is true
    if not request.confirm:
        raise ValueError("Must set confirm=true to reset database")

    logger.info("Resetting database state")

    # Use reset_db which properly handles initialization tracking
    await reset_db()

    logger.info("Database reset successfully")

    # Format timestamp with Z suffix (not +00:00) per spec
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    return ResetStateOutput(
        success=True,
        message="Database reset successfully",
        timestamp=timestamp,
    )
