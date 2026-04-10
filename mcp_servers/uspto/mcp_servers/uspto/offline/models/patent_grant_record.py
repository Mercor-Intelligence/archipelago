"""Composite model for patent grant with all related data."""

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field

from mcp_servers.uspto.offline.models.assignee import Assignee
from mcp_servers.uspto.offline.models.citation import PatentCitation
from mcp_servers.uspto.offline.models.cpc_classification import CPCClassification
from mcp_servers.uspto.offline.models.examiner import Examiner
from mcp_servers.uspto.offline.models.inventor import Inventor
from mcp_servers.uspto.offline.models.patent import PatentRecord


class PatentGrantRecord(BaseModel):
    """Complete patent grant record with all related data.

    This composite model contains:
    - Main patent data (patents table)
    - Related inventors (inventors table)
    - Related assignees (assignees table)
    - CPC classifications (cpc_classifications table)
    - Patent citations (patent_citations table)
    - Examiners (examiners table)

    Used by the ingestion pipeline to extract all data from XML,
    then persister separates and inserts into appropriate tables.
    """

    # Main patent data
    patent: PatentRecord = Field(..., description="Main patent record")

    # Normalized related data
    inventors: list[Inventor] = Field(default_factory=list, description="Patent inventors")
    assignees: list[Assignee] = Field(default_factory=list, description="Patent assignees")
    cpc_classifications: list[CPCClassification] = Field(
        default_factory=list, description="CPC classification codes"
    )
    patent_citations: list[PatentCitation] = Field(
        default_factory=list, description="Citations to other patents"
    )
    examiners: list[Examiner] = Field(default_factory=list, description="Patent examiners")

    class Config:
        """Pydantic config."""

        from_attributes = True
