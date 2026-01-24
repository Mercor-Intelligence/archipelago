import os
from typing import Annotated, Any, Literal

from docx import Document
from docx.document import Document as DocumentObject
from docx.enum.section import WD_SECTION
from pydantic import Discriminator, Field, Tag, validate_call
from pydantic.dataclasses import dataclass
from utils.decorators import make_async_background

DOCS_ROOT = os.getenv("APP_DOCS_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


@dataclass
class DocumentMetadata:
    """Optional metadata applied to the generated document."""

    title: str | None = Field(
        default=None, description="Document title shown in file properties"
    )
    subject: str | None = Field(
        default=None, description="Subject or topic of the document"
    )
    author: str | None = Field(default=None, description="Author name for the document")
    comments: str | None = Field(
        default=None, description="Additional comments or notes about the document"
    )


@dataclass
class ParagraphBlock:
    """A paragraph content block for the document."""

    text: str = Field(description="The paragraph text content")
    type: Literal["paragraph"] = Field(
        default="paragraph", description="Block type identifier"
    )
    style: str | None = Field(
        default=None, description="Named Documents style to apply (e.g., 'Normal', 'Quote')"
    )
    bold: bool = Field(
        default=False, description="Whether to bold the entire paragraph"
    )
    italic: bool = Field(
        default=False, description="Whether to italicize the entire paragraph"
    )


@dataclass
class HeadingBlock:
    """A heading content block for the document."""

    text: str = Field(description="The heading text content")
    type: Literal["heading"] = Field(
        default="heading", description="Block type identifier"
    )
    level: int = Field(
        default=1, description="Heading level from 1 (largest) to 4 (smallest)"
    )
    style: str | None = Field(
        default=None,
        description="Named Documents style to override the default heading style",
    )


@dataclass
class BulletListBlock:
    """A bullet list content block for the document."""

    items: list[str] = Field(description="List of bullet point text items")
    type: Literal["bullet_list"] = Field(
        default="bullet_list", description="Block type identifier"
    )
    style: str | None = Field(
        default=None,
        description="Named Documents style to apply (defaults to 'List Bullet')",
    )


@dataclass
class NumberedListBlock:
    """A numbered list content block for the document."""

    items: list[str] = Field(description="List of numbered list text items")
    type: Literal["numbered_list"] = Field(
        default="numbered_list", description="Block type identifier"
    )
    style: str | None = Field(
        default=None,
        description="Named Documents style to apply (defaults to 'List Number')",
    )


@dataclass
class TableBlock:
    """A table content block for the document."""

    rows: list[list[str]] = Field(
        description="2D list of cell values; each inner list is a row"
    )
    type: Literal["table"] = Field(default="table", description="Block type identifier")
    style: str | None = Field(
        default=None, description="Named Documents table style to apply"
    )
    header: bool = Field(
        default=True, description="Whether to bold the first row as a header"
    )


@dataclass
class PageBreakBlock:
    """A page break content block for the document."""

    type: str = Field(default="page_break", description="Block type identifier")


@dataclass
class SectionBreakBlock:
    """A section break content block for the document."""

    type: str = Field(default="section_break", description="Block type identifier")
    start_type: str = Field(
        default="new_page",
        description="Section start type: 'new_page', 'continuous', 'odd_page', 'even_page'",
    )


# Map section break start types to WD_SECTION enum values
SECTION_START_TYPES = {
    "new_page": WD_SECTION.NEW_PAGE,
    "continuous": WD_SECTION.CONTINUOUS,
    "odd_page": WD_SECTION.ODD_PAGE,
    "even_page": WD_SECTION.EVEN_PAGE,
}


def _get_block_type(v: Any) -> str:
    """Extract the type discriminator from a content block."""
    if isinstance(v, dict):
        return v.get("type", "")
    return getattr(v, "type", "")


ContentBlock = Annotated[
    Annotated[ParagraphBlock, Tag("paragraph")]
    | Annotated[HeadingBlock, Tag("heading")]
    | Annotated[BulletListBlock, Tag("bullet_list")]
    | Annotated[NumberedListBlock, Tag("numbered_list")]
    | Annotated[TableBlock, Tag("table")]
    | Annotated[PageBreakBlock, Tag("page_break")]
    | Annotated[SectionBreakBlock, Tag("section_break")],
    Discriminator(_get_block_type),
]


def _resolve_under_root(directory: str, file_name: str) -> str:
    """Map directory and filename to the docs root."""
    directory = directory.strip("/")
    if directory:
        full_path = os.path.join(DOCS_ROOT, directory, file_name)
    else:
        full_path = os.path.join(DOCS_ROOT, file_name)
    return os.path.normpath(full_path)


def _apply_metadata(doc: DocumentObject, metadata: DocumentMetadata) -> None:
    core = doc.core_properties
    if metadata.title is not None:
        core.title = metadata.title
    if metadata.subject is not None:
        core.subject = metadata.subject
    if metadata.author is not None:
        core.author = metadata.author
    if metadata.comments is not None:
        core.comments = metadata.comments


@make_async_background
@validate_call
def create_document(
    directory: Annotated[
        str,
        Field(description="Directory path starting with '/' (e.g., '/' or '/reports')"),
    ],
    file_name: Annotated[
        str, Field(description="File name ending with .docx (e.g., 'report.docx')")
    ],
    content: Annotated[
        list[ContentBlock],
        Field(
            description="List of content blocks. Each block must have a 'type' field."
        ),
    ],
    metadata: Annotated[
        DocumentMetadata | None,
        Field(description="Optional document metadata"),
    ] = None,
) -> str:
    """Create a new .docx document composed of structured content blocks.

    Args:
        directory: Directory path (must start with /)
        file_name: Document filename (must end with .docx)
        content: List of content blocks. Supported block types:

            - paragraph: {type, text, style?, bold?, italic?}
            - heading: {type, text, level? (1-4)}
            - bullet_list: {type, items[], style?}
            - numbered_list: {type, items[], style?}
            - table: {type, rows[][], style?, header? (bool)}
            - page_break: {type} - Insert a page break
            - section_break: {type, start_type?} - Insert a section break
                - start_type: 'new_page' (default), 'continuous', 'odd_page', 'even_page'

        metadata: Optional document metadata {title?, subject?, author?, comments?}

    Returns:
        Success message with file path, or error message
    """

    # @validate_call handles type conversion, but we still need business logic validation
    if not directory:
        return "Directory is required"
    if not directory.startswith("/"):
        return "Directory must start with /"

    if not file_name:
        return "File name is required"
    if "/" in file_name:
        return "File name cannot contain /"
    if not file_name.lower().endswith(".docx"):
        return "File name must end with .docx"

    if not content:
        return "Content must be a non-empty list"

    doc = Document()

    if metadata:
        _apply_metadata(doc, metadata)

    available_styles = {style.name for style in doc.styles}

    # Content blocks are already validated and converted by @validate_call
    for block in content:
        try:
            if isinstance(block, ParagraphBlock):
                if not block.text:
                    return "Paragraph text must not be empty"
                if block.style and block.style not in available_styles:
                    return f"Style '{block.style}' is not defined in the document"
                paragraph = doc.add_paragraph(block.text, style=block.style)
                if block.bold or block.italic:
                    for run in paragraph.runs:
                        run.bold = block.bold
                        run.italic = block.italic

            elif isinstance(block, HeadingBlock):
                if not block.text:
                    return "Heading text must not be empty"
                # Clamp level between 1 and 4
                level = max(1, min(4, block.level))
                if block.style:
                    # Use custom style if provided
                    if block.style not in available_styles:
                        return f"Style '{block.style}' is not defined in the document"
                    doc.add_paragraph(block.text, style=block.style)
                else:
                    doc.add_heading(block.text, level=level)

            elif isinstance(block, BulletListBlock):
                if not block.items:
                    return "Bullet list must contain at least one item"
                list_style = block.style or "List Bullet"
                if list_style not in available_styles:
                    return f"Style '{list_style}' is not defined in the document"
                for item in block.items:
                    doc.add_paragraph(item, style=list_style)

            elif isinstance(block, NumberedListBlock):
                if not block.items:
                    return "Numbered list must contain at least one item"
                list_style = block.style or "List Number"
                if list_style not in available_styles:
                    return f"Style '{list_style}' is not defined in the document"
                for item in block.items:
                    doc.add_paragraph(item, style=list_style)

            elif isinstance(block, TableBlock):
                if not block.rows:
                    return "Table must contain at least one row"
                # Validate all rows have same column count
                column_count = len(block.rows[0])
                for idx, row in enumerate(block.rows):
                    if not row:
                        return f"Table row {idx} must contain at least one cell"
                    if len(row) != column_count:
                        return "All table rows must have the same number of cells"

                table = doc.add_table(rows=len(block.rows), cols=column_count)
                if block.style:
                    table.style = block.style
                for row_idx, row_values in enumerate(block.rows):
                    for col_idx, cell_value in enumerate(row_values):
                        table.cell(row_idx, col_idx).text = cell_value
                if block.header:
                    for cell in table.rows[0].cells:
                        for paragraph in cell.paragraphs:
                            for run in paragraph.runs:
                                run.bold = True

            elif isinstance(block, PageBreakBlock):
                doc.add_page_break()

            elif isinstance(block, SectionBreakBlock):
                start_type = block.start_type
                if start_type not in SECTION_START_TYPES:
                    valid_types = ", ".join(sorted(SECTION_START_TYPES.keys()))
                    return f"Invalid section start_type: '{start_type}'. Must be one of: {valid_types}"
                doc.add_section(SECTION_START_TYPES[start_type])

            else:
                return f"Unknown block type: {type(block)}"

        except Exception as exc:
            return f"Invalid content block: {repr(exc)}"

    # Save document to filesystem
    target_path = _resolve_under_root(directory, file_name)
    try:
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        doc.save(target_path)
    except Exception as exc:
        return f"Failed to create document: {repr(exc)}"

    return f"Document {file_name} created at {target_path}"
