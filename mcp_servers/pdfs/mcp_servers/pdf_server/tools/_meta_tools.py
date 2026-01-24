"""Meta-tools for LLM agents - consolidated interface with action-based routing."""

from typing import Any, Literal

from fastmcp.utilities.types import Image
from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import ConfigDict, Field

# Import existing tools for delegation
from tools.create_pdf import create_pdf as _create_pdf
from tools.read_image import read_image as _read_image
from tools.read_page_as_image import read_page_as_image as _read_page_as_image
from tools.read_pdf_pages import read_pdf_pages as _read_pdf_pages
from tools.search_pdf import search_pdf as _search_pdf


# ============ Error Detection ============
def _is_create_error(result: str) -> bool:
    """Check if create_pdf result indicates an error.

    Only checks for error patterns at the START of the result to avoid
    false positives from filenames like 'error_report.pdf'.
    """
    # Success format: "PDF {filename} created at {path}"
    if result.startswith("PDF ") and " created at " in result:
        return False
    # Any other result is an error
    return True


def _is_read_pages_error(result: str) -> bool:
    """Check if read_pdf_pages result indicates an error.

    Uses a prefix check to avoid matching error-like text within PDF content.
    Underlying tool returns error strings that start with specific patterns.
    """
    error_prefixes = (
        "File path ",  # "File path must start with /"
        "File not found:",
        "Not a file:",
        "Page ",  # "Page X is out of range"
        "Failed to",
        "Invalid",
        "Path traversal",
    )
    return result.startswith(error_prefixes)


def _is_search_error(result: str) -> bool:
    """Check if search_pdf result indicates an error."""
    error_prefixes = (
        "File path ",
        "File not found:",
        "Not a file:",
        "Search failed:",
        "Query ",  # "Query is required"
    )
    return result.startswith(error_prefixes)


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
    """Result from creating a PDF."""

    model_config = ConfigDict(extra="forbid")
    status: str
    file_path: str


class ReadPagesResult(BaseModel):
    """Result from reading PDF pages."""

    model_config = ConfigDict(extra="forbid")
    raw_output: str = Field(..., description="PDF content output")


class SearchResult(BaseModel):
    """Result from searching PDF."""

    model_config = ConfigDict(extra="forbid")
    raw_output: str = Field(..., description="Search results output")


# ============ Input Model ============
class PdfInput(BaseModel):
    """Input for pdf meta-tool."""

    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "help",
        "create",
        "read_pages",
        "read_image",
        "page_as_image",
        "search",
    ] = Field(..., description="Action to perform")

    # File operations
    file_path: str | None = Field(
        None,
        description="""Absolute path to PDF file. REQUIRED for read_pages, read_image, page_as_image, search.

Path MUST start with '/'. Files are at root, e.g., '/report.pdf', '/data.pdf'.

WRONG paths (will fail):
- 'report.pdf' (missing leading /)
- '/tmp/report.pdf' (/tmp doesn't exist)
- '/mnt/data/report.pdf' (OpenAI sandbox path - not supported)
- 'https://example.com/file.pdf' (URLs not supported - use filesystem paths)

CORRECT: '/report.pdf', '/my_file.pdf'""",
    )
    directory: str | None = Field(
        None,
        description="Directory for 'create' action. Use '/' for root. REQUIRED for create.",
    )
    file_name: str | None = Field(
        None,
        description="File name for 'create' action (e.g., 'report.pdf'). REQUIRED for create.",
    )

    # Content blocks for create
    content: list[dict[str, Any]] | None = Field(
        None,
        description="Content blocks for 'create': [{type, text, ...}]",
    )
    metadata: dict[str, Any] | None = Field(
        None, description="PDF metadata for 'create': {title?, author?, ...}"
    )
    page_size: str | None = Field(
        None, description="Page size for 'create': 'letter' or 'a4'"
    )

    # Read options
    pages: list[int] | None = Field(
        None, description="Page numbers to read (1-indexed), None for all"
    )
    page_number: int | None = Field(
        None, description="Page number for 'page_as_image' (1-indexed)"
    )
    annotation: str | None = Field(
        None, description="Image annotation key for 'read_image'"
    )

    # Search options
    query: str | None = Field(
        None, description="Search text. Matches names, descriptions. Case-insensitive."
    )
    case_sensitive: bool | None = Field(
        None, description="Case-sensitive search (default: False)"
    )
    whole_documents: bool | None = Field(
        None, description="Match whole words only (default: False)"
    )
    max_results: int | None = Field(
        None, description="Maximum results to return (default: 100)"
    )
    context_chars: int | None = Field(
        None, description="Context characters around match (default: 50)"
    )


# ============ Output Model ============
class PdfOutput(BaseModel):
    """Output for pdf meta-tool (non-image actions)."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(
        ...,
        description="The operation to perform. REQUIRED. Call with action='help' first.",
    )
    error: str | None = Field(None, description="Error message if failed")

    # Discovery
    help: HelpResponse | None = None

    # Action-specific results (non-image actions)
    create: CreateResult | None = None
    read_pages: ReadPagesResult | None = None
    search: SearchResult | None = None


# ============ Help Definition ============
PDF_HELP = HelpResponse(
    tool_name="pdf",
    description="PDF operations: create, read, search, and extract images from .pdf files.",
    actions={
        "help": ActionInfo(
            description="List all available actions",
            required_params=[],
            optional_params=[],
        ),
        "create": ActionInfo(
            description="Create a new PDF document",
            required_params=["directory", "file_name", "content"],
            optional_params=["metadata", "page_size"],
        ),
        "read_pages": ActionInfo(
            description="Read PDF pages (text + images + strikethrough)",
            required_params=["file_path"],
            optional_params=["pages"],
        ),
        "read_image": ActionInfo(
            description="Read an extracted image by annotation",
            required_params=["file_path", "annotation"],
            optional_params=[],
        ),
        "page_as_image": ActionInfo(
            description="Render a page as an image",
            required_params=["file_path", "page_number"],
            optional_params=[],
        ),
        "search": ActionInfo(
            description="Search text in PDF (like Ctrl+F)",
            required_params=["file_path", "query"],
            optional_params=[
                "case_sensitive",
                "whole_documents",
                "max_results",
                "context_chars",
            ],
        ),
    },
)


# ============ Meta-Tool Implementation ============
async def pdf(request: PdfInput) -> PdfOutput | Image:
    """PDF operations: create, read, search, and extract images from .pdf files."""
    match request.action:
        case "help":
            return PdfOutput(action="help", help=PDF_HELP)

        case "create":
            if not request.directory or not request.file_name or not request.content:
                return PdfOutput(
                    action="create",
                    error="Required: directory, file_name, content",
                )
            result = await _create_pdf(
                request.directory,
                request.file_name,
                request.content,
                request.metadata,
                request.page_size or "letter",
            )
            if _is_create_error(result):
                return PdfOutput(action="create", error=result)
            return PdfOutput(
                action="create",
                create=CreateResult(
                    status="success",
                    file_path=f"{request.directory.rstrip('/')}/{request.file_name}",
                ),
            )

        case "read_pages":
            if not request.file_path:
                return PdfOutput(action="read_pages", error="Required: file_path")
            result = await _read_pdf_pages(request.file_path, request.pages)
            # Result is a string - check if it's an error by prefix matching
            # to avoid false positives from PDF content containing error-like text
            if _is_read_pages_error(result):
                return PdfOutput(action="read_pages", error=result)
            return PdfOutput(
                action="read_pages", read_pages=ReadPagesResult(raw_output=result)
            )

        case "read_image":
            if not request.file_path or not request.annotation:
                return PdfOutput(
                    action="read_image", error="Required: file_path, annotation"
                )
            try:
                # Return the Image directly - FastMCP handles image serialization
                image = await _read_image(request.file_path, request.annotation)
                return image
            except Exception as exc:
                return PdfOutput(action="read_image", error=str(exc))

        case "page_as_image":
            if request.file_path is None or request.page_number is None:
                return PdfOutput(
                    action="page_as_image",
                    error="Required: file_path, page_number",
                )
            result = await _read_page_as_image(request.file_path, request.page_number)
            # _read_page_as_image returns str on error, Image on success
            if isinstance(result, str):
                return PdfOutput(action="page_as_image", error=result)
            # Return the Image directly - FastMCP handles image serialization
            return result

        case "search":
            if not request.file_path or not request.query:
                return PdfOutput(action="search", error="Required: file_path, query")
            result = await _search_pdf(
                request.file_path,
                request.query,
                request.case_sensitive if request.case_sensitive is not None else False,
                request.whole_documents if request.whole_documents is not None else False,
                request.max_results if request.max_results is not None else 100,
                request.context_chars if request.context_chars is not None else 50,
            )
            result_str = str(result)
            if _is_search_error(result_str):
                return PdfOutput(action="search", error=result_str)
            return PdfOutput(
                action="search", search=SearchResult(raw_output=result_str)
            )

        case _:
            return PdfOutput(
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
    "input": PdfInput,
    "output": PdfOutput,
    "CreateResult": CreateResult,
    "ReadPagesResult": ReadPagesResult,
    "SearchResult": SearchResult,
}


async def pdf_schema(request: SchemaInput) -> SchemaOutput:
    """Get JSON schema for pdf input/output models."""
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
