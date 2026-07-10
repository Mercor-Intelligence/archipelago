"""Meta-tools for LLM agents - consolidated interface with action-based routing."""

import base64
from typing import Annotated, Any, Literal

from mcp_schema import GeminiBaseModel
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

# Import existing tools for delegation
from tools.add_content_text import add_content_text as _add_content_text
from tools.add_image import AddImageInput
from tools.add_image import add_image as _add_image
from tools.apply_formatting import ApplyFormattingInput
from tools.apply_formatting import apply_formatting as _apply_formatting
from tools.comments import CommentsInput
from tools.comments import comments as _comments
from tools.create_document import (
    ContentBlock,
    CreateDocumentInput,
    DocumentMetadata,
)
from tools.create_document import (
    create_document as _create_document,
)
from tools.delete_content_text import delete_content_text as _delete_content_text
from tools.delete_document import delete_document as _delete_document
from tools.edit_content_text import edit_content_text as _edit_content_text
from tools.get_document_overview import get_document_overview as _get_document_overview
from tools.header_footer import HeaderFooterInput
from tools.header_footer import header_footer as _header_footer
from tools.modify_image import ModifyImageInput
from tools.modify_image import modify_image as _modify_image
from tools.page_margins import PageMarginsInput
from tools.page_margins import page_margins as _page_margins
from tools.page_orientation import PageOrientationInput
from tools.page_orientation import page_orientation as _page_orientation
from tools.read_document_content import ReadDocumentContentInput
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
class ActionInfo(GeminiBaseModel):
    """Information about an action."""

    description: str
    required_params: list[str]
    optional_params: list[str]


class HelpResponse(GeminiBaseModel):
    """Help response listing available actions."""

    tool_name: str
    description: str
    actions: dict[str, ActionInfo]


# ============ Result Models ============
# All output models inherit from GeminiBaseModel so their JSON schemas are flat
# (no $defs/$ref/anyOf) — required for GDM's sanitize_schema to accept them.
# All fields are required (no Optional[X] = None) so the flat schema does not
# need a `nullable: true` annotation that mcp's jsonschema.validate would reject.


class CreateResult(GeminiBaseModel):
    """Result from creating a document."""

    status: str
    file_path: str


class DeleteResult(GeminiBaseModel):
    """Result from deleting a document."""

    status: str
    file_path: str


class OverviewResult(GeminiBaseModel):
    """Result from getting document overview."""

    raw_output: str = Field(..., description="Document structure output")


class ReadContentResult(GeminiBaseModel):
    """Result from reading document content."""

    raw_output: str = Field(..., description="Document content output")


class ReadImageResult(GeminiBaseModel):
    """Result from reading an image."""

    status: str
    message: str
    image_data: str = Field(..., description="Base64-encoded JPEG image data")
    image_format: str = Field(..., description="Image format (e.g., 'jpeg')")


class AddTextResult(GeminiBaseModel):
    """Result from adding text content."""

    status: str
    file_path: str
    identifier: str
    position: str


class EditTextResult(GeminiBaseModel):
    """Result from editing text content."""

    status: str
    file_path: str
    identifier: str


class DeleteTextResult(GeminiBaseModel):
    """Result from deleting text content."""

    status: str
    file_path: str
    identifier: str


class AddImageResult(GeminiBaseModel):
    """Result from adding an image."""

    status: str
    message: str


class ModifyImageResult(GeminiBaseModel):
    """Result from modifying an image."""

    status: str
    message: str


class FormatResult(GeminiBaseModel):
    """Result from applying formatting."""

    status: str
    file_path: str
    identifier: str
    applied: dict[str, Any]


class HeaderFooterResult(GeminiBaseModel):
    """Result from header/footer operations.

    `raw_output` is populated only for hf_action='read'; empty string for set/clear/link.
    """

    status: str
    file_path: str
    area: str
    section_index: int
    hf_action: str
    raw_output: str = Field(
        "", description="Raw output for read action; empty otherwise"
    )


class PageMarginsResult(GeminiBaseModel):
    """Result from page margins operations.

    `raw_output` is populated only for pm_action='read'; empty string for set.
    """

    status: str
    file_path: str
    section_index: int
    pm_action: str
    raw_output: str = Field(
        "", description="Raw output for read action; empty otherwise"
    )


class PageOrientationResult(GeminiBaseModel):
    """Result from page orientation operations.

    `raw_output` is populated only for po_action='read'; empty string for set.
    """

    status: str
    file_path: str
    section_index: int
    po_action: str
    raw_output: str = Field(
        "", description="Raw output for read action; empty otherwise"
    )


class CommentsResult(GeminiBaseModel):
    """Result from comments operations.

    `raw_output` is populated only for comments_action='read'; empty string otherwise.
    """

    status: str
    file_path: str
    comments_action: str
    raw_output: str = Field(
        "", description="Raw output for read action; empty otherwise"
    )


# ============ Input Model ============
class DocsInput(GeminiBaseModel):
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
        None,
        description="Full file path. REQUIRED for file operations. Must start with '/' and end with '.docx'.",
    )
    directory: str | None = Field(
        None,
        description="Directory for 'create' (e.g., '/'). Must be provided for 'create' action. Must start with '/'.",
    )
    file_name: str | None = Field(
        None,
        description="File name for 'create' (e.g., 'report.docx'). Must be provided for 'create' action. Must end with '.docx' and not contain '/'.",
    )

    # Content blocks for create
    content: list[dict[str, Any]] | None = Field(
        None,
        description=(
            "Content blocks for actions that populate document content ('create', 'header_footer'). "
            "Must be provided for 'create' action. "
            "REQUIRED when hf_action='set' for 'header_footer' action. "
            "Each block needs a 'type' field. Types: "
            "paragraph ({type: 'paragraph', text: 'Your text'}), "
            "heading ({type: 'heading', text: 'Title', level: 1}), "
            "bullet_list ({type: 'bullet_list', items: ['A', 'B']}), "
            "numbered_list ({type: 'numbered_list', items: ['1st', '2nd']}), "
            "table ({type: 'table', rows: [['H1', 'H2'], ['C1', 'C2']], header: true}), "
            "page_break ({type: 'page_break'}), "
            "section_break ({type: 'section_break'}). "
            "Note: 'text' fields must not be empty; 'items' lists must not be empty. "
            "For 'header_footer' action only: 'page_break' and 'section_break' blocks are not supported; 'table' blocks require a 'width' field (in inches). "
            "Example: [{type: 'heading', text: 'My Doc', level: 1}, {type: 'paragraph', text: 'Hello'}]"
        ),
    )
    metadata: dict[str, Any] | None = Field(
        None, description="Document metadata for 'create': {title?, author?, ...}"
    )

    # Content operations
    identifier: str | None = Field(
        None,
        description="Obtained from read_document_content output. Target element identifier. Must be provided for 'add_text', 'edit_text', 'delete_text', 'add_image', 'format', and comments 'add' actions. Format depends on action: For text operations (add_text, edit_text, delete_text): paragraph ID (e.g., 'body.p.3'), run ID (e.g., 'body.p.3.r.1'), or cell ID (e.g., 'body.tbl.1.row.2.cell.3.p.0'). For add_image: paragraph ID or run ID. For format: run, paragraph, or cell ID. For comments 'add': run, paragraph, or cell ID to attach comment to.",
    )
    text: str | None = Field(None, description="Text content for add_text")
    new_text: str | None = Field(None, description="Replacement text for edit_text")
    position: str | None = Field(
        None,
        description="Position for add_text/add_image: 'start' or 'end'. 'before' and 'after' are accepted as aliases for 'start' and 'end'.",
    )
    scope: str | None = Field(
        None,
        description="Scope for delete_text: 'content' or 'element'. Note: 'element' scope is not supported for cell identifiers; use 'content' to clear cell contents.",
    )
    collapse_whitespace: bool | None = Field(
        None, description="Collapse whitespace for delete_text in cells"
    )

    # Read options
    page_index: int | None = Field(
        None,
        description=(
            "For read_content only: 0-based page for reading large docs in chunks. "
            "page_index=0 is paragraphs 0-49, page_index=1 is 50-99, etc. "
            "Omit to read entire document."
        ),
    )
    section_index: int | None = Field(
        None,
        description=(
            "ONLY for page_margins, page_orientation, and header_footer actions. "
            "0-based index into Word page-layout sections (controls margins, headers, orientation). "
            "Do NOT pass this for read_content — it will be rejected. "
            "To paginate read_content, use page_index instead."
        ),
    )
    annotation: str | None = Field(
        None,
        description=(
            "Image annotation key for read_image. Obtained from read_content output. "
            "Format: 'body_p_N_rM' where N is paragraph index and M is run index. "
            "Example: 'body_p_0_r0' for first image in first paragraph. "
            "The '@' prefix sometimes seen in read_content output is optional and will be stripped."
        ),
    )

    # Image operations
    image_path: str | None = Field(
        None,
        description="Path to image file for add_image. Must be provided for 'add_image' action. Must start with '/'. Supported formats: .jpg, .jpeg, .png.",
    )
    image_index: int | None = Field(
        None,
        description="0-based image index for modify_image. Obtain from read_document_content output which lists images with their indices.",
    )
    operation: str | None = Field(
        None,
        description="Operation for modify_image: rotate, flip, brightness, contrast",
    )
    rotation: int | None = Field(
        None,
        description="Rotation angle in degrees. Valid range: 0-360. Must be provided for 'modify_image' when operation is 'rotate'.",
    )
    flip: str | None = Field(
        None,
        description="Flip direction: 'horizontal' or 'vertical'. Must be provided for 'modify_image' when operation is 'flip'.",
    )
    brightness: float | None = Field(
        None,
        description="Brightness factor. Must be positive (e.g., 0.5=darker, 1.0=unchanged, 2.0=brighter). Must be provided for 'modify_image' when operation is 'brightness'.",
    )
    contrast: float | None = Field(
        None,
        description="Contrast factor. Must be positive (e.g., 0.5=less contrast, 1.0=unchanged, 2.0=more contrast). Must be provided for 'modify_image' when operation is 'contrast'.",
    )
    width: float | None = Field(
        None, description="Width in inches. Optional for add_image."
    )
    height: float | None = Field(
        None, description="Height in inches. Optional for add_image."
    )

    # Formatting
    bold: bool | None = Field(
        None,
        description="True to enable, False to disable, null/omit to leave unchanged. For 'format' action, at least one formatting parameter must be provided.",
    )
    italic: bool | None = Field(
        None,
        description="True to enable, False to disable, null/omit to leave unchanged. For 'format' action, at least one formatting parameter must be provided.",
    )
    underline: bool | None = Field(
        None,
        description="True to enable, False to disable, null/omit to leave unchanged. For 'format' action, at least one formatting parameter must be provided.",
    )
    strikethrough: bool | None = Field(
        None,
        description="True to enable, False to disable, null/omit to leave unchanged. For 'format' action, at least one formatting parameter must be provided.",
    )
    font_size: float | None = Field(
        None,
        description="Font size in points (e.g., 12, 14, 24). Null/omit to leave unchanged. For 'format' action, at least one formatting parameter must be provided.",
    )
    font_color: str | None = Field(
        None,
        description="Hex color code (e.g., 'FF0000' for red, '0000FF' for blue). Null/omit to leave unchanged. For 'format' action, at least one formatting parameter must be provided.",
    )

    # Header/Footer operations
    hf_action: str | None = Field(
        None, description="Header/footer action: 'read', 'set', 'clear', or 'link'"
    )
    area: str | None = Field(
        None, description="Header/footer area: 'header' or 'footer'"
    )
    link_to_previous: bool | None = Field(
        None,
        description="For 'link' hf_action: True to link, False to unlink. Note: Section 0 cannot be linked to a previous section.",
    )

    # Page Margins operations
    pm_action: str | None = Field(
        None, description="Page margins action: 'read' or 'set'"
    )
    margin_top: float | None = Field(
        None,
        description="Top margin in inches. Must be non-negative. For 'page_margins' set action, at least one margin must be provided.",
    )
    margin_bottom: float | None = Field(
        None,
        description="Bottom margin in inches. Must be non-negative. For 'page_margins' set action, at least one margin must be provided.",
    )
    margin_left: float | None = Field(
        None,
        description="Left margin in inches. Must be non-negative. For 'page_margins' set action, at least one margin must be provided.",
    )
    margin_right: float | None = Field(
        None,
        description="Right margin in inches. Must be non-negative. For 'page_margins' set action, at least one margin must be provided.",
    )

    # Page Orientation operations
    po_action: str | None = Field(
        None, description="Page orientation action: 'read' or 'set'"
    )
    orientation: str | None = Field(
        None,
        description="Page orientation: 'portrait' or 'landscape'. Must be provided for 'page_orientation' with po_action='set'.",
    )

    # Comments operations
    comments_action: str | None = Field(
        None,
        description="Comments action: 'read', 'add', or 'delete'. For 'add': requires identifier and comment_text. For 'delete': requires comment_id.",
    )
    comment_text: str | None = Field(
        None, description="Comment text for 'add' action. Must not be empty."
    )
    comment_author: str | None = Field(
        None, description="Comment author for 'add' action"
    )
    comment_id: int | None = Field(
        None,
        description="Comment ID for 'delete' action. Must be an existing comment ID from 'read' action output.",
    )


# ============ Output Variants (Discriminated Union) ============
# Each variant carries exactly the fields it needs — no Optional/None patterns.
# `action` is the discriminator. `DocsError.action="error"` is synthetic; the
# original requested action is preserved in `requested_action`.


class DocsHelp(GeminiBaseModel):
    """Output when action='help'."""

    action: Literal["help"] = "help"
    help: HelpResponse


class DocsCreate(GeminiBaseModel):
    """Output when action='create' succeeds."""

    action: Literal["create"] = "create"
    create: CreateResult


class DocsDelete(GeminiBaseModel):
    """Output when action='delete' succeeds."""

    action: Literal["delete"] = "delete"
    delete: DeleteResult


class DocsOverview(GeminiBaseModel):
    """Output when action='overview' succeeds."""

    action: Literal["overview"] = "overview"
    overview: OverviewResult


class DocsReadContent(GeminiBaseModel):
    """Output when action='read_content' succeeds."""

    action: Literal["read_content"] = "read_content"
    read_content: ReadContentResult


class DocsReadImage(GeminiBaseModel):
    """Output when action='read_image' succeeds."""

    action: Literal["read_image"] = "read_image"
    read_image: ReadImageResult


class DocsAddText(GeminiBaseModel):
    """Output when action='add_text' succeeds."""

    action: Literal["add_text"] = "add_text"
    add_text: AddTextResult


class DocsEditText(GeminiBaseModel):
    """Output when action='edit_text' succeeds."""

    action: Literal["edit_text"] = "edit_text"
    edit_text: EditTextResult


class DocsDeleteText(GeminiBaseModel):
    """Output when action='delete_text' succeeds."""

    action: Literal["delete_text"] = "delete_text"
    delete_text: DeleteTextResult


class DocsAddImage(GeminiBaseModel):
    """Output when action='add_image' succeeds."""

    action: Literal["add_image"] = "add_image"
    add_image: AddImageResult


class DocsModifyImage(GeminiBaseModel):
    """Output when action='modify_image' succeeds."""

    action: Literal["modify_image"] = "modify_image"
    modify_image: ModifyImageResult


class DocsFormat(GeminiBaseModel):
    """Output when action='format' succeeds."""

    action: Literal["format"] = "format"
    format: FormatResult


class DocsHeaderFooter(GeminiBaseModel):
    """Output when action='header_footer' succeeds."""

    action: Literal["header_footer"] = "header_footer"
    header_footer: HeaderFooterResult


class DocsPageMargins(GeminiBaseModel):
    """Output when action='page_margins' succeeds."""

    action: Literal["page_margins"] = "page_margins"
    page_margins: PageMarginsResult


class DocsPageOrientation(GeminiBaseModel):
    """Output when action='page_orientation' succeeds."""

    action: Literal["page_orientation"] = "page_orientation"
    page_orientation: PageOrientationResult


class DocsComments(GeminiBaseModel):
    """Output when action='comments' succeeds."""

    action: Literal["comments"] = "comments"
    comments: CommentsResult


class DocsError(GeminiBaseModel):
    """Output when an action fails."""

    action: Literal["error"] = "error"
    requested_action: str = Field(
        ..., description="The action that was requested but failed"
    )
    error: str = Field(..., description="Error message")


DocsOutput = Annotated[
    DocsHelp
    | DocsCreate
    | DocsDelete
    | DocsOverview
    | DocsReadContent
    | DocsReadImage
    | DocsAddText
    | DocsEditText
    | DocsDeleteText
    | DocsAddImage
    | DocsModifyImage
    | DocsFormat
    | DocsHeaderFooter
    | DocsPageMargins
    | DocsPageOrientation
    | DocsComments
    | DocsError,
    Field(discriminator="action"),
]


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
            description="Get heading structure and total_pages for pagination planning",
            required_params=["file_path"],
            optional_params=[],
        ),
        "read_content": ActionInfo(
            description="Read document with element IDs. Use page_index for large docs (0=first 50 paragraphs).",
            required_params=["file_path"],
            optional_params=["page_index"],
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
            description="Apply text formatting. At least one formatting parameter must be provided.",
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
            description="Read, set, clear, or link headers/footers. For hf_action='set', content blocks are required. Header/footer restrictions: 'page_break' and 'section_break' blocks are NOT supported; 'table' blocks REQUIRE a 'width' field (in inches).",
            required_params=["file_path", "hf_action", "area"],
            optional_params=["section_index", "content", "link_to_previous"],
        ),
        "page_margins": ActionInfo(
            description="Read or set page margins. For pm_action='set', at least one margin (margin_top, margin_bottom, margin_left, margin_right) is required.",
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
            description="Read or set page orientation (portrait/landscape). For po_action='set', orientation must be provided.",
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


def _err(action: str, message: str) -> DocsError:
    """Build a DocsError for a failed action."""
    return DocsError(requested_action=action, error=message)


# ============ Meta-Tool Implementation ============
async def docs(request: DocsInput) -> DocsOutput:
    """Manage .docx documents: create, read, edit text/images, format, headers/footers, margins.

    Actions: help | create | delete | overview | read_content | read_image |
             add_text | edit_text | delete_text | add_image | modify_image |
             format | header_footer | page_margins | page_orientation | comments

    Workflow: read_content to get identifiers (e.g., 'body.p.0') -> use them for edits.
    Paths must start with '/' and end with '.docx' (e.g., '/docs/report.docx').
    Call action='help' for full parameter details.

    Returns one of seventeen variants discriminated by `action` (or `action='error'`
    for failures, with `requested_action` carrying the original action).
    """
    match request.action:
        case "help":
            return DocsHelp(help=DOCS_HELP)

        case "create":
            if not request.directory or not request.file_name or not request.content:
                return _err("create", "Required: directory, file_name, content")
            try:
                validated_content = _CONTENT_ADAPTER.validate_python(request.content)
                validated_metadata = _METADATA_ADAPTER.validate_python(request.metadata)
            except ValidationError as exc:
                return _err("create", f"Invalid input: {exc}")
            result = await _create_document(
                CreateDocumentInput(
                    directory=request.directory,
                    file_name=request.file_name,
                    content=validated_content,
                    metadata=validated_metadata,
                )
            )
            if not _is_create_success(result):
                return _err("create", result)
            return DocsCreate(
                create=CreateResult(
                    status="success",
                    file_path=f"{request.directory.rstrip('/')}/{request.file_name}",
                ),
            )

        case "delete":
            if not request.file_path:
                return _err("delete", "Required: file_path")
            result = await _delete_document(request.file_path)
            if not _is_delete_success(result):
                return _err("delete", result)
            return DocsDelete(
                delete=DeleteResult(status="success", file_path=request.file_path),
            )

        case "overview":
            if not request.file_path:
                return _err("overview", "Required: file_path")
            result = await _get_document_overview(request.file_path)
            if not _is_pydantic_success(result):
                return _err("overview", result)
            return DocsOverview(overview=OverviewResult(raw_output=result))

        case "read_content":
            if not request.file_path:
                return _err("read_content", "Required: file_path")
            if request.section_index is not None:
                return _err(
                    "read_content",
                    "section_index is not supported for read_content. Use page_index for pagination (e.g. page_index=0 for first 50 paragraphs).",
                )
            result = await _read_document_content(
                ReadDocumentContentInput(
                    file_path=request.file_path, page_index=request.page_index
                )
            )
            if not _is_pydantic_success(result):
                return _err("read_content", result)
            return DocsReadContent(read_content=ReadContentResult(raw_output=result))

        case "read_image":
            if not request.file_path or not request.annotation:
                return _err("read_image", "Required: file_path, annotation")
            try:
                image = await _read_image(request.file_path, request.annotation)
                if image.data is None:
                    return _err("read_image", "Image data is empty")
                image_b64 = base64.b64encode(image.data).decode("utf-8")
                return DocsReadImage(
                    read_image=ReadImageResult(
                        status="success",
                        message=f"Image retrieved: {request.annotation}",
                        image_data=image_b64,
                        image_format="jpeg",  # Always jpeg - see read_image.py
                    ),
                )
            except Exception as exc:
                return _err("read_image", str(exc))

        case "add_text":
            if not request.file_path or not request.identifier or not request.text:
                return _err("add_text", "Required: file_path, identifier, text")
            result = await _add_content_text(
                request.file_path,
                request.identifier,
                request.text,
                request.position or "end",
            )
            if not _is_pydantic_success(result):
                return _err("add_text", result)
            return DocsAddText(
                add_text=AddTextResult(
                    status="success",
                    file_path=request.file_path,
                    identifier=request.identifier,
                    position=request.position or "end",
                ),
            )

        case "edit_text":
            if (
                not request.file_path
                or not request.identifier
                or request.new_text is None
            ):
                return _err("edit_text", "Required: file_path, identifier, new_text")
            result = await _edit_content_text(
                request.file_path, request.identifier, request.new_text
            )
            if not _is_pydantic_success(result):
                return _err("edit_text", result)
            return DocsEditText(
                edit_text=EditTextResult(
                    status="success",
                    file_path=request.file_path,
                    identifier=request.identifier,
                ),
            )

        case "delete_text":
            if not request.file_path or not request.identifier:
                return _err("delete_text", "Required: file_path, identifier")
            result = await _delete_content_text(
                request.file_path,
                request.identifier,
                request.scope or "content",
                request.collapse_whitespace
                if request.collapse_whitespace is not None
                else False,
            )
            if not _is_pydantic_success(result):
                return _err("delete_text", result)
            return DocsDeleteText(
                delete_text=DeleteTextResult(
                    status="success",
                    file_path=request.file_path,
                    identifier=request.identifier,
                ),
            )

        case "add_image":
            if (
                not request.file_path
                or not request.image_path
                or not request.identifier
            ):
                return _err("add_image", "Required: file_path, image_path, identifier")
            result = await _add_image(
                AddImageInput(
                    file_path=request.file_path,
                    image_path=request.image_path,
                    identifier=request.identifier,
                    position=request.position or "end",
                    width=request.width,
                    height=request.height,
                )
            )
            if not _is_add_image_success(result):
                return _err("add_image", result)
            return DocsAddImage(
                add_image=AddImageResult(status="success", message=result),
            )

        case "modify_image":
            if (
                not request.file_path
                or request.image_index is None
                or not request.operation
            ):
                return _err(
                    "modify_image", "Required: file_path, image_index, operation"
                )
            result = await _modify_image(
                ModifyImageInput(
                    file_path=request.file_path,
                    image_index=request.image_index,
                    operation=request.operation,
                    rotation=request.rotation,
                    flip=request.flip,
                    brightness=request.brightness,
                    contrast=request.contrast,
                )
            )
            if not _is_modify_image_success(result):
                return _err("modify_image", result)
            return DocsModifyImage(
                modify_image=ModifyImageResult(status="success", message=result),
            )

        case "format":
            if not request.file_path or not request.identifier:
                return _err("format", "Required: file_path, identifier")
            result = await _apply_formatting(
                ApplyFormattingInput(
                    file_path=request.file_path,
                    identifier=request.identifier,
                    bold=request.bold,
                    italic=request.italic,
                    underline=request.underline,
                    strikethrough=request.strikethrough,
                    font_size=request.font_size,
                    font_color=request.font_color,
                )
            )
            if not _is_pydantic_success(result):
                return _err("format", result)

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

            return DocsFormat(
                format=FormatResult(
                    status="success",
                    file_path=request.file_path,
                    identifier=request.identifier,
                    applied=applied,
                ),
            )

        case "header_footer":
            if not request.file_path or not request.hf_action or not request.area:
                return _err("header_footer", "Required: file_path, hf_action, area")
            section_index = (
                request.section_index if request.section_index is not None else 0
            )
            result = await _header_footer(
                HeaderFooterInput(
                    file_path=request.file_path,
                    action=request.hf_action,
                    area=request.area,
                    section_index=section_index,
                    content=request.content,
                    link_to_previous=request.link_to_previous,
                )
            )
            if not _is_header_footer_success(result):
                return _err("header_footer", result)
            return DocsHeaderFooter(
                header_footer=HeaderFooterResult(
                    status="success",
                    file_path=request.file_path,
                    area=request.area,
                    section_index=section_index,
                    hf_action=request.hf_action,
                    raw_output=result if request.hf_action == "read" else "",
                ),
            )

        case "page_margins":
            if not request.file_path or not request.pm_action:
                return _err("page_margins", "Required: file_path, pm_action")
            section_index = (
                request.section_index if request.section_index is not None else 0
            )
            result = await _page_margins(
                PageMarginsInput(
                    file_path=request.file_path,
                    action=request.pm_action,
                    section_index=section_index,
                    top=request.margin_top,
                    bottom=request.margin_bottom,
                    left=request.margin_left,
                    right=request.margin_right,
                )
            )
            if not _is_page_margins_success(result):
                return _err("page_margins", result)
            return DocsPageMargins(
                page_margins=PageMarginsResult(
                    status="success",
                    file_path=request.file_path,
                    section_index=section_index,
                    pm_action=request.pm_action,
                    raw_output=result if request.pm_action == "read" else "",
                ),
            )

        case "page_orientation":
            if not request.file_path or not request.po_action:
                return _err("page_orientation", "Required: file_path, po_action")
            section_index = (
                request.section_index if request.section_index is not None else 0
            )
            result = await _page_orientation(
                PageOrientationInput(
                    file_path=request.file_path,
                    action=request.po_action,
                    section_index=section_index,
                    orientation=request.orientation,
                )
            )
            if not _is_page_orientation_success(result):
                return _err("page_orientation", result)
            return DocsPageOrientation(
                page_orientation=PageOrientationResult(
                    status="success",
                    file_path=request.file_path,
                    section_index=section_index,
                    po_action=request.po_action,
                    raw_output=result if request.po_action == "read" else "",
                ),
            )

        case "comments":
            if not request.file_path or not request.comments_action:
                return _err("comments", "Required: file_path, comments_action")
            result = await _comments(
                CommentsInput(
                    file_path=request.file_path,
                    action=request.comments_action,
                    identifier=request.identifier,
                    text=request.comment_text,
                    author=request.comment_author,
                    comment_id=request.comment_id,
                )
            )
            if not _is_comments_success(result):
                return _err("comments", result)
            return DocsComments(
                comments=CommentsResult(
                    status="success",
                    file_path=request.file_path,
                    comments_action=request.comments_action,
                    raw_output=result if request.comments_action == "read" else "",
                ),
            )

        case _:
            return _err(request.action, f"Unknown action: {request.action}")


# ============ Schema Tool ============
class SchemaInput(GeminiBaseModel):
    """Input for schema introspection."""

    model_config = ConfigDict(extra="forbid")
    model: str = Field(
        ...,
        description="Model name: 'input', 'output', a variant name (e.g. 'DocsCreate'), or a result type",
    )


class SchemaOutput(GeminiBaseModel):
    """Output for schema introspection."""

    model: str
    json_schema: dict[str, Any]


_DOCS_OUTPUT_ADAPTER: TypeAdapter[Any] = TypeAdapter(DocsOutput)

# Map of named models exposed by the schema tool. "output" returns the full
# discriminated-union schema via TypeAdapter; everything else is a concrete model.
SCHEMAS: dict[str, type[BaseModel]] = {
    "input": DocsInput,
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
    "DocsHelp": DocsHelp,
    "DocsCreate": DocsCreate,
    "DocsDelete": DocsDelete,
    "DocsOverview": DocsOverview,
    "DocsReadContent": DocsReadContent,
    "DocsReadImage": DocsReadImage,
    "DocsAddText": DocsAddText,
    "DocsEditText": DocsEditText,
    "DocsDeleteText": DocsDeleteText,
    "DocsAddImage": DocsAddImage,
    "DocsModifyImage": DocsModifyImage,
    "DocsFormat": DocsFormat,
    "DocsHeaderFooter": DocsHeaderFooter,
    "DocsPageMargins": DocsPageMargins,
    "DocsPageOrientation": DocsPageOrientation,
    "DocsComments": DocsComments,
    "DocsError": DocsError,
}


async def docs_schema(request: SchemaInput) -> SchemaOutput:
    """Return JSON schema for docs meta-tool models and result payload shapes."""
    if request.model == "output":
        return SchemaOutput(
            model="output",
            json_schema=_DOCS_OUTPUT_ADAPTER.json_schema(mode="serialization"),
        )
    if request.model not in SCHEMAS:
        available = ", ".join(sorted(["output", *SCHEMAS.keys()]))
        return SchemaOutput(
            model=request.model,
            json_schema={"error": f"Unknown model. Available: {available}"},
        )
    return SchemaOutput(
        model=request.model,
        json_schema=SCHEMAS[request.model].model_json_schema(),
    )
