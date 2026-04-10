"""Assignee model for USPTO offline mode."""

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field


class Assignee(BaseModel):
    """Represents an assignee (organization or person) on a patent."""

    id: int | None = Field(default=None, description="Database ID (auto-generated)")
    patent_id: int | None = Field(default=None, description="Foreign key to patents table")
    name: str = Field(..., description="Organization or person name")
    role: str | None = Field(default=None, description="Assignee role code (e.g., 02, 03, 05)")
    city: str | None = Field(default=None, description="City")
    state: str | None = Field(default=None, description="State/province")
    country: str | None = Field(default=None, description="Country code")

    class Config:
        """Pydantic config."""

        from_attributes = True
