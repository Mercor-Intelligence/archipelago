"""Meta-tools for LLM agents - consolidated interface with action-based routing."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Import existing tools for delegation
from tools.add_content_text import add_content_text as _add_content_text
from tools.add_tab import add_tab as _add_tab
from tools.create_chart import create_chart as _create_chart
from tools.create_spreadsheet import create_spreadsheet as _create_spreadsheet
from tools.delete_content_cell import delete_content_cell as _delete_content_cell
from tools.delete_spreadsheet import delete_spreadsheet as _delete_spreadsheet
from tools.delete_tab import delete_tab as _delete_tab
from tools.edit_spreadsheet import edit_spreadsheet as _edit_spreadsheet
from tools.list_tabs_in_spreadsheet import (
    list_tabs_in_spreadsheet as _list_tabs_in_spreadsheet,
)
from tools.read_csv import read_csv as _read_csv
from tools.read_tab import read_tab as _read_tab

# ============ Error Detection ============
# Use specific success markers or prefix checks to avoid false positives from
# spreadsheet cell content or user-controlled names (e.g., "Created Data" sheet,
# "Added Items" tab) that might contain success-like strings.


def _is_status_error(result: str) -> bool:
    """Check if a Pydantic response indicates an error.

    Success format: "{'status': 'success', ...}"
    Used for create, delete, add_tab, delete_tab operations that return
    structured responses with a status field.

    Uses startswith to avoid false negatives from user content like
    tab names containing "'status': 'success'" which would otherwise
    match substring check in error messages like:
    "Tab 'status': 'success' already exists..."
    """
    # Success responses from Pydantic __str__ always start with "{'status': 'success'"
    # Error messages never start with "{" - they're plain strings
    return not result.startswith("{'status': 'success'")


def _is_read_error(result: str) -> bool:
    """Check if read_tab/read_csv/list_tabs result indicates an error.

    Uses prefix checking to avoid false positives from cell content
    like "Invalid email" or "Failed to submit".
    """
    error_prefixes = (
        "File path ",
        "File not found:",
        "Not a file:",
        "Tab index ",  # "Tab index must be...", "Tab index ... is out of range"
        "Delimiter ",
        "Encoding ",
        "Row limit ",
        "Invalid cell",  # "Invalid cell range '...'", "Invalid cell reference '...'"
        "Failed to",  # Covers "Failed to access", "Failed to load", "Failed to decode", "Failed to parse"
        "Unexpected error",  # Covers "Unexpected error:" and "Unexpected error reading CSV:"
    )
    return result.startswith(error_prefixes)


def _is_chart_error(result: str) -> bool:
    """Check if create_chart result indicates an error.

    Success format: "Chart 'name' created in sheet at position POS"
    Uses startswith to avoid false positives from sheet names like
    "Created In Q4" matching "created in".
    """
    # Success messages start with "Chart '" - error messages don't
    return not result.startswith("Chart '")


def _parse_tab_name(result: str, fallback_index: int) -> str:
    """Extract tab_name from DeleteTabResponse string.

    Handles tab names containing single quotes (e.g., "Year's Data") by
    finding the content between "'tab_name': '" and "', 'tab_index':".
    """
    start_marker = "'tab_name': '"
    end_marker = "', 'tab_index':"
    start_idx = result.find(start_marker)
    if start_idx == -1:
        return f"tab_{fallback_index}"
    start_idx += len(start_marker)
    end_idx = result.find(end_marker, start_idx)
    if end_idx == -1:
        return f"tab_{fallback_index}"
    return result[start_idx:end_idx]


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
class ReadTabResult(BaseModel):
    """Result from reading a worksheet tab."""

    model_config = ConfigDict(extra="forbid")
    raw_output: str = Field(..., description="Formatted table output")


class ReadCsvResult(BaseModel):
    """Result from reading a CSV file."""

    model_config = ConfigDict(extra="forbid")
    raw_output: str = Field(..., description="Formatted table output")


class CreateResult(BaseModel):
    """Result from creating a spreadsheet."""

    model_config = ConfigDict(extra="forbid")
    status: str
    file_path: str
    sheets_created: int


class DeleteResult(BaseModel):
    """Result from deleting a file."""

    model_config = ConfigDict(extra="forbid")
    status: str
    file_path: str


class ListTabsResult(BaseModel):
    """Result from listing tabs."""

    model_config = ConfigDict(extra="forbid")
    raw_output: str = Field(..., description="Tab listing output")


class AddTabResult(BaseModel):
    """Result from adding a tab."""

    model_config = ConfigDict(extra="forbid")
    status: str
    tab_name: str
    file_path: str
    rows_added: int | None = None


class DeleteTabResult(BaseModel):
    """Result from deleting a tab."""

    model_config = ConfigDict(extra="forbid")
    status: str
    tab_name: str
    tab_index: int
    file_path: str


class EditResult(BaseModel):
    """Result from edit operations."""

    model_config = ConfigDict(extra="forbid")
    status: str
    file_path: str
    operations_applied: int


class ContentResult(BaseModel):
    """Result from add/delete content operations."""

    model_config = ConfigDict(extra="forbid")
    status: str
    cell: str
    tab_index: int
    file_path: str
    old_value: Any | None = None


class ChartResult(BaseModel):
    """Result from creating a chart."""

    model_config = ConfigDict(extra="forbid")
    message: str


# ============ Input Model ============
class SheetsInput(BaseModel):
    """Input for sheets meta-tool."""

    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "help",
        "create",
        "delete",
        "read_tab",
        "read_csv",
        "list_tabs",
        "add_tab",
        "delete_tab",
        "edit",
        "add_content",
        "delete_content",
        "create_chart",
    ] = Field(..., description="Action to perform")

    # File operations
    file_path: str | None = Field(
        None,
        description="Full file path (e.g., '/report.xlsx'). REQUIRED for all actions except 'create'.",
    )
    directory: str | None = Field(
        None,
        description="Directory path. REQUIRED for 'create' action (e.g., '/'). Use with file_name.",
    )
    file_name: str | None = Field(
        None,
        description="File name with .xlsx extension. REQUIRED for 'create' action (e.g., 'report.xlsx').",
    )

    # Tab operations
    tab_index: int | None = Field(
        None,
        description="0-based tab index. REQUIRED for read_tab, delete_tab, add_content, delete_content. Use 0 for first tab.",
    )
    tab_name: str | None = Field(
        None,
        description="Tab name for 'add_tab' action only. NOT used for read_tab (use tab_index instead).",
    )
    cell_range: str | None = Field(
        None, description="Cell range for 'read_tab' (e.g., 'A1:C5')"
    )

    # Sheet data for create/add_tab
    sheets: list[dict[str, Any]] | None = Field(
        None,
        description="Sheet definitions for 'create'. REQUIRED for create. Format: [{name: 'Sheet1', headers: ['A','B'], rows: [[1,2], [3,4]]}]",
    )
    sheet_data: dict[str, Any] | None = Field(
        None, description="Data for 'add_tab': {headers?, rows}"
    )

    # Edit operations
    operations: list[dict[str, Any]] | None = Field(
        None,
        description="""Operations for 'edit' action. Each operation needs 'type' field.

Valid types (MUST use exact names):
- 'set_cell': Set cell value. {type: 'set_cell', sheet: 'Sheet1', cell: 'A1', value: 123}
- 'append_rows': Add rows. {type: 'append_rows', sheet: 'Sheet1', rows: [[1,2], [3,4]]}
- 'rename_sheet': Rename tab. {type: 'rename_sheet', old_name: 'Sheet1', new_name: 'Data'}
- 'format_cells': Format cells. {type: 'format_cells', sheet: 'Sheet1', range: 'A1:B2', ...}

WRONG: 'set_value', 'add_rows' - these will fail!""",
    )

    # Content operations
    cell: str | None = Field(
        None, description="Cell reference for add_content/delete_content (e.g., 'A1')"
    )
    value: Any | None = Field(None, description="Value to set or match.")

    # Chart operations
    sheet: str | None = Field(None, description="Target sheet name.")
    data_range: str | None = Field(
        None, description="Data range for chart (e.g., 'A1:C10')"
    )
    chart_type: Literal["bar", "line", "pie"] | None = Field(
        None, description="Chart type"
    )
    title: str | None = Field(
        None, description="Title for the entity. REQUIRED for create."
    )
    position: str | None = Field(None, description="Chart position (e.g., 'E2')")
    categories_column: int | None = Field(
        None, description="Column index for X-axis categories"
    )
    include_header: bool | None = Field(None, description="Whether first row is header")

    # CSV options
    delimiter: str | None = Field(None, description="CSV delimiter")
    encoding: str | None = Field(None, description="CSV encoding")
    has_header: bool | None = Field(None, description="CSV has header row")
    row_limit: int | None = Field(None, description="Max rows to read from CSV")


# ============ Output Model ============
class SheetsOutput(BaseModel):
    """Output for sheets meta-tool."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(
        ...,
        description="The operation to perform. REQUIRED. Call with action='help' first.",
    )
    error: str | None = Field(None, description="Error message if failed")

    # Discovery
    help: HelpResponse | None = None

    # Action-specific results
    read_tab: ReadTabResult | None = None
    read_csv: ReadCsvResult | None = None
    create: CreateResult | None = None
    delete: DeleteResult | None = None
    list_tabs: ListTabsResult | None = None
    add_tab: AddTabResult | None = None
    delete_tab: DeleteTabResult | None = None
    edit: EditResult | None = None
    add_content: ContentResult | None = None
    delete_content: ContentResult | None = None
    create_chart: ChartResult | None = None


# ============ Help Definition ============
SHEETS_HELP = HelpResponse(
    tool_name="sheets",
    description="Spreadsheet operations: create, read, edit, and manage .xlsx files.",
    actions={
        "help": ActionInfo(
            description="List all available actions",
            required_params=[],
            optional_params=[],
        ),
        "create": ActionInfo(
            description="Create a new .xlsx spreadsheet",
            required_params=["directory", "file_name", "sheets"],
            optional_params=[],
        ),
        "delete": ActionInfo(
            description="Delete a spreadsheet",
            required_params=["file_path"],
            optional_params=[],
        ),
        "read_tab": ActionInfo(
            description="Read a worksheet tab",
            required_params=["file_path", "tab_index"],
            optional_params=["cell_range"],
        ),
        "read_csv": ActionInfo(
            description="Read a CSV file",
            required_params=["file_path"],
            optional_params=["delimiter", "encoding", "has_header", "row_limit"],
        ),
        "list_tabs": ActionInfo(
            description="List all tabs in a spreadsheet",
            required_params=["file_path"],
            optional_params=[],
        ),
        "add_tab": ActionInfo(
            description="Add a new tab to a spreadsheet",
            required_params=["file_path", "tab_name"],
            optional_params=["sheet_data"],
        ),
        "delete_tab": ActionInfo(
            description="Delete a tab from a spreadsheet",
            required_params=["file_path", "tab_index"],
            optional_params=[],
        ),
        "edit": ActionInfo(
            description="Apply operations (set_cell, append_rows, rename_sheet, format_cells)",
            required_params=["file_path", "operations"],
            optional_params=[],
        ),
        "add_content": ActionInfo(
            description="Add content to a cell (only if empty)",
            required_params=["file_path", "tab_index", "cell", "value"],
            optional_params=[],
        ),
        "delete_content": ActionInfo(
            description="Delete content from a cell",
            required_params=["file_path", "tab_index", "cell"],
            optional_params=[],
        ),
        "create_chart": ActionInfo(
            description="Create a chart from data",
            required_params=["file_path", "sheet", "data_range"],
            optional_params=[
                "chart_type",
                "title",
                "position",
                "categories_column",
                "include_header",
            ],
        ),
    },
)


# ============ Meta-Tool Implementation ============
async def sheets(request: SheetsInput) -> SheetsOutput:
    """Spreadsheet operations: create, read, edit, and manage .xlsx files."""
    match request.action:
        case "help":
            return SheetsOutput(action="help", help=SHEETS_HELP)

        case "create":
            if not request.directory or not request.file_name or not request.sheets:
                return SheetsOutput(
                    action="create",
                    error="Required: directory, file_name, sheets",
                )
            result = await _create_spreadsheet(
                request.directory, request.file_name, request.sheets
            )
            if _is_status_error(result):
                return SheetsOutput(action="create", error=result)
            return SheetsOutput(
                action="create",
                create=CreateResult(
                    status="success",
                    file_path=f"{request.directory.rstrip('/')}/{request.file_name}",
                    sheets_created=len(request.sheets),
                ),
            )

        case "delete":
            if not request.file_path:
                return SheetsOutput(action="delete", error="Required: file_path")
            result = await _delete_spreadsheet(request.file_path)
            if _is_status_error(result):
                return SheetsOutput(action="delete", error=result)
            return SheetsOutput(
                action="delete",
                delete=DeleteResult(status="success", file_path=request.file_path),
            )

        case "read_tab":
            if request.file_path is None or request.tab_index is None:
                return SheetsOutput(
                    action="read_tab", error="Required: file_path, tab_index"
                )
            result = await _read_tab(
                request.file_path, request.tab_index, request.cell_range
            )
            if _is_read_error(result):
                return SheetsOutput(action="read_tab", error=result)
            return SheetsOutput(
                action="read_tab", read_tab=ReadTabResult(raw_output=result)
            )

        case "read_csv":
            if not request.file_path:
                return SheetsOutput(action="read_csv", error="Required: file_path")
            result = await _read_csv(
                request.file_path,
                request.delimiter or ",",
                request.encoding or "utf-8",
                request.has_header if request.has_header is not None else True,
                request.row_limit,
            )
            if _is_read_error(result):
                return SheetsOutput(action="read_csv", error=result)
            return SheetsOutput(
                action="read_csv", read_csv=ReadCsvResult(raw_output=result)
            )

        case "list_tabs":
            if not request.file_path:
                return SheetsOutput(action="list_tabs", error="Required: file_path")
            result = await _list_tabs_in_spreadsheet(request.file_path)
            if _is_read_error(result):
                return SheetsOutput(action="list_tabs", error=result)
            return SheetsOutput(
                action="list_tabs", list_tabs=ListTabsResult(raw_output=result)
            )

        case "add_tab":
            if not request.file_path or not request.tab_name:
                return SheetsOutput(
                    action="add_tab", error="Required: file_path, tab_name"
                )
            result = await _add_tab(
                request.file_path, request.tab_name, request.sheet_data
            )
            if _is_status_error(result):
                return SheetsOutput(action="add_tab", error=result)
            return SheetsOutput(
                action="add_tab",
                add_tab=AddTabResult(
                    status="success",
                    tab_name=request.tab_name,
                    file_path=request.file_path,
                ),
            )

        case "delete_tab":
            if request.file_path is None or request.tab_index is None:
                return SheetsOutput(
                    action="delete_tab", error="Required: file_path, tab_index"
                )
            result = await _delete_tab(request.file_path, request.tab_index)
            if _is_status_error(result):
                return SheetsOutput(action="delete_tab", error=result)
            # Parse tab_name using marker-based extraction (handles quotes in names)
            tab_name = _parse_tab_name(result, request.tab_index)
            return SheetsOutput(
                action="delete_tab",
                delete_tab=DeleteTabResult(
                    status="success",
                    tab_name=tab_name,
                    tab_index=request.tab_index,
                    file_path=request.file_path,
                ),
            )

        case "edit":
            if not request.file_path or not request.operations:
                return SheetsOutput(
                    action="edit", error="Required: file_path, operations"
                )
            result = await _edit_spreadsheet(request.file_path, request.operations)
            if _is_status_error(result):
                return SheetsOutput(action="edit", error=result)
            return SheetsOutput(
                action="edit",
                edit=EditResult(
                    status="success",
                    file_path=request.file_path,
                    operations_applied=len(request.operations),
                ),
            )

        case "add_content":
            if (
                request.file_path is None
                or request.tab_index is None
                or not request.cell
                or request.value is None
            ):
                return SheetsOutput(
                    action="add_content",
                    error="Required: file_path, tab_index, cell, value",
                )
            result = await _add_content_text(
                request.file_path, request.tab_index, request.cell, request.value
            )
            if _is_status_error(result):
                return SheetsOutput(action="add_content", error=result)
            return SheetsOutput(
                action="add_content",
                add_content=ContentResult(
                    status="success",
                    cell=request.cell,
                    tab_index=request.tab_index,
                    file_path=request.file_path,
                ),
            )

        case "delete_content":
            if (
                request.file_path is None
                or request.tab_index is None
                or not request.cell
            ):
                return SheetsOutput(
                    action="delete_content",
                    error="Required: file_path, tab_index, cell",
                )
            result = await _delete_content_cell(
                request.file_path, request.tab_index, request.cell
            )
            if _is_status_error(result):
                return SheetsOutput(action="delete_content", error=result)
            return SheetsOutput(
                action="delete_content",
                delete_content=ContentResult(
                    status="success",
                    cell=request.cell,
                    tab_index=request.tab_index,
                    file_path=request.file_path,
                ),
            )

        case "create_chart":
            if not request.file_path or not request.sheet or not request.data_range:
                return SheetsOutput(
                    action="create_chart",
                    error="Required: file_path, sheet, data_range",
                )
            result = await _create_chart(
                request.file_path,
                request.sheet,
                request.data_range,
                request.chart_type or "bar",
                request.title,
                request.position or "E2",
                request.categories_column,
                request.include_header if request.include_header is not None else True,
            )
            if _is_chart_error(result):
                return SheetsOutput(action="create_chart", error=result)
            return SheetsOutput(
                action="create_chart", create_chart=ChartResult(message=result)
            )

        case _:
            return SheetsOutput(
                action=request.action, error=f"Unknown action: {request.action}"
            )


# ============ Schema Tool ============
class SchemaInput(BaseModel):
    """Input for schema introspection."""

    model_config = ConfigDict(extra="forbid")
    model: str = Field(
        ...,
        description="Model name: 'input', 'output', or a result type like 'ReadTabResult'",
    )


class SchemaOutput(BaseModel):
    """Output for schema introspection."""

    model_config = ConfigDict(extra="forbid")
    model: str
    json_schema: dict[str, Any]


SCHEMAS: dict[str, type[BaseModel]] = {
    "input": SheetsInput,
    "output": SheetsOutput,
    "ReadTabResult": ReadTabResult,
    "ReadCsvResult": ReadCsvResult,
    "CreateResult": CreateResult,
    "DeleteResult": DeleteResult,
    "ListTabsResult": ListTabsResult,
    "AddTabResult": AddTabResult,
    "DeleteTabResult": DeleteTabResult,
    "EditResult": EditResult,
    "ContentResult": ContentResult,
    "ChartResult": ChartResult,
}


async def sheets_schema(request: SchemaInput) -> SchemaOutput:
    """Get JSON schema for sheets input/output models."""
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
