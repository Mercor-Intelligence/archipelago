"""Ingestion Log model for USPTO offline mode."""

from datetime import datetime
from enum import Enum

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field


class IngestionStatus(str, Enum):
    """Status of data ingestion."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class IngestionLog(BaseModel):
    """Tracks data ingestion operations."""

    id: int | None = Field(default=None, description="Database ID (auto-generated)")
    file_name: str = Field(..., description="Name of ingested file")
    file_path: str | None = Field(default=None, description="Full path to source file")
    file_size_bytes: int | None = Field(default=None, description="File size in bytes")
    format: str | None = Field(default=None, description="File format (xml, json)")
    data_type: str | None = Field(default=None, description="Data type (patent, trademark)")
    records_processed: int = Field(default=0, description="Total records processed")
    records_inserted: int = Field(default=0, description="Records successfully inserted")
    records_updated: int = Field(default=0, description="Records updated")
    records_skipped: int = Field(default=0, description="Records skipped")
    parse_errors: int = Field(default=0, description="Parsing errors encountered")
    validation_errors: int = Field(default=0, description="Validation errors")
    database_errors: int = Field(default=0, description="Database errors")
    started_at: datetime | None = Field(default=None, description="Ingestion start time")
    completed_at: datetime | None = Field(default=None, description="Ingestion completion time")
    duration_seconds: int | None = Field(default=None, description="Total duration in seconds")
    status: IngestionStatus = Field(..., description="Ingestion status")
    error_message: str | None = Field(default=None, description="Error message if failed")
    checkpoint_file: str | None = Field(
        default=None, description="Path to checkpoint file for resumption"
    )

    class Config:
        """Pydantic config."""

        from_attributes = True
        use_enum_values = True
