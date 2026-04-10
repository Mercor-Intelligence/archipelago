"""Shared base schemas for Workday Help MCP."""

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field


class PaginationRequest(BaseModel):
    cursor: str | None = Field(None, description="Opaque pagination cursor")
    limit: int = Field(50, ge=1, le=200, description="Page size (max 200)")


class PaginationResponse(BaseModel):
    next_cursor: str | None = Field(None, description="Cursor for next page")
    has_more: bool = Field(..., description="Whether more results exist")
    limit: int = Field(..., description="Page size used")


class ErrorResponse(BaseModel):
    error: dict[str, object]
