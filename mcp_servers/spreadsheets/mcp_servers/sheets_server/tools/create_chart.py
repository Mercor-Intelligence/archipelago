import os
from io import BytesIO
from typing import Annotated, Literal

from openpyxl import load_workbook
from openpyxl.chart import BarChart, LineChart, PieChart
from openpyxl.chart.reference import Reference
from pydantic import Field
from utils.decorators import make_async_background

SHEETS_ROOT = os.getenv("APP_SHEETS_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")

ChartType = Literal["bar", "line", "pie"]


def _resolve_under_root(path: str) -> str:
    """Map path to the sheets root."""
    path = path.lstrip("/")
    full_path = os.path.join(SHEETS_ROOT, path)
    return os.path.normpath(full_path)


def _create_chart_object(chart_type: ChartType):
    """Create the appropriate chart object based on type."""
    if chart_type == "bar":
        chart = BarChart()
        chart.type = "col"
        return chart
    elif chart_type == "line":
        return LineChart()
    elif chart_type == "pie":
        return PieChart()
    else:
        raise ValueError(f"Unsupported chart type: {chart_type}")


@make_async_background
def create_chart(
    file_path: Annotated[str, Field(description="Path to the .xlsx file")],
    sheet: Annotated[str, Field(description="Sheet name containing the data")],
    data_range: Annotated[str, Field(description="Data range like 'A1:C10'")],
    chart_type: Annotated[
        ChartType, Field(description="Chart type: bar, line, or pie")
    ] = "bar",
    title: Annotated[str | None, Field(description="Chart title")] = None,
    position: Annotated[
        str, Field(description="Cell position for chart placement")
    ] = "E2",
    categories_column: Annotated[
        int | None, Field(description="Column index for X-axis categories")
    ] = None,
    include_header: Annotated[
        bool, Field(description="Whether first row is header")
    ] = True,
) -> str:
    """Create a chart from data in a spreadsheet."""
    if not isinstance(file_path, str) or not file_path:
        return "File path is required"
    if not file_path.startswith("/"):
        return "File path must start with /"
    if not file_path.lower().endswith(".xlsx"):
        return "File path must end with .xlsx"

    if not isinstance(sheet, str) or not sheet.strip():
        return "Sheet name is required"

    if not isinstance(data_range, str) or ":" not in data_range:
        return "Data range must be a valid range like 'A1:C10'"

    valid_chart_types = {"bar", "line", "pie"}
    if chart_type not in valid_chart_types:
        return f"Chart type must be one of: {sorted(valid_chart_types)}"

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

    try:
        workbook = load_workbook(BytesIO(file_bytes))
    except Exception as exc:
        return f"Failed to open spreadsheet: {repr(exc)}"

    if sheet not in workbook.sheetnames:
        workbook.close()
        return f"Sheet '{sheet}' does not exist"

    worksheet = workbook[sheet]

    try:
        from openpyxl.utils import range_boundaries

        min_col, min_row, max_col, max_row = range_boundaries(data_range.upper())
    except Exception as exc:
        workbook.close()
        return f"Invalid data range '{data_range}': {repr(exc)}"

    if None in (min_col, min_row, max_col, max_row):
        workbook.close()
        return (
            f"Data range '{data_range}' must be a bounded rectangular range like 'A1:C10'. "
            "Unbounded ranges (e.g., '1:10' or 'A:C') are not supported."
        )

    # Type assertions: after validation, these are guaranteed to be int
    assert min_col is not None
    assert min_row is not None
    assert max_col is not None
    assert max_row is not None

    try:
        chart = _create_chart_object(chart_type)
    except Exception as exc:
        workbook.close()
        return f"Failed to create chart: {repr(exc)}"

    if include_header and min_row == max_row:
        workbook.close()
        return (
            f"Data range '{data_range}' has only one row but include_header=True. "
            "Either provide at least 2 rows of data or set include_header=False."
        )

    if categories_column is not None and categories_column < 0:
        workbook.close()
        return f"categories_column must be non-negative, got: {categories_column}"

    num_cols = max_col - min_col + 1

    if categories_column is not None and categories_column > num_cols:
        workbook.close()
        return (
            f"categories_column must not exceed the number of columns in the data range. "
            f"Got categories_column={categories_column}, but data range '{data_range}' has only {num_cols} column(s)."
        )

    if categories_column is None or categories_column > 0:
        if num_cols < 2:
            workbook.close()
            return (
                f"Data range '{data_range}' has only {num_cols} column(s). "
                "When using category labels, provide at least 2 columns (1 for categories, 1+ for data), "
                "or set categories_column=0 to skip categories."
            )

    try:
        if title:
            chart.title = title

        data_start_row = min_row + 1 if include_header else min_row

        if categories_column is None:
            cat_col = min_col
        elif categories_column == 0:
            cat_col = None
        else:
            cat_col = min_col + categories_column - 1

        if chart_type == "pie":
            data_col = min_col + 1 if cat_col == min_col else min_col
            data = Reference(
                worksheet,
                min_col=data_col,
                min_row=min_row if include_header else data_start_row,
                max_row=max_row,
            )
            chart.add_data(data, titles_from_data=include_header)

            if cat_col:
                cats = Reference(
                    worksheet, min_col=cat_col, min_row=data_start_row, max_row=max_row
                )
                chart.set_categories(cats)
        else:
            data_min_col = min_col + 1 if cat_col == min_col else min_col
            data = Reference(
                worksheet,
                min_col=data_min_col,
                max_col=max_col,
                min_row=min_row if include_header else data_start_row,
                max_row=max_row,
            )
            chart.add_data(data, titles_from_data=include_header)

            if cat_col:
                cats = Reference(
                    worksheet, min_col=cat_col, min_row=data_start_row, max_row=max_row
                )
                chart.set_categories(cats)

        worksheet.add_chart(chart, position.upper())  # type: ignore[call-arg]

    except Exception as exc:
        workbook.close()
        return f"Failed to create chart: {repr(exc)}"

    try:
        workbook.save(target_path)
        return f"Chart '{title or chart_type}' created in {sheet} at position {position.upper()}"
    except Exception as exc:
        return f"Failed to save spreadsheet: {repr(exc)}"
    finally:
        workbook.close()
