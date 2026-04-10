"""Examiner model for USPTO offline mode."""

from enum import Enum

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field


class ExaminerType(str, Enum):
    """Type of examiner."""

    PRIMARY = "primary"
    ASSISTANT = "assistant"


class Examiner(BaseModel):
    """Represents a USPTO examiner who reviewed the patent."""

    id: int | None = Field(default=None, description="Database ID (auto-generated)")
    patent_id: int | None = Field(default=None, description="Foreign key to patents table")
    examiner_type: ExaminerType = Field(..., description="Primary or assistant examiner")
    last_name: str | None = Field(default=None, description="Examiner last name")
    first_name: str | None = Field(default=None, description="Examiner first name")
    department: str | None = Field(default=None, description="Art unit or department")

    class Config:
        """Pydantic config."""

        from_attributes = True
        use_enum_values = True
