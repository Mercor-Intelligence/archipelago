"""Patent Citation model for USPTO offline mode."""

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field


class PatentCitation(BaseModel):
    """Represents a patent citation (prior art reference)."""

    id: int | None = Field(default=None, description="Database ID (auto-generated)")
    patent_id: int | None = Field(default=None, description="Foreign key to patents table")
    cited_patent_number: str | None = Field(default=None, description="Patent number being cited")
    cited_country: str | None = Field(default=None, description="Country of cited patent")
    cited_kind: str | None = Field(default=None, description="Kind code of cited patent")
    cited_date: str | None = Field(
        default=None, description="Date of cited patent (YYYYMMDD format, may have day=00)"
    )
    category: str | None = Field(
        default=None, description="Citation category (cited by examiner/applicant)"
    )

    class Config:
        """Pydantic config."""

        from_attributes = True
