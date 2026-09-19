"""Cell reference parsing utilities for spreadsheet verifier."""

import re
from dataclasses import dataclass, field
from typing import Any

from openpyxl.cell import Cell
from openpyxl.utils import range_boundaries
from openpyxl.worksheet.worksheet import Worksheet


@dataclass
class SpreadsheetData:
    """Simple grid data structure to replace pandas DataFrame.

    Stores spreadsheet data as a list of rows, where each row is a list of cell values.
    Supports cell access by (row, col) index (0-based internally, but external
    references use 1-based row numbers like Excel).
    """

    rows: list[list[Any]] = field(default_factory=list)

    @property
    def num_rows(self) -> int:
        """Return number of rows."""
        return len(self.rows)

    @property
    def num_cols(self) -> int:
        """Return number of columns (from first row, or 0 if empty)."""
        return len(self.rows[0]) if self.rows else 0

    def get_value(self, row_idx: int, col_idx: int) -> Any | None:
        """Get cell value by 0-based row and column index.

        Returns None if indices are out of bounds.
        """
        if row_idx < 0 or row_idx >= len(self.rows):
            return None
        row = self.rows[row_idx]
        if col_idx < 0 or col_idx >= len(row):
            return None
        return row[col_idx]

    def is_empty_value(self, value: Any) -> bool:
        """Check if a value should be considered empty/null."""
        if value is None:
            return True
        if isinstance(value, str) and not value.strip():
            return True
        if isinstance(value, float) and value != value:  # NaN check
            return True
        return False


@dataclass
class CellAddress:
    """Parsed cell address."""

    column: str
    row: int
    sheet: str | None = None

    @property
    def column_index(self) -> int:
        """Convert column letters to 0-based index."""
        result = 0
        for char in self.column.upper():
            result = result * 26 + (ord(char) - ord("A") + 1)
        return result - 1

    def __str__(self) -> str:
        if self.sheet:
            return f"{self.sheet}!{self.column}{self.row}"
        return f"{self.column}{self.row}"


@dataclass
class CellRange:
    """Parsed cell range."""

    start: CellAddress
    end: CellAddress | None = None

    @property
    def is_single_cell(self) -> bool:
        return self.end is None

    def __str__(self) -> str:
        if self.end:
            return f"{self.start}:{self.end}"
        return str(self.start)


CELL_REF_PATTERN = re.compile(r"^(?:([^!]+)!)?([A-Za-z]{1,3})(\d+)$")


def parse_cell_reference(ref: str) -> CellAddress | None:
    """Parse a cell reference like 'A1', 'Sheet1!B2', 'AA100'.

    Returns:
        CellAddress or None if invalid.
    """
    ref = ref.strip()
    match = CELL_REF_PATTERN.match(ref)
    if not match:
        return None

    sheet = match.group(1)
    column = match.group(2).upper()
    row = int(match.group(3))

    if row < 1:
        return None

    return CellAddress(column=column, row=row, sheet=sheet)


def parse_cell_range(ref: str) -> CellRange | None:
    """Parse a cell range like 'A1', 'A1:B10', 'Sheet1!C1:C100'.

    Returns:
        CellRange or None if invalid.
    """
    ref = ref.strip()

    sheet = None
    if "!" in ref:
        parts = ref.split("!", 1)
        sheet = parts[0]
        ref = parts[1]

    if ":" in ref:
        parts = ref.split(":", 1)
        start_ref = f"{sheet}!{parts[0]}" if sheet else parts[0]
        end_ref = f"{sheet}!{parts[1]}" if sheet else parts[1]

        start = parse_cell_reference(start_ref)
        end = parse_cell_reference(end_ref)

        if not start or not end:
            return None

        return CellRange(start=start, end=end)

    full_ref = f"{sheet}!{ref}" if sheet else ref
    cell = parse_cell_reference(full_ref)
    if not cell:
        return None

    return CellRange(start=cell, end=None)


def get_cell_value(data: SpreadsheetData, cell: CellAddress) -> tuple[Any, str | None]:
    """Get cell value from SpreadsheetData.

    Args:
        data: The spreadsheet data
        cell: Parsed cell address

    Returns:
        Tuple of (value, error_message). If error, value is None.
    """
    col_idx = cell.column_index

    if col_idx < 0 or col_idx >= data.num_cols:
        return None, f"Column '{cell.column}' out of range (max: {data.num_cols})"

    row_idx = cell.row - 1

    if row_idx < 0 or row_idx >= data.num_rows:
        return None, f"Row {cell.row} out of range (max: {data.num_rows})"

    value = data.get_value(row_idx, col_idx)

    if data.is_empty_value(value):
        return None, None

    return value, None


def get_cells_from_worksheet(
    ws: Worksheet, cell_range: CellRange, workbook: Any = None
) -> list[Cell]:
    """Get Cell objects from worksheet for a range.

    Args:
        ws: openpyxl Worksheet
        cell_range: Parsed cell range
        workbook: openpyxl Workbook (for named ranges)

    Returns:
        List of Cell objects.
    """
    cells: list[Cell] = []
    ref_str = str(cell_range)

    if cell_range.start.sheet:
        ref_str = ref_str.split("!", 1)[1] if "!" in ref_str else ref_str

    if workbook and ref_str in workbook.defined_names:
        defn = workbook.defined_names[ref_str]
        for dest in defn.destinations:
            sheet_title, range_str = dest
            if sheet_title == ws.title:
                parsed = parse_cell_range(range_str)
                if parsed:
                    cells.extend(get_cells_from_worksheet(ws, parsed, None))
        return cells

    if cell_range.is_single_cell:
        try:
            coord = f"{cell_range.start.column}{cell_range.start.row}"
            cell = ws[coord]
            if isinstance(cell, Cell):
                cells.append(cell)
        except (KeyError, ValueError):
            pass
    else:
        end = cell_range.end
        if end is not None:
            try:
                range_str = f"{cell_range.start.column}{cell_range.start.row}:{end.column}{end.row}"
                min_col, min_row, max_col, max_row = range_boundaries(range_str)
                if min_row and max_row and min_col and max_col:
                    for row in range(min_row, max_row + 1):
                        for col in range(min_col, max_col + 1):
                            cell = ws.cell(row=row, column=col)
                            if isinstance(cell, Cell):
                                cells.append(cell)
            except (ValueError, TypeError):
                pass

    return cells


def expand_cell_range(cell_range: CellRange) -> list[CellAddress]:
    """Expand a cell range into list of individual cell addresses.

    Args:
        cell_range: Parsed cell range

    Returns:
        List of CellAddress objects.
    """
    if cell_range.is_single_cell:
        return [cell_range.start]

    addresses: list[CellAddress] = []
    start = cell_range.start
    end = cell_range.end

    # After is_single_cell check, end is guaranteed to be non-None
    assert end is not None

    start_col = start.column_index
    end_col = end.column_index
    start_row = start.row
    end_row = end.row

    for row in range(min(start_row, end_row), max(start_row, end_row) + 1):
        for col_idx in range(min(start_col, end_col), max(start_col, end_col) + 1):
            col_letters = _index_to_column_letters(col_idx)
            addresses.append(
                CellAddress(column=col_letters, row=row, sheet=start.sheet)
            )

    return addresses


def _index_to_column_letters(index: int) -> str:
    """Convert 0-based column index to Excel column letters."""
    result = ""
    index += 1
    while index > 0:
        index -= 1
        result = chr(ord("A") + (index % 26)) + result
        index //= 26
    return result
