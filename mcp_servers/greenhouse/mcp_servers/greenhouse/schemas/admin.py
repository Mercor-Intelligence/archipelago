"""Pydantic schemas for administrative Greenhouse MCP tools."""

from typing import Literal

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field


class GreenhouseResetStateInput(BaseModel):
    """Input parameters for the `greenhouse_reset_state` tool."""

    confirm: bool = Field(False, description="Must be true to proceed with resetting the database.")
    clear_users: bool = Field(
        True,
        description="Set to false to preserve system/persona users (and related mappings).",
    )


class GreenhouseResetStateResponse(BaseModel):
    """Response returned after successfully resetting the database."""

    status: Literal["reset"] = Field(
        "reset", description="Indicates that the reset operation completed."
    )
    tables_cleared: list[str] = Field(
        ..., description="Ordered list of tables that were truncated."
    )
    message: str = Field(..., description="Human-readable reset confirmation message.")
