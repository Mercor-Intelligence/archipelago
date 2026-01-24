import os
from typing import Annotated

from models.response import ListTabsResponse, WorksheetInfo
from openpyxl import load_workbook
from pydantic import Field
from utils.decorators import make_async_background

SHEETS_ROOT = os.getenv("APP_SHEETS_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> str:
    """Map path to the sheets root."""
    path = path.lstrip("/")
    full_path = os.path.join(SHEETS_ROOT, path)
    return os.path.normpath(full_path)


@make_async_background
def list_tabs_in_spreadsheet(
    file_path: Annotated[str, Field(description="Path to the .xlsx file")],
) -> str:
    """List worksheet names and indices for a spreadsheet."""

    if not isinstance(file_path, str) or not file_path:
        return "File path is required"
    if not file_path.startswith("/"):
        return "File path must start with /"
    if not file_path.lower().endswith(".xlsx"):
        return "File path must end with .xlsx"

    target_path = _resolve_under_root(file_path)

    try:
        if not os.path.exists(target_path):
            return f"File not found: {file_path}"
        if not os.path.isfile(target_path):
            return f"Not a file: {file_path}"
    except Exception as exc:
        return f"Failed to access file: {repr(exc)}"

    try:
        workbook = load_workbook(target_path)
        worksheets = []
        for idx, sheet_name in enumerate(workbook.sheetnames):
            sheet = workbook[sheet_name]
            # Get the number of rows and columns in the sheet
            row_count = sheet.max_row if sheet.max_row else 0
            column_count = sheet.max_column if sheet.max_column else 0
            worksheets.append(
                WorksheetInfo(
                    name=sheet_name,
                    index=idx,
                    row_count=row_count,
                    column_count=column_count,
                )
            )
        workbook.close()

        response = ListTabsResponse(worksheets=worksheets)
        return str(response)
    except Exception as exc:
        return f"Failed to load workbook: {repr(exc)}"
