"""
Unified types for file extraction service.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class ImageMetadata(BaseModel):
    """Metadata for an image extracted from a document"""

    url: str = Field(description="URL to access the image")
    placeholder: str = Field(
        description="Placeholder text in the extracted content (e.g., '[IMAGE_1]')"
    )
    type: str = Field(
        default="Figure", description="Type of image (Figure, Chart, etc.)"
    )
    caption: str | None = Field(
        default=None, description="Caption or description of the image"
    )
    page_number: int | None = Field(
        default=None, description="Page number where image appears"
    )


class SubArtifact(BaseModel):
    """
    Represents a sub-artifact within a multi-part document.

    For presentations: individual slides
    For spreadsheets: individual sheets/tabs
    For PDFs: individual pages
    """

    index: int = Field(
        description="0-based index of the sub-artifact (slide/sheet/page number - 1)"
    )
    type: Literal["slide", "sheet", "page"] = Field(description="Type of sub-artifact")
    title: str | None = Field(
        default=None, description="Title of the slide/sheet (if available)"
    )
    content: str = Field(description="Text content of this sub-artifact")
    images: list[ImageMetadata] = Field(
        default_factory=list, description="Images within this specific sub-artifact"
    )


class ExtractedContent(BaseModel):
    """
    Result from extracting content from a document.

    This is the unified model used across all file extraction methods.

    For multi-part documents (presentations, spreadsheets), the sub_artifacts field
    contains structured data for each slide/sheet/page. The text field contains
    the concatenated content for backward compatibility.
    """

    text: str = Field(
        description="Extracted text content, may contain image placeholders like [IMAGE_1]"
    )
    images: list[ImageMetadata] = Field(
        default_factory=list,
        description="List of images found in the document with metadata",
    )
    extraction_method: str = Field(
        default="unknown",
        description="Name of the extraction method used (e.g., 'reducto', 'pypdf', etc.)",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata about the extraction",
    )
    sub_artifacts: list[SubArtifact] = Field(
        default_factory=list,
        description="For multi-part documents: individual slides/sheets/pages with their content",
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
        """Check if this document has sub-artifacts (slides/sheets/pages)."""
        return len(self.sub_artifacts) > 0

    @property
    def sub_artifact_count(self) -> int:
        """Get the number of sub-artifacts."""
        return len(self.sub_artifacts)
