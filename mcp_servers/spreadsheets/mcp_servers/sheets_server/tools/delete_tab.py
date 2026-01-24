import os
from io import BytesIO
from typing import Annotated

from models.response import DeleteTabResponse
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
def delete_tab(
    file_path: Annotated[str, Field(description="Path to the .xlsx file")],
    tab_index: Annotated[
        int, Field(description="0-based worksheet tab index to delete", ge=0)
    ],
) -> str:
    """Delete a worksheet tab from a spreadsheet."""

    if not isinstance(file_path, str) or not file_path:
        return "File path is required"
    if not file_path.startswith("/"):
        return "File path must start with /"
    if not file_path.lower().endswith(".xlsx"):
        return "File path must end with .xlsx"

    if not isinstance(tab_index, int) or tab_index < 0:
        return "Tab index must be a non-negative integer"

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

        if len(workbook.sheetnames) == 1:
            return "Cannot delete the only remaining tab. Spreadsheets requires at least one worksheet."

        if tab_index >= len(workbook.sheetnames):
            sheet_count = len(workbook.sheetnames)
            return f"Tab index {tab_index} is out of range. Available sheets: {sheet_count}"

        sheet_to_delete = workbook.sheetnames[tab_index]
        worksheet = workbook[sheet_to_delete]

        workbook.remove(worksheet)

        workbook.save(target_path)

        response = DeleteTabResponse(
            status="success",
            tab_name=sheet_to_delete,
            tab_index=tab_index,
            file_path=file_path,
        )

    except Exception as exc:
        return f"Failed to delete tab: {repr(exc)}"
    finally:
        if workbook is not None:
            workbook.close()

    recalculate_formulas(target_path)

    return str(response)
