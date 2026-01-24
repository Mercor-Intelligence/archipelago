import csv
import os
from typing import Annotated, Any

from models.response import ReadCsvResponse
from pydantic import Field
from utils.decorators import make_async_background
from utils.helpers import trim_empty_trailing_data

SHEETS_ROOT = os.getenv("APP_SHEETS_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> str:
    """Map path to the sheets root."""
    path = path.lstrip("/")
    full_path = os.path.join(SHEETS_ROOT, path)
    return os.path.normpath(full_path)


@make_async_background
def read_csv(
    file_path: Annotated[str, Field(description="Path to the .csv file")],
    delimiter: Annotated[str, Field(description="Column delimiter character")] = ",",
    encoding: Annotated[
        str, Field(description="File encoding (e.g., utf-8)")
    ] = "utf-8",
    has_header: Annotated[
        bool, Field(description="Whether first row is header")
    ] = True,
    row_limit: Annotated[int | None, Field(description="Maximum rows to read")] = None,
) -> str:
    """Read data from a CSV file."""

    if not isinstance(file_path, str) or not file_path:
        return "File path is required"
    if not file_path.startswith("/"):
        return "File path must start with /"
    if not file_path.lower().endswith(".csv"):
        return "File path must end with .csv"

    if not isinstance(delimiter, str) or len(delimiter) == 0:
        return "Delimiter must be a non-empty string"
    if delimiter == "\\t":
        delimiter = "\t"

    if not isinstance(encoding, str) or not encoding:
        return "Encoding must be a non-empty string"

    if row_limit is not None and (not isinstance(row_limit, int) or row_limit < 0):
        return "Row limit must be a non-negative integer"

    target_path = _resolve_under_root(file_path)

    try:
        if not os.path.exists(target_path):
            return f"File not found: {file_path}"
        if not os.path.isfile(target_path):
            return f"Not a file: {file_path}"
    except Exception as exc:
        return f"Failed to access file: {repr(exc)}"

    try:
        with open(target_path, encoding=encoding, newline="") as csvfile:
            reader = csv.reader(csvfile, delimiter=delimiter)

            headers: list[str] | None = None
            values: list[list[Any]] = []

            rows_read = 0
            for row_idx, row in enumerate(reader):
                if row_idx == 0 and has_header:
                    headers = row
                    continue

                if row_limit is not None and rows_read >= row_limit:
                    break

                parsed_row: list[Any] = []
                for cell in row:
                    parsed_row.append(_parse_cell_value(cell))
                values.append(parsed_row)
                rows_read += 1

        values = trim_empty_trailing_data(values)

        column_count = 0
        if headers:
            column_count = len(headers)
        elif values:
            column_count = max(len(row) for row in values)

        response = ReadCsvResponse(
            file_path=file_path,
            headers=headers,
            values=values,
            row_count=len(values),
            column_count=column_count,
        )
        return str(response)

    except UnicodeDecodeError as exc:
        return f"Failed to decode file with encoding '{encoding}': {repr(exc)}. Try a different encoding (e.g., 'latin-1', 'cp1252', 'utf-8-sig')."
    except csv.Error as exc:
        return f"Failed to parse CSV: {repr(exc)}"
    except Exception as exc:
        return f"Unexpected error reading CSV: {repr(exc)}"


def _parse_cell_value(value: str) -> Any:
    """Attempt to parse a CSV cell value to appropriate Python type."""
    if value == "":
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
