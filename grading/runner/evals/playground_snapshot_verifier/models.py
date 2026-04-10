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
    """Result of comparing normalized file contents between two snapshots.

    Used by compute_normalized_diff, has_differences, and format_diff_for_prompt.
    """

    matches: list[str] = Field(default_factory=list)
    """File paths that match exactly after normalization."""

    mismatches: list[FileMismatch] = Field(default_factory=list)
    """Files with differing content, including preview snippets."""

    missing_in_agent: list[str] = Field(default_factory=list)
    """File paths present in golden but not in agent snapshot."""

    extra_in_agent: list[str] = Field(default_factory=list)
    """File paths present in agent but not in golden snapshot."""


class ExtractionResult(BaseModel):
    """Result of file extraction with error tracking.

    Tracks normalized files, skipped binary files, and any errors
    encountered during extraction without failing the entire operation.
    """

    normalized_files: dict[str, str] = Field(default_factory=dict)
    skipped_binary: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    had_fatal_error: bool = False


class PlaygroundSnapshotJudgeResponse(JudgeResponse):
    """Response schema for playground snapshot LLM judge output.

    Inherits result (int) and reason (str) from JudgeResponse.
    """

    pass
