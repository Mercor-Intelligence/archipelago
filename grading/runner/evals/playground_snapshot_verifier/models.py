"""Data models for playground snapshot verifier."""

from pydantic import BaseModel, Field

from runner.utils.llm_judge import JudgeResponse


class FileMismatch(BaseModel):
    """A single file content mismatch between golden and agent snapshots."""

    file_path: str
    golden_preview: str
    agent_preview: str
    # Full content for DB files (used for accurate table classification)
    # Only populated for database files to avoid memory bloat
    golden_full: str | None = None
    agent_full: str | None = None


class NormalizedDiff(BaseModel):
    """Result of comparing normalized file contents between two snapshots."""

    matches: list[str] = Field(default_factory=list)
    mismatches: list[FileMismatch] = Field(default_factory=list)
    missing_in_agent: list[str] = Field(default_factory=list)
    extra_in_agent: list[str] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    """Result of file extraction with error tracking."""

    normalized_files: dict[str, str] = Field(default_factory=dict)
    skipped_binary: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    had_fatal_error: bool = False


class TableVerdict(BaseModel):
    """Per-table classification result from the LLM judge."""

    classification: str  # "noise" | "task_incomplete" | "unwanted_mutation"
    reason: str


class PlaygroundSnapshotJudgeResponse(JudgeResponse):
    """Response schema for the aggregate LLM judge call.

    Inherits result (int) and reason (str) from JudgeResponse.
    table_verdicts is attached after aggregation.
    """

    table_verdicts: dict[str, TableVerdict] = {}
