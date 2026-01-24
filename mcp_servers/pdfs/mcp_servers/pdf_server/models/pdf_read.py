from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import ConfigDict, Field


class ImageInfo(BaseModel):
    """Information about an image extracted from a PDF page."""

    model_config = ConfigDict(extra="forbid")

    annotation: str = Field(..., description="Annotation key for image in cache")
    page_number: int = Field(..., description="Page number where image was found")
    image_index: int = Field(..., description="Index of image on the page (0-based)")
    width: float | None = Field(None, description="Image width in points")
    height: float | None = Field(None, description="Image height in points")

    def __str__(self) -> str:
        parts = [
            f"page={self.page_number}",
            f"index={self.image_index}",
            f"ref=@{self.annotation}",
        ]
        if self.width and self.height:
            parts.append(f"size={self.width}x{self.height}")
        return f"[image: {', '.join(parts)}]"


class StrikethroughInfo(BaseModel):
    """Information about strikethrough text annotation in a PDF."""

    model_config = ConfigDict(extra="forbid")

    page_number: int = Field(..., description="Page number (1-indexed)")
    contents: str | None = Field(None, description="Text content")
    rect: list[float] | None = Field(None, description="Bounding box [x0, y0, x1, y1]")

    def __str__(self) -> str:
        parts = [f"page={self.page_number}"]
        if self.contents:
            parts.append(f'text="{self.contents}"')
        if self.rect:
            parts.append(f"bbox={[round(r, 1) for r in self.rect]}")
        return f"[strikethrough: {', '.join(parts)}]"


class PdfPagesRead(BaseModel):
    """Result of reading pages from a PDF document."""

    model_config = ConfigDict(extra="forbid")

    content: dict[int, str] = Field(..., description="Page number -> text content")
    total_pages: int = Field(..., ge=0, description="Total pages in PDF")
    requested_pages: list[int] = Field(..., description="Pages requested")
    images: list[ImageInfo] = Field(
        default_factory=list, description="Extracted images"
    )
    strikethrough: list[StrikethroughInfo] = Field(
        default_factory=list, description="Strikethrough text"
    )
    errors: list[str] | None = Field(None, description="Errors encountered")

    def __str__(self) -> str:
        lines = []

        # Header
        lines.append(f"[pdf: pages={self.total_pages}, read={len(self.content)}]")

        # Summary counts
        if self.images:
            lines.append(f"[images: count={len(self.images)}]")
        if self.strikethrough:
            lines.append(f"[strikethrough: count={len(self.strikethrough)}]")

        # Content per page
        for page_num in sorted(self.content.keys()):
            lines.append(f"\n[page {page_num}]")
            lines.append(self.content[page_num])

            # Page images
            for img in (i for i in self.images if i.page_number == page_num):
                lines.append(str(img))

            # Page strikethrough
            for st in (s for s in self.strikethrough if s.page_number == page_num):
                lines.append(str(st))

        # Errors
        if self.errors:
            lines.append("\n[errors]")
            lines.extend(f"- {e}" for e in self.errors)

        return "\n".join(lines)


class ReadImageResponse(BaseModel):
    """Response model for read_image."""

    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(..., description="PDF file path")
    annotation: str = Field(..., description="Annotation key")
    status: str = Field(..., description="Operation status")
    mime_type: str = Field(..., description="MIME type")
    base64_data: str = Field(..., description="Base64 encoded data")

    def __str__(self) -> str:
        return "\n".join(
            [
                f"[image_response: file={self.file_path}, ref=@{self.annotation}, status={self.status}, type={self.mime_type}]",
                f"[data: length={len(self.base64_data)}]",
                self.base64_data,
            ]
        )
