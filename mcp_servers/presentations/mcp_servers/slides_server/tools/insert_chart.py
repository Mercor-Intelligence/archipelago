import os
from io import BytesIO
from typing import Literal

from models.response import InsertChartResponse
from openpyxl import load_workbook
from openpyxl.utils import range_boundaries
from pptx import Presentation
from pptx.chart.data import CategoryChartData, XyChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches
from utils.decorators import make_async_background

SLIDES_ROOT = os.getenv("APP_SLIDES_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")

ChartType = Literal["bar", "line", "pie", "area", "scatter", "doughnut", "radar"]

CHART_TYPE_MAP = {
    "bar": XL_CHART_TYPE.COLUMN_CLUSTERED,
    "line": XL_CHART_TYPE.LINE,
    "pie": XL_CHART_TYPE.PIE,
    "area": XL_CHART_TYPE.AREA,
    "scatter": XL_CHART_TYPE.XY_SCATTER,
    "doughnut": XL_CHART_TYPE.DOUGHNUT,
    "radar": XL_CHART_TYPE.RADAR,
}


def _resolve_under_root(path: str) -> str:
    """Map path to the slides root."""
    path = path.lstrip("/")
    full_path = os.path.join(SLIDES_ROOT, path)
    return os.path.normpath(full_path)


def _read_spreadsheet_data(
    spreadsheet_path: str, sheet_name: str, data_range: str, include_header: bool
) -> tuple[list[str], list[str], list[list[float]], str | None]:
    """Read data from spreadsheet and return categories, series names, and values."""
    target_path = _resolve_under_root(spreadsheet_path)
    if not os.path.exists(target_path):
        return [], [], [], f"Spreadsheet not found: {spreadsheet_path}"
    try:
        with open(target_path, "rb") as f:
            workbook = load_workbook(BytesIO(f.read()), data_only=True)
    except Exception as exc:
        return [], [], [], f"Failed to open spreadsheet: {repr(exc)}"
    if sheet_name not in workbook.sheetnames:
        workbook.close()
        return [], [], [], f"Sheet '{sheet_name}' does not exist"
    worksheet = workbook[sheet_name]
    try:
        min_col, min_row, max_col, max_row = range_boundaries(data_range.upper())
    except Exception as exc:
        workbook.close()
        return [], [], [], f"Invalid data range: {repr(exc)}"
    all_rows = []
    for row in worksheet.iter_rows(
        min_row=min_row,
        max_row=max_row,
        min_col=min_col,
        max_col=max_col,
        values_only=True,
    ):
        all_rows.append(list(row))
    workbook.close()
    if not all_rows:
        return [], [], [], "No data found in the specified range"
    if include_header:
        header_row = all_rows[0]
        series_names = [
            str(h) if h else f"Series {i}" for i, h in enumerate(header_row[1:], 1)
        ]
        data_rows = all_rows[1:]
    else:
        series_names = [f"Series {i}" for i in range(1, len(all_rows[0]))]
        data_rows = all_rows
    categories = []
    numeric_data = []
    for row in data_rows:
        categories.append(str(row[0]) if row[0] else "")
        row_values = []
        for val in row[1:]:
            try:
                row_values.append(float(val) if val is not None else 0.0)
            except (ValueError, TypeError):
                row_values.append(0.0)
        numeric_data.append(row_values)
    return categories, series_names, numeric_data, None


@make_async_background
def insert_chart(
    presentation_path: str,
    slide_index: int,
    spreadsheet_path: str,
    sheet_name: str,
    data_range: str,
    chart_type: ChartType = "bar",
    title: str | None = None,
    position: str = "body",
    include_header: bool = True,
) -> InsertChartResponse:
    """Insert a chart into a slide using data from a spreadsheet."""

    def error(msg: str) -> InsertChartResponse:
        return InsertChartResponse(success=False, error=msg)

    # Validate presentation path
    if not isinstance(presentation_path, str) or not presentation_path:
        return error("Presentation path is required")
    if not presentation_path.startswith("/"):
        return error("Presentation path must start with /")
    if not presentation_path.lower().endswith(".pptx"):
        return error("Presentation path must end with .pptx")

    # Validate spreadsheet path
    if not isinstance(spreadsheet_path, str) or not spreadsheet_path:
        return error("Spreadsheet path is required")
    if not spreadsheet_path.startswith("/"):
        return error("Spreadsheet path must start with /")
    if not spreadsheet_path.lower().endswith(".xlsx"):
        return error("Spreadsheet path must end with .xlsx")

    # Validate other inputs
    if not isinstance(sheet_name, str) or not sheet_name.strip():
        return error("Sheet name is required")

    if not isinstance(data_range, str) or ":" not in data_range:
        return error("Data range must be a valid range like 'A1:D5'")

    valid_chart_types = {"bar", "line", "pie", "area", "scatter", "doughnut", "radar"}
    if chart_type not in valid_chart_types:
        return error(f"Chart type must be one of: {sorted(valid_chart_types)}")

    # Read data from spreadsheet
    categories, series_names, data_rows, data_error = _read_spreadsheet_data(
        spreadsheet_path, sheet_name, data_range, include_header
    )
    if data_error:
        return error(data_error)

    if not categories or not data_rows:
        return error("No valid data found in the specified range")

    if not series_names:
        return error(
            "No data series found in the specified range. "
            "Data range must have at least 2 columns: 1 for categories and 1+ for data series."
        )

    # Load presentation
    pptx_path = _resolve_under_root(presentation_path)

    if not os.path.exists(pptx_path):
        return error(f"Presentation not found: {presentation_path}")

    try:
        with open(pptx_path, "rb") as f:
            presentation = Presentation(BytesIO(f.read()))
    except Exception as exc:
        return error(f"Failed to open presentation: {repr(exc)}")

    # Get slide
    if slide_index < 0 or slide_index >= len(presentation.slides):
        return error(
            f"Slide index {slide_index} is out of range (0-{len(presentation.slides) - 1})"
        )

    slide = presentation.slides[slide_index]

    # Build chart data
    if chart_type == "scatter":
        # Scatter charts use XyChartData with (x, y) pairs
        chart_data = XyChartData()
        # For scatter, the categories column contains X values (must be numeric)
        x_values = []
        non_numeric_values = []
        for cat in categories:
            try:
                x_val = float(cat) if cat else 0.0
                x_values.append(x_val)
            except (ValueError, TypeError):
                non_numeric_values.append(cat)
                x_values.append(0.0)

        # Return error if any X values were non-numeric
        if non_numeric_values:
            sample = non_numeric_values[:3]
            return error(
                f"Scatter charts require numeric X values in the first column. "
                f"Found non-numeric values: {sample}. "
                f"Use a different chart type (e.g., 'line' or 'bar') for categorical data."
            )

        # Each column in data_rows is a Y series
        for series_idx, series_name in enumerate(series_names):
            series = chart_data.add_series(series_name)
            for row_idx, row in enumerate(data_rows):
                y_val = row[series_idx] if series_idx < len(row) else 0.0
                series.add_data_point(x_values[row_idx], y_val)
    else:
        chart_data = CategoryChartData()
        chart_data.categories = categories

        # Add each series (transpose data: rows become series values per category)
        num_series = len(series_names)
        for series_idx in range(num_series):
            series_values = [
                row[series_idx] if series_idx < len(row) else 0.0 for row in data_rows
            ]
            chart_data.add_series(series_names[series_idx], series_values)

    # Determine chart position and size
    if position == "body":
        x, y = Inches(1.0), Inches(1.5)
        cx, cy = Inches(8.0), Inches(5.0)
    elif position == "left":
        x, y = Inches(0.5), Inches(1.5)
        cx, cy = Inches(4.5), Inches(5.0)
    elif position == "right":
        x, y = Inches(5.0), Inches(1.5)
        cx, cy = Inches(4.5), Inches(5.0)
    else:
        # Default position
        x, y = Inches(1.0), Inches(1.5)
        cx, cy = Inches(8.0), Inches(5.0)

    # Get chart type enum
    xl_chart_type = CHART_TYPE_MAP.get(chart_type, XL_CHART_TYPE.COLUMN_CLUSTERED)

    # Add chart to slide
    try:
        graphic_frame = slide.shapes.add_chart(
            xl_chart_type,
            x,
            y,
            cx,
            cy,
            chart_data,  # type: ignore[arg-type]
        )
        chart = graphic_frame.chart  # type: ignore[attr-defined]
        if title:
            chart.has_title = True
            chart.chart_title.text_frame.text = title
    except Exception as exc:
        return error(f"Failed to create chart: {repr(exc)}")

    # Save presentation
    try:
        presentation.save(pptx_path)
    except Exception as exc:
        return error(f"Failed to save presentation: {repr(exc)}")

    return InsertChartResponse(
        success=True,
        slide_index=slide_index,
        chart_type=chart_type,
        title=title,
    )
