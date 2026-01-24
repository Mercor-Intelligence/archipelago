"""Meta-tools for LLM agents - consolidated interface with action-based routing."""

import base64
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

# Import existing tools for delegation
from tools.add_content_text import add_content_text as _add_content_text
from tools.add_image import add_image as _add_image
from tools.apply_formatting import apply_formatting as _apply_formatting
from tools.comments import comments as _comments
from tools.create_document import (
    ContentBlock,
    DocumentMetadata,
)
from tools.create_document import (
    create_document as _create_document,
)
from tools.delete_content_text import delete_content_text as _delete_content_text
from tools.delete_document import delete_document as _delete_document
from tools.edit_content_text import edit_content_text as _edit_content_text
from tools.get_document_overview import get_document_overview as _get_document_overview
from tools.header_footer import header_footer as _header_footer
from tools.modify_image import modify_image as _modify_image
from tools.page_margins import page_margins as _page_margins
from tools.page_orientation import page_orientation as _page_orientation
from tools.read_document_content import read_document_content as _read_document_content
from tools.read_image import read_image as _read_image

# ============ Success/Error Detection ============
# The underlying tools return either:
# 1. Pydantic models converted to strings via str() - contain "Status: success"
# 2. Simple strings for create/delete/image ops
#
# We check for success rather than error patterns to avoid false positives
# when document content contains error-like words (e.g., "Invalid input handling").


def _is_pydantic_success(result: str) -> bool:
    """Check if a Pydantic model result indicates success.

    Underlying tools use Pydantic models with __str__ methods that output
    'Status: success' for successful operations.
    """
    return "Status: success" in result


def _is_create_success(result: str) -> bool:
    """Check if create_document result indicates success.

    Success format: "Document {filename} created at {path}"
    """
    return " created at " in result


def _is_delete_success(result: str) -> bool:
    """Check if delete_document result indicates success.

    Success format: "Document {filepath} deleted successfully"
    """
    return "deleted successfully" in result


def _is_add_image_success(result: str) -> bool:
    """Check if add_image result indicates success.

    Success format: "Image added to {identifier} at position {position}..."
    """
    return result.startswith("Image added to ")


def _is_modify_image_success(result: str) -> bool:
    """Check if modify_image result indicates success.

    Success format: "Image {index} at {location} {operation_desc}"
    Errors start with: "File path", "Image path", "Invalid", "No images", etc.
    """
    # Success messages start with "Image N at"
    return result.startswith("Image ") and " at " in result


def _is_header_footer_success(result: str) -> bool:
    """Check if header_footer result indicates success.

    Success format includes "Status: success" from Pydantic __str__ methods.
    """
    return "Status: success" in result


def _is_page_margins_success(result: str) -> bool:
    """Check if page_margins result indicates success.

    Success format includes "Status: success" from Pydantic __str__ methods.
    """
    return "Status: success" in result


def _is_page_orientation_success(result: str) -> bool:
    """Check if page_orientation result indicates success.

    Success format includes "Status: success" from Pydantic __str__ methods.
    """
    return "Status: success" in result


def _is_comments_success(result: str) -> bool:
    """Check if comments result indicates success.

    Success format includes "Status: success" from Pydantic __str__ methods.
    """
    return "Status: success" in result


# ============ Help Response ============
class ActionInfo(BaseModel):
    """Information about an action."""

    model_config = ConfigDict(extra="forbid")
    description: str
    required_params: list[str]
    optional_params: list[str]


class HelpResponse(BaseModel):
    """Help response listing available actions."""

    model_config = ConfigDict(extra="forbid")
    tool_name: str
    description: str
    actions: dict[str, ActionInfo]


# ============ Result Models ============
class CreateResult(BaseModel):
    """Result from creating a document."""

    model_config = ConfigDict(extra="forbid")
    status: str
    file_path: str


class DeleteResult(BaseModel):
    """Result from deleting a document."""

    model_config = ConfigDict(extra="forbid")
    status: str
    file_path: str


class OverviewResult(BaseModel):
    """Result from getting document overview."""

    model_config = ConfigDict(extra="forbid")
    raw_output: str = Field(..., description="Document structure output")


class ReadContentResult(BaseModel):
    """Result from reading document content."""

    model_config = ConfigDict(extra="forbid")
    raw_output: str = Field(..., description="Document content output")


class ReadImageResult(BaseModel):
    """Result from reading an image."""

    model_config = ConfigDict(extra="forbid")
    status: str
    message: str
    image_data: str | None = Field(None, description="Base64-encoded JPEG image data")
    image_format: str | None = Field(None, description="Image format (e.g., 'jpeg')")


class AddTextResult(BaseModel):
    """Result from adding text content."""

    model_config = ConfigDict(extra="forbid")
    status: str
    file_path: str
    identifier: str
    position: str


class EditTextResult(BaseModel):
    """Result from editing text content."""

    model_config = ConfigDict(extra="forbid")
    status: str
    file_path: str
    identifier: str


class DeleteTextResult(BaseModel):
    """Result from deleting text content."""

    model_config = ConfigDict(extra="forbid")
    status: str
    file_path: str
    identifier: str


class AddImageResult(BaseModel):
    """Result from adding an image."""

    model_config = ConfigDict(extra="forbid")
    status: str
    message: str


class ModifyImageResult(BaseModel):
    """Result from modifying an image."""

    model_config = ConfigDict(extra="forbid")
    status: str
    message: str


class FormatResult(BaseModel):
    """Result from applying formatting."""

    model_config = ConfigDict(extra="forbid")
    status: str
    file_path: str
    identifier: str
    applied: dict[str, Any]


class HeaderFooterResult(BaseModel):
    """Result from header/footer operations."""

    model_config = ConfigDict(extra="forbid")
    status: str
    file_path: str
    area: str
    section_index: int
    hf_action: str
    raw_output: str | None = Field(None, description="Raw output for read action")


class PageMarginsResult(BaseModel):
    """Result from page margins operations."""

    model_config = ConfigDict(extra="forbid")
    status: str
    file_path: str
    section_index: int
    pm_action: str
    raw_output: str | None = Field(None, description="Raw output for read action")


class PageOrientationResult(BaseModel):
    """Result from page orientation operations."""

    model_config = ConfigDict(extra="forbid")
    status: str
    file_path: str
    section_index: int
    po_action: str
    raw_output: str | None = Field(None, description="Raw output for read action")


class CommentsResult(BaseModel):
    """Result from comments operations."""

    model_config = ConfigDict(extra="forbid")
    status: str
    file_path: str
    comments_action: str
    raw_output: str | None = Field(None, description="Raw output for read action")


# ============ Input Model ============
class DocsInput(BaseModel):
    """Input for docs meta-tool."""

    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "help",
        "create",
        "delete",
        "overview",
        "read_content",
        "read_image",
        "add_text",
        "edit_text",
        "delete_text",
        "add_image",
        "modify_image",
        "format",
        "header_footer",
        "page_margins",
        "page_orientation",
        "comments",
    ] = Field(..., description="Action to perform")

    # File operations
    file_path: str | None = Field(
        None, description="Full file path. REQUIRED for file operations."
    )
    directory: str | None = Field(
        None, description="Directory for 'create' (e.g., '/')"
    )
    file_name: str | None = Field(
        None, description="File name for 'create' (e.g., 'report.docx')"
    )

    # Content blocks for create
    content: list[dict[str, Any]] | None = Field(
        None,
        description=(
            "Content blocks for 'create'. Each block needs 'type' field. Types: "
            "paragraph ({type: 'paragraph', text: 'Your text'}), "
            "heading ({type: 'heading', text: 'Title', level: 1}), "
            "bullet_list ({type: 'bullet_list', items: ['A', 'B']}), "
            "numbered_list ({type: 'numbered_list', items: ['1st', '2nd']}), "
            "table ({type: 'table', rows: [['H1', 'H2'], ['C1', 'C2']], header: true}). "
            "Example: [{type: 'heading', text: 'My Doc', level: 1}, {type: 'paragraph', text: 'Hello'}]"
        ),
    )
    metadata: dict[str, Any] | None = Field(
        None, description="Document metadata for 'create': {title?, author?, ...}"
    )

    # Content operations
    identifier: str | None = Field(
        None, description="Stable identifier from read_content (e.g., 'body.p.0')"
    )
    text: str | None = Field(None, description="Text content for add_text")
    new_text: str | None = Field(None, description="Replacement text for edit_text")
    position: str | None = Field(
        None, description="Position for add_text/add_image: 'start' or 'end'"
    )
    scope: str | None = Field(
        None, description="Scope for delete_text: 'content' or 'element'"
    )
    collapse_whitespace: bool | None = Field(
        None, description="Collapse whitespace for delete_text in cells"
    )

    # Read options
    section_index: int | None = Field(
        None, description="Section index for read_content pagination"
    )
    annotation: str | None = Field(
        None, description="Image annotation key for read_image"
    )

    # Image operations
    image_path: str | None = Field(None, description="Path to image file for add_image")
    image_index: int | None = Field(
        None, description="0-based image index for modify_image"
    )
    operation: str | None = Field(
        None,
        description="Operation for modify_image: rotate, flip, brightness, contrast",
    )
    rotation: int | None = Field(None, description="Rotation angle (0-360)")
    flip: str | None = Field(
        None, description="Flip direction: 'horizontal' or 'vertical'"
    )
    brightness: float | None = Field(
        None, description="Brightness factor (0.0-2.0). 1.0=unchanged."
    )
    contrast: float | None = Field(
        None, description="Contrast factor (0.0-2.0). 1.0=unchanged."
    )
    width: float | None = Field(
        None, description="Width in pixels. Optional for export."
    )
    height: float | None = Field(
        None, description="Height in pixels. Optional for export."
    )

    # Formatting
    bold: bool | None = Field(None, description="Apply bold formatting.")
    italic: bool | None = Field(None, description="Apply italic formatting.")
    underline: bool | None = Field(None, description="Underline formatting")
    strikethrough: bool | None = Field(None, description="Strikethrough formatting")
    font_size: float | None = Field(None, description="Font size in points.")
    font_color: str | None = Field(
        None, description="Font color as hex (e.g., 'FF0000')"
    )

    # Header/Footer operations
    hf_action: str | None = Field(
        None, description="Header/footer action: 'read', 'set', 'clear', or 'link'"
    )
    area: str | None = Field(
        None, description="Header/footer area: 'header' or 'footer'"
    )
    link_to_previous: bool | None = Field(
        None, description="For 'link' hf_action: True to link, False to unlink"
    )

    # Page Margins operations
    pm_action: str | None = Field(
        None, description="Page margins action: 'read' or 'set'"
    )
    margin_top: float | None = Field(None, description="Top margin in inches")
    margin_bottom: float | None = Field(None, description="Bottom margin in inches")
    margin_left: float | None = Field(None, description="Left margin in inches")
    margin_right: float | None = Field(None, description="Right margin in inches")

    # Page Orientation operations
    po_action: str | None = Field(
        None, description="Page orientation action: 'read' or 'set'"
    )
    orientation: str | None = Field(
        None, description="Page orientation: 'portrait' or 'landscape'"
    )

    # Comments operations
    comments_action: str | None = Field(
        None, description="Comments action: 'read', 'add', or 'delete'"
    )
    comment_text: str | None = Field(None, description="Comment text for 'add' action")
    comment_author: str | None = Field(
        None, description="Comment author for 'add' action"
    )
    comment_id: int | None = Field(None, description="Comment ID for 'delete' action")


# ============ Output Model ============
class DocsOutput(BaseModel):
    """Output for docs meta-tool."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(
        ...,
        description="The operation to perform. REQUIRED. Call with action='help' first.",
    )
    error: str | None = Field(None, description="Error message if failed")

    # Discovery
    help: HelpResponse | None = None

    # Action-specific results
    create: CreateResult | None = None
    delete: DeleteResult | None = None
    overview: OverviewResult | None = None
    read_content: ReadContentResult | None = None
    read_image: ReadImageResult | None = None
    add_text: AddTextResult | None = None
    edit_text: EditTextResult | None = None
    delete_text: DeleteTextResult | None = None
    add_image: AddImageResult | None = None
    modify_image: ModifyImageResult | None = None
    format: FormatResult | None = None
    header_footer: HeaderFooterResult | None = None
    page_margins: PageMarginsResult | None = None
    page_orientation: PageOrientationResult | None = None
    comments: CommentsResult | None = None


# ============ Help Definition ============
DOCS_HELP = HelpResponse(
    tool_name="docs",
    description="Document operations: create, read, edit, and manage .docx files.",
    actions={
        "help": ActionInfo(
            description="List all available actions",
            required_params=[],
            optional_params=[],
        ),
        "create": ActionInfo(
            description="Create a new .docx document",
            required_params=["directory", "file_name", "content"],
            optional_params=["metadata"],
        ),
        "delete": ActionInfo(
            description="Delete a document",
            required_params=["file_path"],
            optional_params=[],
        ),
        "overview": ActionInfo(
            description="Get document structure/headings",
            required_params=["file_path"],
            optional_params=[],
        ),
        "read_content": ActionInfo(
            description="Read document content with stable identifiers",
            required_params=["file_path"],
            optional_params=["section_index"],
        ),
        "read_image": ActionInfo(
            description="Read an embedded image by annotation",
            required_params=["file_path", "annotation"],
            optional_params=[],
        ),
        "add_text": ActionInfo(
            description="Insert text at a location",
            required_params=["file_path", "identifier", "text"],
            optional_params=["position"],
        ),
        "edit_text": ActionInfo(
            description="Replace text at a location",
            required_params=["file_path", "identifier", "new_text"],
            optional_params=[],
        ),
        "delete_text": ActionInfo(
            description="Delete text or element",
            required_params=["file_path", "identifier"],
            optional_params=["scope", "collapse_whitespace"],
        ),
        "add_image": ActionInfo(
            description="Add an image to the document",
            required_params=["file_path", "image_path", "identifier"],
            optional_params=["position", "width", "height"],
        ),
        "modify_image": ActionInfo(
            description="Modify an existing image",
            required_params=["file_path", "image_index", "operation"],
            optional_params=["rotation", "flip", "brightness", "contrast"],
        ),
        "format": ActionInfo(
            description="Apply text formatting",
            required_params=["file_path", "identifier"],
            optional_params=[
                "bold",
                "italic",
                "underline",
                "strikethrough",
                "font_size",
                "font_color",
            ],
        ),
        "header_footer": ActionInfo(
            description="Read, set, clear, or link headers/footers",
            required_params=["file_path", "hf_action", "area"],
            optional_params=["section_index", "content", "link_to_previous"],
        ),
        "page_margins": ActionInfo(
            description="Read or set page margins",
            required_params=["file_path", "pm_action"],
            optional_params=[
                "section_index",
                "margin_top",
                "margin_bottom",
                "margin_left",
                "margin_right",
            ],
        ),
        "page_orientation": ActionInfo(
            description="Read or set page orientation (portrait/landscape)",
            required_params=["file_path", "po_action"],
            optional_params=["section_index", "orientation"],
        ),
        "comments": ActionInfo(
            description="Read, add, or delete comments",
            required_params=["file_path", "comments_action"],
            optional_params=[
                "identifier",
                "comment_text",
                "comment_author",
                "comment_id",
            ],
        ),
    },
)


# ============ Type Adapters ============
_CONTENT_ADAPTER: TypeAdapter[list[ContentBlock]] = TypeAdapter(list[ContentBlock])
_METADATA_ADAPTER: TypeAdapter[DocumentMetadata | None] = TypeAdapter(
    DocumentMetadata | None
)


# ============ Meta-Tool Implementation ============
async def docs(request: DocsInput) -> DocsOutput:
    """Document operations: create, read, edit, and manage .docx files."""
    match request.action:
        case "help":
            return DocsOutput(action="help", help=DOCS_HELP, error=None)

        case "create":
            if not request.directory or not request.file_name or not request.content:
                return DocsOutput(
                    action="create",
                    error="Required: directory, file_name, content",
                )
            # Validate content and metadata before calling - gives proper types and clear errors
            try:
                validated_content = _CONTENT_ADAPTER.validate_python(request.content)
                validated_metadata = _METADATA_ADAPTER.validate_python(request.metadata)
            except ValidationError as exc:
                return DocsOutput(action="create", error=f"Invalid input: {exc}")
            result = await _create_document(
                request.directory,
                request.file_name,
                validated_content,
                validated_metadata,
            )
            if not _is_create_success(result):
                return DocsOutput(action="create", error=result)
            return DocsOutput(
                action="create",
                create=CreateResult(
                    status="success",
                    file_path=f"{request.directory.rstrip('/')}/{request.file_name}",
                ),
                error=None,
            )

        case "delete":
            if not request.file_path:
                return DocsOutput(action="delete", error="Required: file_path")
            result = await _delete_document(request.file_path)
            if not _is_delete_success(result):
                return DocsOutput(action="delete", error=result)
            return DocsOutput(
                action="delete",
                delete=DeleteResult(status="success", file_path=request.file_path),
                error=None,
            )

        case "overview":
            if not request.file_path:
                return DocsOutput(action="overview", error="Required: file_path")
            result = await _get_document_overview(request.file_path)
            # Check for success indicator - Pydantic __str__ outputs "Status: success"
            if not _is_pydantic_success(result):
                return DocsOutput(action="overview", error=result)
            return DocsOutput(
                action="overview",
                overview=OverviewResult(raw_output=result),
                error=None,
            )

        case "read_content":
            if not request.file_path:
                return DocsOutput(action="read_content", error="Required: file_path")
            result = await _read_document_content(
                request.file_path, request.section_index
            )
            # Check for success indicator - Pydantic __str__ outputs "Status: success"
            if not _is_pydantic_success(result):
                return DocsOutput(action="read_content", error=result)
            return DocsOutput(
                action="read_content",
                read_content=ReadContentResult(raw_output=result),
                error=None,
            )

        case "read_image":
            if not request.file_path or not request.annotation:
                return DocsOutput(
                    action="read_image", error="Required: file_path, annotation"
                )
            try:
                image = await _read_image(request.file_path, request.annotation)
                # Encode image data as base64 for JSON serialization
                if image.data is None:
                    return DocsOutput(action="read_image", error="Image data is empty")
                image_b64 = base64.b64encode(image.data).decode("utf-8")
                return DocsOutput(
                    action="read_image",
                    read_image=ReadImageResult(
                        status="success",
                        message=f"Image retrieved: {request.annotation}",
                        image_data=image_b64,
                        image_format="jpeg",  # Always jpeg - see read_image.py
                    ),
                    error=None,
                )
            except Exception as exc:
                return DocsOutput(action="read_image", error=str(exc))

        case "add_text":
            if not request.file_path or not request.identifier or not request.text:
                return DocsOutput(
                    action="add_text",
                    error="Required: file_path, identifier, text",
                )
            result = await _add_content_text(
                request.file_path,
                request.identifier,
                request.text,
                request.position or "end",
            )
            if not _is_pydantic_success(result):
                return DocsOutput(action="add_text", error=result)
            return DocsOutput(
                action="add_text",
                add_text=AddTextResult(
                    status="success",
                    file_path=request.file_path,
                    identifier=request.identifier,
                    position=request.position or "end",
                ),
                error=None,
            )

        case "edit_text":
            if (
                not request.file_path
                or not request.identifier
                or request.new_text is None
            ):
                return DocsOutput(
                    action="edit_text",
                    error="Required: file_path, identifier, new_text",
                )
            result = await _edit_content_text(
                request.file_path, request.identifier, request.new_text
            )
            if not _is_pydantic_success(result):
                return DocsOutput(action="edit_text", error=result)
            return DocsOutput(
                action="edit_text",
                edit_text=EditTextResult(
                    status="success",
                    file_path=request.file_path,
                    identifier=request.identifier,
                ),
                error=None,
            )

        case "delete_text":
            if not request.file_path or not request.identifier:
                return DocsOutput(
                    action="delete_text",
                    error="Required: file_path, identifier",
                )
            result = await _delete_content_text(
                request.file_path,
                request.identifier,
                request.scope or "content",
                request.collapse_whitespace
                if request.collapse_whitespace is not None
                else False,
            )
            if not _is_pydantic_success(result):
                return DocsOutput(action="delete_text", error=result)
            return DocsOutput(
                action="delete_text",
                delete_text=DeleteTextResult(
                    status="success",
                    file_path=request.file_path,
                    identifier=request.identifier,
                ),
                error=None,
            )

        case "add_image":
            if (
                not request.file_path
                or not request.image_path
                or not request.identifier
            ):
                return DocsOutput(
                    action="add_image",
                    error="Required: file_path, image_path, identifier",
                )
            result = await _add_image(
                request.file_path,
                request.image_path,
                request.identifier,
                request.position or "end",
                request.width,
                request.height,
            )
            if not _is_add_image_success(result):
                return DocsOutput(action="add_image", error=result)
            return DocsOutput(
                action="add_image",
                add_image=AddImageResult(status="success", message=result),
                error=None,
            )

        case "modify_image":
            if (
                not request.file_path
                or request.image_index is None
                or not request.operation
            ):
                return DocsOutput(
                    action="modify_image",
                    error="Required: file_path, image_index, operation",
                )
            result = await _modify_image(
                request.file_path,
                request.image_index,
                request.operation,
                request.rotation,
                request.flip,
                request.brightness,
                request.contrast,
            )
            if not _is_modify_image_success(result):
                return DocsOutput(action="modify_image", error=result)
            return DocsOutput(
                action="modify_image",
                modify_image=ModifyImageResult(status="success", message=result),
                error=None,
            )

        case "format":
            if not request.file_path or not request.identifier:
                return DocsOutput(
                    action="format",
                    error="Required: file_path, identifier",
                )
            result = await _apply_formatting(
                request.file_path,
                request.identifier,
                request.bold,
                request.italic,
                request.underline,
                request.strikethrough,
                request.font_size,
                request.font_color,
            )
            if not _is_pydantic_success(result):
                return DocsOutput(action="format", error=result)

            applied: dict[str, Any] = {}
            if request.bold is not None:
                applied["bold"] = request.bold
            if request.italic is not None:
                applied["italic"] = request.italic
            if request.underline is not None:
                applied["underline"] = request.underline
            if request.strikethrough is not None:
                applied["strikethrough"] = request.strikethrough
            if request.font_size is not None:
                applied["font_size"] = request.font_size
            if request.font_color is not None:
                applied["font_color"] = request.font_color

            return DocsOutput(
                action="format",
                format=FormatResult(
                    status="success",
                    file_path=request.file_path,
                    identifier=request.identifier,
                    applied=applied,
                ),
                error=None,
            )

        case "header_footer":
            if not request.file_path or not request.hf_action or not request.area:
                return DocsOutput(
                    action="header_footer",
                    error="Required: file_path, hf_action, area",
                )
            result = await _header_footer(
                request.file_path,
                request.hf_action,
                request.area,
                request.section_index if request.section_index is not None else 0,
                request.content,
                request.link_to_previous,
            )
            if not _is_header_footer_success(result):
                return DocsOutput(action="header_footer", error=result)
            return DocsOutput(
                action="header_footer",
                header_footer=HeaderFooterResult(
                    status="success",
                    file_path=request.file_path,
                    area=request.area,
                    section_index=request.section_index
                    if request.section_index is not None
                    else 0,
                    hf_action=request.hf_action,
                    raw_output=result if request.hf_action == "read" else None,
                ),
                error=None,
            )

        case "page_margins":
            if not request.file_path or not request.pm_action:
                return DocsOutput(
                    action="page_margins",
                    error="Required: file_path, pm_action",
                )
            result = await _page_margins(
                request.file_path,
                request.pm_action,
                request.section_index if request.section_index is not None else 0,
                request.margin_top,
                request.margin_bottom,
                request.margin_left,
                request.margin_right,
            )
            if not _is_page_margins_success(result):
                return DocsOutput(action="page_margins", error=result)
            return DocsOutput(
                action="page_margins",
                page_margins=PageMarginsResult(
                    status="success",
                    file_path=request.file_path,
                    section_index=request.section_index
                    if request.section_index is not None
                    else 0,
                    pm_action=request.pm_action,
                    raw_output=result if request.pm_action == "read" else None,
                ),
                error=None,
            )

        case "page_orientation":
            if not request.file_path or not request.po_action:
                return DocsOutput(
                    action="page_orientation",
                    error="Required: file_path, po_action",
                )
            result = await _page_orientation(
                request.file_path,
                request.po_action,
                request.section_index if request.section_index is not None else 0,
                request.orientation,
            )
            if not _is_page_orientation_success(result):
                return DocsOutput(action="page_orientation", error=result)
            return DocsOutput(
                action="page_orientation",
                page_orientation=PageOrientationResult(
                    status="success",
                    file_path=request.file_path,
                    section_index=request.section_index
                    if request.section_index is not None
                    else 0,
                    po_action=request.po_action,
                    raw_output=result if request.po_action == "read" else None,
                ),
                error=None,
            )

        case "comments":
            if not request.file_path or not request.comments_action:
                return DocsOutput(
                    action="comments",
                    error="Required: file_path, comments_action",
                )
            result = await _comments(
                request.file_path,
                request.comments_action,
                request.identifier,
                request.comment_text,
                request.comment_author,
                request.comment_id,
            )
            if not _is_comments_success(result):
                return DocsOutput(action="comments", error=result)
            return DocsOutput(
                action="comments",
                comments=CommentsResult(
                    status="success",
                    file_path=request.file_path,
                    comments_action=request.comments_action,
                    raw_output=result if request.comments_action == "read" else None,
                ),
                error=None,
            )

        case _:
            return DocsOutput(
                action=request.action, error=f"Unknown action: {request.action}"
            )


# ============ Schema Tool ============
class SchemaInput(BaseModel):
    """Input for schema introspection."""

    model_config = ConfigDict(extra="forbid")
    model: str = Field(
        ...,
        description="Model name: 'input', 'output', or a result type",
    )


class SchemaOutput(BaseModel):
    """Output for schema introspection."""

    model_config = ConfigDict(extra="forbid")
    model: str
    json_schema: dict[str, Any]


SCHEMAS: dict[str, type[BaseModel]] = {
    "input": DocsInput,
    "output": DocsOutput,
    "CreateResult": CreateResult,
    "DeleteResult": DeleteResult,
    "OverviewResult": OverviewResult,
    "ReadContentResult": ReadContentResult,
    "ReadImageResult": ReadImageResult,
    "AddTextResult": AddTextResult,
    "EditTextResult": EditTextResult,
    "DeleteTextResult": DeleteTextResult,
    "AddImageResult": AddImageResult,
    "ModifyImageResult": ModifyImageResult,
    "FormatResult": FormatResult,
    "HeaderFooterResult": HeaderFooterResult,
    "PageMarginsResult": PageMarginsResult,
    "PageOrientationResult": PageOrientationResult,
    "CommentsResult": CommentsResult,
}


async def docs_schema(request: SchemaInput) -> SchemaOutput:
    """Get JSON schema for docs input/output models."""
    if request.model not in SCHEMAS:
        available = ", ".join(sorted(SCHEMAS.keys()))
        return SchemaOutput(
            model=request.model,
            json_schema={"error": f"Unknown model. Available: {available}"},
        )
    return SchemaOutput(
        model=request.model,
        json_schema=SCHEMAS[request.model].model_json_schema(),
    )
