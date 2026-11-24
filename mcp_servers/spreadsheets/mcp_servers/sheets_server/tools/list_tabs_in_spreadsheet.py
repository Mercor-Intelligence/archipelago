import os
from typing import Annotated

from models.response import ListTabsResponse, WorksheetInfo
from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet
from pydantic import Field
from utils.decorators import make_async_background
from utils.path_utils import PathTraversalError, resolve_under_root
from utils.structured_output import structured_text


def _data_extent(sheet: Worksheet) -> tuple[int, int]:
    """Last row and column holding a value, widened over merged ranges.

    openpyxl's ``max_row``/``max_column`` report the used range, which counts
    cells that carry only a style -- a Google Sheets export fills a whole
    1000x26 block that way. ``row_count``/``column_count`` promise rows and
    columns with data, and read_tab bounds its grid by value, so measure the
    values here too.
    """
    last_row = 0
    last_column = 0
    for row_index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
        for column_index, value in enumerate(row, start=1):
            if value is not None:
                last_row = row_index
                last_column = max(last_column, column_index)

    if last_row == 0:
        return 0, 0

    # A merged range stores its value in the anchor cell only; every other cell
    # it covers reads back as None while still carrying that value on screen.
    for merged in sheet.merged_cells.ranges:
        last_row = max(last_row, merged.max_row)
        last_column = max(last_column, merged.max_col)

    return last_row, last_column


@make_async_background
def list_tabs_in_spreadsheet(
    file_path: Annotated[
        str,
        Field(
            description="Absolute path to the .xlsx file starting with '/' (e.g., '/data/report.xlsx')"
        ),
    ],
) -> str:
    """List all worksheet tabs in a workbook with their metadata."""

    if not isinstance(file_path, str) or not file_path:
        return "File path is required"
    if not file_path.startswith("/"):
        return "File path must start with /"
    if not file_path.lower().endswith(".xlsx"):
        return "File path must end with .xlsx"

    try:
        target_path = resolve_under_root(file_path)
    except PathTraversalError:
        return f"Invalid path: {file_path}"

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
            # Get the number of rows and columns with data in the sheet
            row_count, column_count = _data_extent(sheet)
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
        return structured_text(response)
    except Exception as exc:
        return f"Failed to load workbook: {repr(exc)}"
