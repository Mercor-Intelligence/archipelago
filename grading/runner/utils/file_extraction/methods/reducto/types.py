"""
Pydantic models for Reducto client responses.
"""

from typing import Any

from pydantic import BaseModel, Field


class ReductoExtractedContent(BaseModel):
    """
    Result from extracting content from a document via Reducto.

    This is the raw Reducto-specific format before conversion to unified ExtractedContent.
    """

    text: str = Field(
        description="Extracted text content, may contain image placeholders"
    )
    images: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Image metadata with placeholders, URLs, and other info",
    )
    sub_artifacts: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Structured sub-artifacts (slides/sheets/pages) with their content",
    )

    @property
    def has_images(self) -> bool:
        """Check if any images were extracted."""
        return len(self.images) > 0

    @property
    def image_count(self) -> int:
        """Get the number of extracted images."""
        return len(self.images)

    @property
    def has_sub_artifacts(self) -> bool:
        """Check if sub-artifacts were extracted."""
        return len(self.sub_artifacts) > 0
