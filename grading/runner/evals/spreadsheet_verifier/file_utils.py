"""File utilities for spreadsheet verifier."""

import csv
import fnmatch
import io
import zipfile
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from .cell_utils import SpreadsheetData
from .config import EXCEL_EXTENSIONS, SUPPORTED_EXTENSIONS


def find_matching_files(zip_file: zipfile.ZipFile, file_pattern: str) -> list[str]:
    """Find files in zip matching the pattern.

    Pattern can be:
    - Exact filename: "data.csv"
    - Extension pattern: "*.csv"
    - Path pattern: "output/*.xlsx"
    """
    matching = []
    for name in zip_file.namelist():
        if name.endswith("/"):
            continue

        normalized = name.replace("\\", "/").strip("/")
        basename = Path(normalized).name

        if fnmatch.fnmatch(basename, file_pattern) or fnmatch.fnmatch(
            normalized, file_pattern
        ):
            ext = Path(name).suffix.lower()
            if ext in SUPPORTED_EXTENSIONS:
                matching.append(name)

    return matching


def load_spreadsheet_data(
    file_bytes: io.BytesIO, file_ext: str, sheet_name: str | None = None
) -> SpreadsheetData:
    """Load a spreadsheet file into SpreadsheetData.

    Data is loaded without treating the first row as headers,
    so cell references like A1, B2 map directly to row/column indices.
    """
    file_bytes.seek(0)

    if file_ext == ".csv":
        return _load_csv(file_bytes)

    file_bytes.seek(0)
    return _load_excel(file_bytes, sheet_name)


def _load_csv(file_bytes: io.BytesIO) -> SpreadsheetData:
    """Load CSV file into SpreadsheetData."""
    for encoding in ["utf-8", "latin-1", "cp1252"]:
        try:
            file_bytes.seek(0)
            text = file_bytes.read().decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        file_bytes.seek(0)
        text = file_bytes.read().decode("utf-8", errors="replace")

    rows: list[list[Any]] = []
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        converted_row: list[Any] = []
        for cell in row:
            converted_row.append(_convert_cell_value(cell))
        rows.append(converted_row)

    return SpreadsheetData(rows=rows)


def _load_excel(
    file_bytes: io.BytesIO, sheet_name: str | None = None
) -> SpreadsheetData:
    """Load Excel file into SpreadsheetData using openpyxl."""
    wb = load_workbook(file_bytes, data_only=True, read_only=True)
    try:
        if sheet_name:
            if sheet_name not in wb.sheetnames:
                return SpreadsheetData(rows=[])
            ws = wb[sheet_name]
        else:
            if not wb.worksheets:
                return SpreadsheetData(rows=[])
            ws = wb.worksheets[0]

        rows: list[list[Any]] = []
        for row in ws.iter_rows(values_only=True):
            rows.append(list(row))

        return SpreadsheetData(rows=rows)
    finally:
        wb.close()


def _convert_cell_value(value: str) -> Any:
    """Convert string cell value to appropriate type."""
    if not value:
        return None

    try:
        return int(value)
    except ValueError:
        pass

    try:
        return float(value)
    except ValueError:
        pass

    return value


def load_workbook_from_bytes(file_bytes: bytes) -> Workbook:
    """Load an openpyxl Workbook from bytes."""
    return load_workbook(io.BytesIO(file_bytes), data_only=False)


def get_worksheet(
    workbook: Workbook, sheet_name: str | None = None
) -> tuple[Worksheet | None, str | None]:
    """Get worksheet by name or first sheet.

    Returns:
        Tuple of (worksheet, error_message). If error, worksheet is None.
    """
    if sheet_name:
        if sheet_name not in workbook.sheetnames:
            return (
                None,
                f"Sheet '{sheet_name}' not found. Available: {workbook.sheetnames}",
            )
        return workbook[sheet_name], None

    # Use first sheet by index for consistent, predictable behavior
    # (workbook.active returns whichever sheet was last viewed, not necessarily first)
    if not workbook.worksheets:
        return None, "No worksheets found in workbook"
    return workbook.worksheets[0], None


def is_excel_file(file_ext: str) -> bool:
    """Check if file extension is Excel."""
    return file_ext.lower() in EXCEL_EXTENSIONS
