"""Lookup list schemas for Greenhouse MCP Server.

Simple lookup tables for populating dropdown selections:
- Departments
- Offices
- Sources
- Rejection Reasons
"""

from mcp_schema import GeminiBaseModel as BaseModel
from schemas.users import DepartmentOutput, OfficeOutput

# =============================================================================
# Input Models
# =============================================================================


class ListDepartmentsInput(BaseModel):
    """Input for listing all departments (no parameters required)."""

    pass


class ListOfficesInput(BaseModel):
    """Input for listing all offices (no parameters required)."""

    pass


class ListSourcesInput(BaseModel):
    """Input for listing all candidate sources (no parameters required)."""

    pass


class ListRejectionReasonsInput(BaseModel):
    """Input for listing all rejection reasons (no parameters required)."""

    pass


# =============================================================================
# Output Models
# =============================================================================


class SourceOutput(BaseModel):
    """Source info for dropdown selection."""

    id: int
    name: str
    type_id: int | None = None


class RejectionReasonOutput(BaseModel):
    """Rejection reason info for dropdown selection."""

    id: int
    name: str
    type_id: int | None = None
    type_name: str | None = None


class ListDepartmentsOutput(BaseModel):
    """Output for listing all departments."""

    departments: list[DepartmentOutput]


class ListOfficesOutput(BaseModel):
    """Output for listing all offices."""

    offices: list[OfficeOutput]


class ListSourcesOutput(BaseModel):
    """Output for listing all candidate sources."""

    sources: list[SourceOutput]


class ListRejectionReasonsOutput(BaseModel):
    """Output for listing all rejection reasons."""

    rejection_reasons: list[RejectionReasonOutput]
