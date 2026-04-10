"""Patent model for USPTO offline mode."""

from datetime import date, datetime
from enum import Enum

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field


class DocumentType(str, Enum):
    """Type of patent document."""

    APPLICATION = "application"
    GRANT = "grant"


class ApplicationType(str, Enum):
    """Type of patent application."""

    UTILITY = "utility"
    DESIGN = "design"
    PLANT = "plant"


class PatentRecord(BaseModel):
    """Represents a patent application or grant with all associated data."""

    # Primary identifiers
    id: int | None = Field(default=None, description="Database ID (auto-generated)")
    application_number: str = Field(..., description="USPTO application number")
    publication_number: str | None = Field(default=None, description="Publication number")
    patent_number: str | None = Field(default=None, description="Patent number (for grants)")

    # Document metadata
    kind_code: str | None = Field(default=None, description="Kind code (e.g., A1, B2, S1)")
    document_type: DocumentType = Field(..., description="Application or grant")
    application_type: ApplicationType | None = Field(
        default=None, description="Utility, design, or plant"
    )
    country: str = Field(default="US", description="Country code")

    # Dates
    filing_date: date = Field(..., description="Application filing date")
    publication_date: date | None = Field(default=None, description="Publication date")
    issue_date: date | None = Field(default=None, description="Issue date (for grants)")

    # Title and abstract
    title: str = Field(..., description="Invention title")
    abstract: str | None = Field(default=None, description="Abstract text")

    # Full text content
    description: str | None = Field(default=None, description="Full specification text")
    claims: str | None = Field(default=None, description="Claims text")

    # JSON fields for rarely-queried metadata
    applicants_json: str | None = Field(default=None, description="Applicant details (JSON)")
    attorneys_json: str | None = Field(default=None, description="Attorney/agent info (JSON)")
    ipc_codes_json: str | None = Field(default=None, description="IPC codes (JSON)")
    uspc_codes_json: str | None = Field(default=None, description="USPC codes (JSON)")
    locarno_classification: str | None = Field(
        default=None, description="Locarno classification (for design patents, JSON)"
    )
    npl_citations_json: str | None = Field(
        default=None, description="Non-patent literature citations (JSON)"
    )
    priority_claims_json: str | None = Field(
        default=None, description="Foreign priority claims (JSON)"
    )
    related_applications_json: str | None = Field(
        default=None, description="Related applications (JSON)"
    )

    # Grant-specific fields
    term_of_grant: int | None = Field(default=None, description="Term in years")
    number_of_claims: int | None = Field(default=None, description="Total claims count")
    number_of_figures: int | None = Field(default=None, description="Number of figures")
    number_of_drawing_sheets: int | None = Field(
        default=None, description="Number of drawing sheets"
    )

    # PCT data
    pct_filing_data_json: str | None = Field(
        default=None, description="PCT application data (JSON)"
    )

    # Metadata
    xml_file_name: str | None = Field(default=None, description="Source XML file")
    ingestion_date: datetime | None = Field(default=None, description="Date ingested into database")

    class Config:
        """Pydantic config."""

        from_attributes = True
        use_enum_values = True
