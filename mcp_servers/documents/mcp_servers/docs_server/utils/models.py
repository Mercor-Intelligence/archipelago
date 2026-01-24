"""Pydantic models for docs MCP server."""

from collections.abc import Sequence

from pydantic import BaseModel, Field

# ============================================================================
# Get Document Overview Models
# ============================================================================


class HeadingStructure(BaseModel):
    """A heading element in the document structure."""

    type: str = Field(default="heading", description="Element type")
    level: int = Field(..., description="Heading level (1-9)")
    text: str = Field(..., description="Heading text content")
    annotation: str = Field(..., description="Stable identifier for reference")
    style: str | None = Field(None, description="Style name applied")

    def __str__(self) -> str:
        indent = "  " * (self.level - 1)
        return f"{indent}[{self.annotation}] {self.style or 'Heading'} {self.level}: {self.text}"


class DocumentOverviewMetadata(BaseModel):
    """Metadata for document overview."""

    heading_count: int = Field(..., description="Number of heading elements")
    section_count: int = Field(..., description="Number of sections")


class GetDocumentOverviewResponse(BaseModel):
    """Response model for get_document_overview."""

    filepath: str = Field(..., description="The processed document path")
    extension: str = Field(..., description="File extension")
    status: str = Field(..., description="Operation status")
    metadata: DocumentOverviewMetadata = Field(..., description="Document statistics")
    structure: list[HeadingStructure] = Field(
        default_factory=list, description="List of headings in document order"
    )

    def __str__(self) -> str:
        lines = [
            f"Document Overview: {self.filepath}",
            "=" * 80,
            "",
            f"Status: {self.status}",
            f"Extension: {self.extension}",
            f"Total Headings: {self.metadata.heading_count}",
            f"Sections: {self.metadata.section_count}",
            "",
            "=" * 80,
            "",
        ]

        if self.structure:
            lines.append("Document Structure:")
            lines.append("-" * 80)
            for heading in self.structure:
                lines.append(str(heading))
            lines.append("")
        else:
            lines.append("No headings found in document.")
            lines.append("")

        return "\n".join(lines)


# ============================================================================
# Read Document Content Models
# ============================================================================


class Run(BaseModel):
    """A text run with formatting."""

    id: str = Field(..., description="Unique run identifier")
    text: str = Field(..., description="Text content")
    bold: bool | None = Field(None, description="Bold formatting")
    italic: bool | None = Field(None, description="Italic formatting")
    underline: bool | None = Field(None, description="Underline formatting")
    strikethrough: bool | None = Field(None, description="Strikethrough formatting")
    style: str | None = Field(None, description="Style name")

    def __str__(self) -> str:
        formatting = []
        if self.bold:
            formatting.append("bold")
        if self.italic:
            formatting.append("italic")
        if self.underline:
            formatting.append("underline")
        if self.strikethrough:
            formatting.append("strikethrough")
        if self.style:
            formatting.append(f"style={self.style}")

        fmt_str = f" ({', '.join(formatting)})" if formatting else ""
        return f"[{self.id}]{fmt_str}: {self.text}"


class ImageRun(BaseModel):
    """An image embedded in the document."""

    id: str = Field(..., description="Unique run identifier")
    type: str = Field(default="image", description="Type marker for images")
    annotation: str = Field(..., description="Annotation key for image in memory cache")
    width: int | None = Field(
        None, description="Image width in EMUs (English Metric Units)"
    )
    height: int | None = Field(
        None, description="Image height in EMUs (English Metric Units)"
    )
    alt_text: str | None = Field(None, description="Alternative text for the image")

    def __str__(self) -> str:
        dims = (
            f" ({self.width}x{self.height} EMUs)" if self.width and self.height else ""
        )
        alt = f" alt='{self.alt_text}'" if self.alt_text else ""
        return f"[{self.id}] IMAGE{dims}{alt}: @{self.annotation}"


class Paragraph(BaseModel):
    """A paragraph with runs."""

    id: str = Field(..., description="Unique paragraph identifier")
    style: str | None = Field(None, description="Paragraph style")
    alignment: str | None = Field(None, description="Text alignment")
    runs: Sequence[Run | ImageRun] = Field(
        default_factory=list, description="Text runs and images"
    )

    def __str__(self) -> str:
        meta = []
        if self.style:
            meta.append(f"style={self.style}")
        if self.alignment:
            meta.append(f"align={self.alignment}")

        meta_str = f" ({', '.join(meta)})" if meta else ""
        lines = [f"Paragraph [{self.id}]{meta_str}:"]
        for run in self.runs:
            lines.append(f"  {run}")
        return "\n".join(lines)


class Cell(BaseModel):
    """A table cell with paragraphs."""

    id: str = Field(..., description="Unique cell identifier")
    paragraphs: list[Paragraph] = Field(
        default_factory=list, description="Cell paragraphs"
    )

    def __str__(self) -> str:
        lines = [f"Cell [{self.id}]:"]
        for para in self.paragraphs:
            para_lines = str(para).split("\n")
            for line in para_lines:
                lines.append(f"  {line}")
        return "\n".join(lines)


class TableRow(BaseModel):
    """A table row with cells."""

    cells: list[Cell] = Field(default_factory=list, description="Row cells")


class Table(BaseModel):
    """A table with rows."""

    id: str = Field(..., description="Unique table identifier")
    rows: list[TableRow] = Field(default_factory=list, description="Table rows")

    def __str__(self) -> str:
        lines = [f"Table [{self.id}]:"]
        for row_idx, row in enumerate(self.rows):
            lines.append(f"  Row {row_idx}:")
            for cell in row.cells:
                cell_lines = str(cell).split("\n")
                for line in cell_lines:
                    lines.append(f"    {line}")
        return "\n".join(lines)


class DocumentBody(BaseModel):
    """Document body content."""

    paragraphs: list[Paragraph] = Field(
        default_factory=list, description="Body paragraphs"
    )
    tables: list[Table] = Field(default_factory=list, description="Body tables")


class ReadDocumentContentMetadata(BaseModel):
    """Metadata for read document content."""

    num_paragraphs: int = Field(..., description="Count of body paragraphs")
    num_tables: int = Field(..., description="Count of body tables")
    num_sections: int = Field(..., description="Number of sections")
    total_runs: int = Field(..., description="Total runs across all parts")
    num_images: int = Field(default=0, description="Total images found in document")
    section_index: int | None = Field(
        None, description="Section index if pagination was used"
    )
    section_range: str | None = Field(None, description="Paragraph range description")


class ReadDocumentContentResponse(BaseModel):
    """Response model for read_document_content."""

    filepath: str = Field(..., description="The processed document path")
    extension: str = Field(..., description="File extension")
    status: str = Field(..., description="Operation status")
    metadata: ReadDocumentContentMetadata = Field(..., description="Document stats")
    body: DocumentBody = Field(..., description="Main document content")

    def __str__(self) -> str:
        lines = [
            f"Document Content: {self.filepath}",
            "=" * 80,
            "",
            f"Status: {self.status}",
            f"Extension: {self.extension}",
            f"Paragraphs: {self.metadata.num_paragraphs}",
            f"Tables: {self.metadata.num_tables}",
            f"Sections: {self.metadata.num_sections}",
            f"Total Runs: {self.metadata.total_runs}",
            f"Images: {self.metadata.num_images}",
        ]

        if self.metadata.section_index is not None:
            lines.append(f"Section Index: {self.metadata.section_index}")
            lines.append(f"Section Range: {self.metadata.section_range}")

        lines.extend(["", "=" * 80, ""])

        # Body paragraphs
        if self.body.paragraphs:
            lines.append("Paragraphs:")
            lines.append("-" * 80)
            for para in self.body.paragraphs:
                lines.append(str(para))
                lines.append("")

        # Tables
        if self.body.tables:
            lines.append("=" * 80)
            lines.append("Tables:")
            lines.append("-" * 80)
            for table in self.body.tables:
                lines.append(str(table))
                lines.append("")

        return "\n".join(lines)


# ============================================================================
# Add Content Text Models
# ============================================================================


class TargetInfo(BaseModel):
    """Information about the target element."""

    kind: str = Field(..., description="Target kind (run, paragraph, or cell)")
    identifier: str = Field(..., description="The identifier used")


class AddContentTextResponse(BaseModel):
    """Response model for add_content_text."""

    filepath: str = Field(..., description="Modified document path")
    status: str = Field(..., description="Operation status")
    target: TargetInfo = Field(..., description="Target information")
    position: str = Field(..., description="Effective position used")
    updated_preview: str | None = Field(None, description="Preview of updated text")

    def __str__(self) -> str:
        lines = [
            f"Added Content: {self.filepath}",
            "=" * 80,
            "",
            f"Status: {self.status}",
            f"Target: {self.target.kind} [{self.target.identifier}]",
            f"Position: {self.position}",
            "",
        ]

        if self.updated_preview:
            lines.append("Updated Text Preview:")
            lines.append("-" * 80)
            lines.append(self.updated_preview)
            lines.append("")

        return "\n".join(lines)


# ============================================================================
# Edit Content Text Models
# ============================================================================


class EditTargetInfo(BaseModel):
    """Information about the edit target."""

    kind: str = Field(..., description="Target kind")
    identifier: str = Field(..., description="The identifier used")
    old_text: str = Field(..., description="Previous text value")
    new_text: str = Field(..., description="New text value")


class EditContentTextResponse(BaseModel):
    """Response model for edit_content_text."""

    filepath: str = Field(..., description="Updated document path")
    status: str = Field(..., description="Operation status")
    target: EditTargetInfo = Field(..., description="Information about the change")

    def __str__(self) -> str:
        lines = [
            f"Edited Content: {self.filepath}",
            "=" * 80,
            "",
            f"Status: {self.status}",
            f"Target: {self.target.kind} [{self.target.identifier}]",
            "",
            "Change Summary:",
            "-" * 80,
            f"Old Text: {self.target.old_text}",
            "",
            f"New Text: {self.target.new_text}",
            "",
        ]

        return "\n".join(lines)


# ============================================================================
# Delete Content Text Models
# ============================================================================


class DeleteContentTextResponse(BaseModel):
    """Response model for delete_content_text."""

    filepath: str = Field(..., description="Modified document path")
    status: str = Field(..., description="Operation status")
    target: TargetInfo = Field(..., description="Target information")
    result: dict = Field(..., description="Summary of deletion performed")

    def __str__(self) -> str:
        lines = [
            f"Deleted Content: {self.filepath}",
            "=" * 80,
            "",
            f"Status: {self.status}",
            f"Target: {self.target.kind} [{self.target.identifier}]",
            f"Scope: {self.result.get('scope', 'N/A')}",
            "",
            "Deletion Summary:",
            "-" * 80,
        ]

        # Add specific details based on what was in result
        if "old_text" in self.result:
            lines.append(f"Deleted Text: {self.result['old_text']}")
        if "old_text_runs" in self.result:
            lines.append(f"Deleted Runs: {len(self.result['old_text_runs'])}")
            for idx, text in enumerate(self.result["old_text_runs"]):
                lines.append(f"  Run {idx}: {text}")
        if "old_paragraph_texts" in self.result:
            lines.append(
                f"Deleted Paragraphs: {len(self.result['old_paragraph_texts'])}"
            )
            for idx, text in enumerate(self.result["old_paragraph_texts"]):
                lines.append(f"  Paragraph {idx}: {text}")
        if self.result.get("removed"):
            lines.append("Element removed from document structure.")

        lines.append("")
        return "\n".join(lines)


# ============================================================================
# Apply Formatting Models
# ============================================================================


class ApplyFormattingResponse(BaseModel):
    """Response model for apply_formatting."""

    filepath: str = Field(..., description="Modified document path")
    status: str = Field(..., description="Operation status")
    target: TargetInfo = Field(..., description="Target information")
    applied: dict = Field(..., description="Summary of formatting changes applied")
    updated_runs_count: int = Field(..., description="Number of runs updated")

    def __str__(self) -> str:
        lines = [
            f"Applied Formatting: {self.filepath}",
            "=" * 80,
            "",
            f"Status: {self.status}",
            f"Target: {self.target.kind} [{self.target.identifier}]",
            f"Runs Updated: {self.updated_runs_count}",
            "",
            "Formatting Applied:",
            "-" * 80,
        ]

        for key, value in self.applied.items():
            lines.append(f"{key}: {value}")

        lines.append("")
        return "\n".join(lines)


# ============================================================================
# Read Image Models
# ============================================================================


class ReadImageResponse(BaseModel):
    """Response model for read_image."""

    file_path: str = Field(..., description="The document file path")
    annotation: str = Field(..., description="The annotation key for the image")
    status: str = Field(..., description="Operation status")
    mime_type: str = Field(..., description="MIME type of the image")
    base64_data: str = Field(..., description="Base64 encoded image data")

    def __str__(self) -> str:
        lines = [
            f"Image from {self.file_path}",
            f"Annotation: @{self.annotation}",
            "=" * 80,
            "",
            f"Status: {self.status}",
            f"MIME Type: {self.mime_type}",
            "",
            "Base64 Data:",
            "-" * 80,
            self.base64_data,
            "",
        ]

        return "\n".join(lines)


# ============================================================================
# Header/Footer Models
# ============================================================================


class HeaderFooterContent(BaseModel):
    """Content from a header or footer."""

    paragraphs: list[Paragraph] = Field(
        default_factory=list, description="Paragraphs in the header/footer"
    )
    tables: list[Table] = Field(
        default_factory=list, description="Tables in the header/footer"
    )


class HeaderFooterReadResponse(BaseModel):
    """Response model for header_footer read action."""

    filepath: str = Field(..., description="The processed document path")
    status: str = Field(..., description="Operation status")
    area: str = Field(..., description="Area: 'header' or 'footer'")
    section_index: int = Field(..., description="Section index")
    is_linked_to_previous: bool = Field(
        ..., description="Whether linked to previous section"
    )
    content: HeaderFooterContent = Field(..., description="Header/footer content")

    def __str__(self) -> str:
        lines = [
            f"Header/Footer Content: {self.filepath}",
            "=" * 80,
            "",
            f"Status: {self.status}",
            f"Area: {self.area}",
            f"Section Index: {self.section_index}",
            f"Linked to Previous: {self.is_linked_to_previous}",
            f"Paragraphs: {len(self.content.paragraphs)}",
            f"Tables: {len(self.content.tables)}",
            "",
            "=" * 80,
            "",
        ]

        if self.content.paragraphs:
            lines.append("Paragraphs:")
            lines.append("-" * 80)
            for para in self.content.paragraphs:
                lines.append(str(para))
                lines.append("")

        if self.content.tables:
            lines.append("Tables:")
            lines.append("-" * 80)
            for table in self.content.tables:
                lines.append(str(table))
                lines.append("")

        return "\n".join(lines)


class HeaderFooterSetResponse(BaseModel):
    """Response model for header_footer set action."""

    filepath: str = Field(..., description="The modified document path")
    status: str = Field(..., description="Operation status")
    area: str = Field(..., description="Area: 'header' or 'footer'")
    section_index: int = Field(..., description="Section index")
    blocks_added: int = Field(..., description="Number of content blocks added")

    def __str__(self) -> str:
        lines = [
            f"Set Header/Footer: {self.filepath}",
            "=" * 80,
            "",
            f"Status: {self.status}",
            f"Area: {self.area}",
            f"Section Index: {self.section_index}",
            f"Blocks Added: {self.blocks_added}",
            "",
        ]

        return "\n".join(lines)


class HeaderFooterClearResponse(BaseModel):
    """Response model for header_footer clear action."""

    filepath: str = Field(..., description="The modified document path")
    status: str = Field(..., description="Operation status")
    area: str = Field(..., description="Area: 'header' or 'footer'")
    section_index: int = Field(..., description="Section index")
    paragraphs_removed: int = Field(..., description="Number of paragraphs removed")
    tables_removed: int = Field(..., description="Number of tables removed")

    def __str__(self) -> str:
        lines = [
            f"Cleared Header/Footer: {self.filepath}",
            "=" * 80,
            "",
            f"Status: {self.status}",
            f"Area: {self.area}",
            f"Section Index: {self.section_index}",
            f"Paragraphs Removed: {self.paragraphs_removed}",
            f"Tables Removed: {self.tables_removed}",
            "",
        ]

        return "\n".join(lines)


class HeaderFooterLinkResponse(BaseModel):
    """Response model for header_footer link action."""

    filepath: str = Field(..., description="The modified document path")
    status: str = Field(..., description="Operation status")
    area: str = Field(..., description="Area: 'header' or 'footer'")
    section_index: int = Field(..., description="Section index")
    was_linked: bool = Field(..., description="Previous linked state")
    now_linked: bool = Field(..., description="New linked state")

    def __str__(self) -> str:
        lines = [
            f"Link Header/Footer: {self.filepath}",
            "=" * 80,
            "",
            f"Status: {self.status}",
            f"Area: {self.area}",
            f"Section Index: {self.section_index}",
            f"Was Linked: {self.was_linked}",
            f"Now Linked: {self.now_linked}",
            "",
        ]

        return "\n".join(lines)


# ============================================================================
# Page Margins Models
# ============================================================================


class PageMarginsReadResponse(BaseModel):
    """Response model for page_margins read action."""

    filepath: str = Field(..., description="The processed document path")
    status: str = Field(..., description="Operation status")
    section_index: int = Field(..., description="Section index")
    top: float | None = Field(..., description="Top margin in inches")
    bottom: float | None = Field(..., description="Bottom margin in inches")
    left: float | None = Field(..., description="Left margin in inches")
    right: float | None = Field(..., description="Right margin in inches")

    def __str__(self) -> str:
        lines = [
            f"Page Margins: {self.filepath}",
            "=" * 80,
            "",
            f"Status: {self.status}",
            f"Section Index: {self.section_index}",
            "",
            "Margins (inches):",
            "-" * 80,
            f"  Top: {self.top}",
            f"  Bottom: {self.bottom}",
            f"  Left: {self.left}",
            f"  Right: {self.right}",
            "",
        ]

        return "\n".join(lines)


class PageMarginsSetResponse(BaseModel):
    """Response model for page_margins set action."""

    filepath: str = Field(..., description="The modified document path")
    status: str = Field(..., description="Operation status")
    section_index: int = Field(..., description="Section index")
    old_top: float | None = Field(..., description="Previous top margin in inches")
    old_bottom: float | None = Field(
        ..., description="Previous bottom margin in inches"
    )
    old_left: float | None = Field(..., description="Previous left margin in inches")
    old_right: float | None = Field(..., description="Previous right margin in inches")
    new_top: float | None = Field(..., description="New top margin in inches")
    new_bottom: float | None = Field(..., description="New bottom margin in inches")
    new_left: float | None = Field(..., description="New left margin in inches")
    new_right: float | None = Field(..., description="New right margin in inches")

    def __str__(self) -> str:
        lines = [
            f"Set Page Margins: {self.filepath}",
            "=" * 80,
            "",
            f"Status: {self.status}",
            f"Section Index: {self.section_index}",
            "",
            "Margins Changed (inches):",
            "-" * 80,
            f"  Top: {self.old_top} -> {self.new_top}",
            f"  Bottom: {self.old_bottom} -> {self.new_bottom}",
            f"  Left: {self.old_left} -> {self.new_left}",
            f"  Right: {self.old_right} -> {self.new_right}",
            "",
        ]

        return "\n".join(lines)


# ============================================================================
# Page Orientation Models
# ============================================================================


class PageOrientationReadResponse(BaseModel):
    """Response model for page_orientation read action."""

    filepath: str = Field(..., description="The processed document path")
    status: str = Field(..., description="Operation status")
    section_index: int = Field(..., description="Section index")
    orientation: str = Field(
        ..., description="Page orientation: 'portrait' or 'landscape'"
    )
    page_width: float | None = Field(..., description="Page width in inches")
    page_height: float | None = Field(..., description="Page height in inches")

    def __str__(self) -> str:
        lines = [
            f"Page Orientation: {self.filepath}",
            "=" * 80,
            "",
            f"Status: {self.status}",
            f"Section Index: {self.section_index}",
            f"Orientation: {self.orientation}",
            f"Page Size: {self.page_width} x {self.page_height} inches",
            "",
        ]

        return "\n".join(lines)


class PageOrientationSetResponse(BaseModel):
    """Response model for page_orientation set action."""

    filepath: str = Field(..., description="The modified document path")
    status: str = Field(..., description="Operation status")
    section_index: int = Field(..., description="Section index")
    old_orientation: str = Field(..., description="Previous orientation")
    new_orientation: str = Field(..., description="New orientation")

    def __str__(self) -> str:
        lines = [
            f"Set Page Orientation: {self.filepath}",
            "=" * 80,
            "",
            f"Status: {self.status}",
            f"Section Index: {self.section_index}",
            f"Orientation: {self.old_orientation} -> {self.new_orientation}",
            "",
        ]

        return "\n".join(lines)


# ============================================================================
# Comments Models
# ============================================================================


class CommentInfo(BaseModel):
    """Information about a single comment."""

    id: int = Field(..., description="Comment ID")
    author: str = Field(..., description="Comment author")
    text: str = Field(..., description="Comment text")
    date: str | None = Field(None, description="Comment date")

    def __str__(self) -> str:
        date_str = f" ({self.date})" if self.date else ""
        return f"[{self.id}] {self.author}{date_str}: {self.text}"


class CommentsReadResponse(BaseModel):
    """Response model for comments read action."""

    filepath: str = Field(..., description="The processed document path")
    status: str = Field(..., description="Operation status")
    comment_count: int = Field(..., description="Number of comments")
    comments: list[CommentInfo] = Field(
        default_factory=list, description="List of comments"
    )

    def __str__(self) -> str:
        lines = [
            f"Comments: {self.filepath}",
            "=" * 80,
            "",
            f"Status: {self.status}",
            f"Comment Count: {self.comment_count}",
            "",
        ]

        if self.comments:
            lines.append("Comments:")
            lines.append("-" * 80)
            for comment in self.comments:
                lines.append(str(comment))
            lines.append("")
        else:
            lines.append("No comments in document.")
            lines.append("")

        return "\n".join(lines)


class CommentsAddResponse(BaseModel):
    """Response model for comments add action."""

    filepath: str = Field(..., description="The modified document path")
    status: str = Field(..., description="Operation status")
    comment_id: int = Field(..., description="New comment ID")
    identifier: str = Field(..., description="Target identifier")
    text: str = Field(..., description="Comment text")
    author: str = Field(..., description="Comment author")

    def __str__(self) -> str:
        lines = [
            f"Added Comment: {self.filepath}",
            "=" * 80,
            "",
            f"Status: {self.status}",
            f"Comment ID: {self.comment_id}",
            f"Target: {self.identifier}",
            f"Author: {self.author}",
            f"Text: {self.text}",
            "",
        ]

        return "\n".join(lines)


class CommentsDeleteResponse(BaseModel):
    """Response model for comments delete action."""

    filepath: str = Field(..., description="The modified document path")
    status: str = Field(..., description="Operation status")
    comment_id: int = Field(..., description="Deleted comment ID")
    deleted_author: str = Field(..., description="Deleted comment author")
    deleted_text: str = Field(..., description="Deleted comment text")

    def __str__(self) -> str:
        lines = [
            f"Deleted Comment: {self.filepath}",
            "=" * 80,
            "",
            f"Status: {self.status}",
            f"Comment ID: {self.comment_id}",
            f"Author: {self.deleted_author}",
            f"Text: {self.deleted_text}",
            "",
        ]

        return "\n".join(lines)
