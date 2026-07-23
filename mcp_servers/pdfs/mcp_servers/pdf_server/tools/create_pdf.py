import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

import pypdf
from pydantic import Field
from pydantic.dataclasses import dataclass
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import (
    ListFlowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from utils.decorators import make_async_background
from utils.path_utils import (
    PathTraversalError,
    resolve_under_root,
    virtual_path_from_physical,
)
from utils.schema import GeminiBaseModel

if TYPE_CHECKING:
    from collections.abc import Sequence

PAGE_SIZES = {
    "letter": LETTER,
    "a4": A4,
}


def _pdf_text_safe(text: str) -> str:
    """Escape user text for ReportLab Paragraph's XML-like markup parser."""
    return escape(text)


def _normalize_extracted_text(text: str) -> str:
    """Normalize whitespace for post-write text comparisons."""
    return re.sub(r"\s+", " ", text).strip()


def _normalize_extracted_line(text: str) -> str:
    """Normalize one extracted line and remove common list/table decoration."""
    normalized = _normalize_extracted_text(text)
    normalized = re.sub(r"^[\u2022\-\*]\s*", "", normalized)
    normalized = re.sub(r"^\d+[\.\)]\s*", "", normalized)
    return normalized


def _extract_normalized_lines(text: str) -> set[str]:
    return {
        line.casefold()
        for line in (_normalize_extracted_line(line) for line in text.splitlines())
        if line
    }


def _text_tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def _has_contiguous_token_sequence(haystack: list[str], needle: list[str]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    width = len(needle)
    return any(haystack[idx : idx + width] == needle for idx in range(len(haystack)))


def _fragment_rendered(
    fragment: str, extracted_lines: set[str], extracted_tokens: list[str]
) -> bool:
    normalized_fragment = _normalize_extracted_text(fragment).casefold()
    if normalized_fragment in extracted_lines:
        return True

    fragment_tokens = _text_tokens(fragment)
    if len(fragment_tokens) <= 1:
        return False
    return _has_contiguous_token_sequence(extracted_tokens, fragment_tokens)


def _expected_text_fragments(content: "Sequence[PdfContentBlock]") -> list[str]:
    """Collect non-empty user-supplied text that should be extractable."""
    fragments: list[str] = []
    for block in content:
        block_type = block.type
        if block_type in {"paragraph", "heading"} and block.text:
            fragments.append(block.text)
        elif block_type in {"bullet_list", "numbered_list"} and block.items:
            fragments.extend(item for item in block.items if item)
        elif block_type == "table" and block.rows:
            fragments.extend(cell for row in block.rows for cell in row if cell)
    return fragments


def _extract_pdf_text(path: str) -> str:
    reader = pypdf.PdfReader(Path(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _validate_rendered_text(
    path: str, content: "Sequence[PdfContentBlock]"
) -> str | None:
    """Return an error when non-empty input text did not render into the PDF."""
    expected_fragments = _expected_text_fragments(content)
    if not expected_fragments:
        return None

    try:
        raw_extracted_text = _extract_pdf_text(path)
        extracted_text = _normalize_extracted_text(raw_extracted_text)
    except Exception as exc:
        return f"Failed to validate created PDF text: {repr(exc)}"

    if not extracted_text:
        return "Created PDF has no extractable text despite non-empty input content"

    extracted_lines = _extract_normalized_lines(raw_extracted_text)
    extracted_tokens = _text_tokens(raw_extracted_text)
    missing_fragments = [
        fragment
        for fragment in expected_fragments
        if not _fragment_rendered(fragment, extracted_lines, extracted_tokens)
    ]
    if missing_fragments:
        preview = "; ".join(fragment[:80] for fragment in missing_fragments[:3])
        return f"Created PDF is missing rendered input text: {preview}"

    return None


@dataclass
class PdfMetadata:
    """Optional metadata applied to the generated PDF."""

    title: str | None = None
    subject: str | None = None
    author: str | None = None


@dataclass
class ParagraphBlock:
    type: str = "paragraph"
    text: str = ""
    bold: bool = False
    italic: bool = False


@dataclass
class HeadingBlock:
    type: str = "heading"
    text: str = ""
    level: int = 1


@dataclass
class BulletListBlock:
    type: str = "bullet_list"
    items: list[str] = Field(default_factory=list)


@dataclass
class NumberedListBlock:
    type: str = "numbered_list"
    items: list[str] = Field(default_factory=list)


@dataclass
class TableBlock:
    type: str = "table"
    rows: list[list[str]] = Field(default_factory=list)
    header: bool = True


@dataclass
class PageBreakBlock:
    type: str = "page_break"


@dataclass
class SpacerBlock:
    type: str = "spacer"
    height: float = 12  # points


class PdfContentBlock(GeminiBaseModel):
    """A single content block in a PDF document."""

    type: str = Field(
        ...,
        description="Block type: 'paragraph', 'heading', 'bullet_list', 'numbered_list', 'table', 'page_break', or 'spacer'",
    )
    text: str | None = Field(None, description="Text content (for paragraph, heading)")
    bold: bool | None = Field(None, description="Bold text (paragraph only)")
    italic: bool | None = Field(None, description="Italic text (paragraph only)")
    level: int | None = Field(None, description="Heading level 1-4 (heading only)")
    items: list[str] | None = Field(
        None, description="List items (bullet_list, numbered_list)"
    )
    rows: list[list[str]] | None = Field(
        None, description="2D array of cell values (table only)"
    )
    header: bool | None = Field(
        None, description="Bold the first row as header (table only)"
    )
    height: float | None = Field(None, description="Height in points (spacer only)")


class PdfMetadataInput(GeminiBaseModel):
    """Optional metadata embedded in PDF document properties."""

    title: str | None = Field(
        None, description="Document title shown in PDF properties"
    )
    subject: str | None = Field(None, description="Document subject")
    author: str | None = Field(None, description="Document author")


class CreatePdfInput(GeminiBaseModel):
    directory: str = Field(
        ...,
        description="Target directory path. Created if it doesn't exist. Must start with '/'.",
    )
    file_name: str = Field(
        ...,
        description="Name for the output PDF. Must end with '.pdf'. Cannot contain '/' (no nested path segments).",
    )
    content: list[PdfContentBlock] = Field(
        ...,
        description="Non-empty list of content blocks. Each block must include a 'type' key. "
        "Block types: 'paragraph' (text, bold?, italic?), 'heading' (text, level 1-4?), "
        "'bullet_list' (items), 'numbered_list' (items), "
        "'table' (rows, header?), 'page_break', 'spacer' (height in points?).",
    )
    metadata: PdfMetadataInput | None = Field(
        None,
        description="Optional metadata with 'title', 'subject', and/or 'author' "
        "embedded in the PDF document properties.",
    )
    page_size: str = Field(
        "letter",
        description="Page dimensions — either 'letter' (default) or 'a4'. Case-insensitive.",
    )


def _resolve_under_root(directory: str, file_name: str) -> tuple[str, str | None]:
    """Resolve directory and filename against the active actor's root.

    Returns:
        Tuple of (resolved_path, error_message). If error_message is not None,
        the path is invalid and should not be used.
    """
    directory = directory.strip("/")
    relative_path = os.path.join(directory, file_name) if directory else file_name
    try:
        return resolve_under_root(relative_path), None
    except PathTraversalError:
        return "", "Path traversal detected: directory cannot escape PDF root"


def _get_heading_style(styles: Any, level: int) -> ParagraphStyle:
    """Get or create heading style based on level."""
    level = max(1, min(4, level))

    heading_map = {
        1: ("Heading1", 24, 12, 6),
        2: ("Heading2", 18, 10, 4),
        3: ("Heading3", 14, 8, 3),
        4: ("Heading4", 12, 6, 2),
    }

    name, font_size, space_before, space_after = heading_map[level]

    return ParagraphStyle(
        name,
        parent=styles["Normal"],
        fontSize=font_size,
        leading=font_size + 4,
        spaceAfter=space_after,
        spaceBefore=space_before,
        fontName="Helvetica-Bold",
    )


@make_async_background
def create_pdf(input: CreatePdfInput) -> str:
    """Generate a PDF document from structured blocks and optional metadata.

    Builds a PDF document in the specified directory using a list of block
    dictionaries. Use this tool to generate reports, letters, or any
    multi-section document. Returns a confirmation string with the created
    file path, or an error message if validation fails.
    """
    directory = input.directory
    file_name = input.file_name
    content = input.content
    metadata = input.metadata
    page_size = input.page_size

    # Validate directory
    if not isinstance(directory, str) or not directory:
        return "Directory is required"
    if not directory.startswith("/"):
        return "Directory must start with /"

    # Validate file_name
    if not isinstance(file_name, str) or not file_name:
        return "File name is required"
    if "/" in file_name:
        return "File name cannot contain /"
    if not file_name.lower().endswith(".pdf"):
        return "File name must end with .pdf"

    # Validate content
    if not isinstance(content, list) or not content:
        return "Content must be a non-empty list"

    # Validate page_size
    page_size_lower = page_size.lower()
    if page_size_lower not in PAGE_SIZES:
        return f"Invalid page size: {page_size}. Must be 'letter' or 'a4'"
    selected_page_size = PAGE_SIZES[page_size_lower]

    # Resolve target path
    target_path, path_error = _resolve_under_root(directory, file_name)
    if path_error:
        return path_error

    # Ensure directory exists
    try:
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
    except Exception as exc:
        return f"Failed to create directory: {repr(exc)}"

    # Parse metadata
    pdf_metadata = PdfMetadata()
    if metadata:
        pdf_metadata = PdfMetadata(
            title=metadata.title,
            subject=metadata.subject,
            author=metadata.author,
        )

    # Create PDF document
    try:
        doc = SimpleDocTemplate(
            target_path,
            pagesize=selected_page_size,
            rightMargin=72,
            leftMargin=72,
            topMargin=72,
            bottomMargin=72,
            title=pdf_metadata.title or "",
            author=pdf_metadata.author or "",
            subject=pdf_metadata.subject or "",
        )

        # Get default styles
        styles = getSampleStyleSheet()

        # Create custom styles
        normal_style = styles["Normal"]
        bold_style = ParagraphStyle(
            "BoldNormal",
            parent=normal_style,
            fontName="Helvetica-Bold",
        )
        italic_style = ParagraphStyle(
            "ItalicNormal",
            parent=normal_style,
            fontName="Helvetica-Oblique",
        )
        bold_italic_style = ParagraphStyle(
            "BoldItalicNormal",
            parent=normal_style,
            fontName="Helvetica-BoldOblique",
        )

        # Build flowables from content blocks
        flowables = []

        for block_obj in content:
            block_dict = block_obj.model_dump(exclude_none=True)
            block_type = block_dict.get("type")

            if not block_type:
                return "Each block must have a 'type' field"

            try:
                if block_type == "paragraph":
                    block = ParagraphBlock(**block_dict)
                    if not block.text:
                        return "Paragraph text must not be empty"

                    # Select style based on bold/italic
                    if block.bold and block.italic:
                        style = bold_italic_style
                    elif block.bold:
                        style = bold_style
                    elif block.italic:
                        style = italic_style
                    else:
                        style = normal_style

                    flowables.append(Paragraph(_pdf_text_safe(block.text), style))
                    flowables.append(Spacer(1, 6))

                elif block_type == "heading":
                    block = HeadingBlock(**block_dict)
                    if not block.text:
                        return "Heading text must not be empty"

                    heading_style = _get_heading_style(styles, block.level)
                    flowables.append(
                        Paragraph(_pdf_text_safe(block.text), heading_style)
                    )

                elif block_type == "bullet_list":
                    block = BulletListBlock(**block_dict)
                    if not block.items:
                        return "Bullet list must contain at least one item"

                    list_items = [
                        Paragraph(_pdf_text_safe(item), normal_style)
                        for item in block.items
                    ]
                    flowables.append(
                        ListFlowable(
                            list_items,
                            bulletType="bullet",
                            leftIndent=18,
                            bulletFontSize=8,
                        )
                    )
                    flowables.append(Spacer(1, 6))

                elif block_type == "numbered_list":
                    block = NumberedListBlock(**block_dict)
                    if not block.items:
                        return "Numbered list must contain at least one item"

                    list_items = [
                        Paragraph(_pdf_text_safe(item), normal_style)
                        for item in block.items
                    ]
                    flowables.append(
                        ListFlowable(
                            list_items,
                            bulletType="1",
                            leftIndent=18,
                        )
                    )
                    flowables.append(Spacer(1, 6))

                elif block_type == "table":
                    block = TableBlock(**block_dict)
                    if not block.rows:
                        return "Table must contain at least one row"

                    # Validate all rows have same column count
                    column_count = len(block.rows[0])
                    for idx, row in enumerate(block.rows):
                        if not row:
                            return f"Table row {idx} must contain at least one cell"
                        if len(row) != column_count:
                            return "All table rows must have the same number of cells"

                    # Create table with data
                    table = Table(block.rows)

                    # Apply table style
                    table_style_commands = [
                        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.grey),
                        ("BOX", (0, 0), (-1, -1), 0.5, colors.black),
                        ("LEFTPADDING", (0, 0), (-1, -1), 6),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ]

                    # Bold header if specified
                    if block.header and len(block.rows) > 0:
                        table_style_commands.extend(
                            [
                                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                            ]
                        )

                    table.setStyle(TableStyle(table_style_commands))
                    flowables.append(table)
                    flowables.append(Spacer(1, 12))

                elif block_type == "page_break":
                    flowables.append(PageBreak())

                elif block_type == "spacer":
                    block = SpacerBlock(**block_dict)
                    flowables.append(Spacer(1, block.height))

                else:
                    return f"Unknown block type: {block_type}"

            except Exception as exc:
                return f"Invalid content block: {repr(exc)}"

        # Build the PDF
        doc.build(flowables)
        validation_error = _validate_rendered_text(target_path, content)
        if validation_error:
            try:
                os.remove(target_path)
            except FileNotFoundError:
                pass
            except OSError:
                pass
            return validation_error

    except Exception as exc:
        return f"Failed to create PDF: {repr(exc)}"

    return f"PDF {file_name} created at {virtual_path_from_physical(target_path)}"
