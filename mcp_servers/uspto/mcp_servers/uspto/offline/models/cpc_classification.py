"""CPC Classification model for USPTO offline mode."""

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field


class CPCClassification(BaseModel):
    """Represents a CPC (Cooperative Patent Classification) code."""

    id: int | None = Field(default=None, description="Database ID (auto-generated)")
    patent_id: int | None = Field(default=None, description="Foreign key to patents table")
    is_main: bool = Field(default=False, description="Main vs further classification")
    section: str = Field(..., description="CPC section (e.g., 'A')")
    class_: str = Field(..., alias="class", description="CPC class (e.g., '01')")
    subclass: str = Field(..., description="CPC subclass (e.g., 'B')")
    main_group: str = Field(..., description="Main group (e.g., '59')")
    subgroup: str = Field(..., description="Subgroup (e.g., '066')")
    full_code: str | None = Field(
        default=None, description="Generated full code (e.g., 'A01B 59/066')"
    )

    class Config:
        """Pydantic config."""

        from_attributes = True
        populate_by_name = True
