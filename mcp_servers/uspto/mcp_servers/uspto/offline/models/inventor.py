"""Inventor model for USPTO offline mode."""

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field


class Inventor(BaseModel):
    """Represents an inventor on a patent application or grant."""

    id: int | None = Field(default=None, description="Database ID (auto-generated)")
    patent_id: int | None = Field(default=None, description="Foreign key to patents table")
    sequence: int | None = Field(default=None, description="Order of inventor in patent")
    first_name: str | None = Field(default=None, description="Inventor first name")
    last_name: str | None = Field(default=None, description="Inventor last name")
    full_name: str | None = Field(default=None, description="Combined full name")
    city: str | None = Field(default=None, description="City of residence")
    state: str | None = Field(default=None, description="State/province")
    country: str | None = Field(default=None, description="Country code")

    class Config:
        """Pydantic config."""

        from_attributes = True
