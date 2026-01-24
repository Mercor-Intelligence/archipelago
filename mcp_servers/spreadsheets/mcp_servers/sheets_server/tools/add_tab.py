import os
from io import BytesIO
from typing import Annotated, Any

from models.response import AddTabResponse
from models.sheet import SheetData
from openpyxl import load_workbook
from pydantic import Field
from utils.decorators import make_async_background
from utils.helpers import recalculate_formulas

SHEETS_ROOT = os.getenv("APP_SHEETS_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> str:
    """Map path to the sheets root."""
    path = path.lstrip("/")
    full_path = os.path.join(SHEETS_ROOT, path)
    return os.path.normpath(full_path)


@make_async_background
def add_tab(
    file_path: Annotated[str, Field(description="Path to the .xlsx file")],
    tab_name: Annotated[
        str, Field(description="Name for the new worksheet tab", max_length=31)
    ],
    sheet_data: Annotated[
        dict[str, Any] | None, Field(description="Optional data with headers and rows")
    ] = None,
) -> str:
    """Add a new worksheet tab to an existing spreadsheet with optional data."""

    if not isinstance(file_path, str) or not file_path:
        return "File path is required"
    if not file_path.startswith("/"):
        return "File path must start with /"
    if not file_path.lower().endswith(".xlsx"):
        return "File path must end with .xlsx"

    if not isinstance(tab_name, str) or not tab_name:
        return "Tab name is required"

    if len(tab_name) > 31:
        return "Tab name cannot exceed 31 characters"

    invalid_chars = ["\\", "/", "?", "*", "[", "]"]
    for char in invalid_chars:
        if char in tab_name:
            return f"Tab name cannot contain '{char}'"

    sheet_model = None
    if sheet_data is not None:
        try:
            sheet_model = SheetData.model_validate(sheet_data)
        except Exception as exc:
            return f"Invalid sheet_data: {repr(exc)}"

        if sheet_model.headers is not None:
            header_length = len(sheet_model.headers)
            for row_index, row in enumerate(sheet_model.rows):
                if len(row) != header_length:
                    return f"Row {row_index} must match header length ({header_length})"

    target_path = _resolve_under_root(file_path)

    try:
        if not os.path.exists(target_path):
            return f"File not found: {file_path}"
        if not os.path.isfile(target_path):
            return f"Not a file: {file_path}"

        with open(target_path, "rb") as f:
            file_bytes = f.read()
    except Exception as exc:
        return f"Failed to read spreadsheet: {repr(exc)}"

    workbook = None
    try:
        workbook = load_workbook(BytesIO(file_bytes))

        if tab_name in workbook.sheetnames:
            existing_tabs = ", ".join(workbook.sheetnames)
            return f"Tab '{tab_name}' already exists. Existing tabs: {existing_tabs}"

        ws = workbook.create_sheet(title=tab_name)

        if sheet_model and sheet_model.headers is not None:
            ws.append(sheet_model.headers)
            ws.freeze_panes = "A2"

        if sheet_model and sheet_model.rows:
            for row in sheet_model.rows:
                ws.append(row)

        workbook.save(target_path)

        rows_added = None
        if sheet_model and (sheet_model.headers is not None or sheet_model.rows):
            row_count = len(sheet_model.rows)
            if sheet_model.headers is not None:
                row_count += 1  # Count the header row
            rows_added = row_count

        response = AddTabResponse(
            status="success",
            tab_name=tab_name,
            file_path=file_path,
            rows_added=rows_added,
        )

    except Exception as exc:
        return f"Failed to add tab: {repr(exc)}"
    finally:
        if workbook is not None:
            workbook.close()

    recalculate_formulas(target_path)

    return str(response)
