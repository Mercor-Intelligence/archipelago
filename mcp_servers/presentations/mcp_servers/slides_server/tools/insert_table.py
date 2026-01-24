import os
from io import BytesIO
from typing import Any

from models.response import InsertTableResponse
from pptx import Presentation
from pptx.util import Inches
from utils.decorators import make_async_background

SLIDES_ROOT = os.getenv("APP_SLIDES_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> str:
    """Map path to the slides root."""
    path = path.lstrip("/")
    full_path = os.path.join(SLIDES_ROOT, path)
    return os.path.normpath(full_path)


@make_async_background
def insert_table(
    file_path: str,
    slide_index: int,
    rows: list[list[Any]],
    header: bool = True,
    x: float = 0.5,
    y: float = 1.5,
    width: float = 9.0,
    height: float = 5.0,
) -> InsertTableResponse:
    """Insert a table into a slide."""

    def error(msg: str) -> InsertTableResponse:
        return InsertTableResponse(success=False, error=msg)

    if not isinstance(file_path, str) or not file_path:
        return error("File path is required")
    if not file_path.startswith("/"):
        return error("File path must start with /")
    if not file_path.lower().endswith(".pptx"):
        return error("File path must end with .pptx")

    if not isinstance(rows, list) or not rows:
        return error("Rows must be a non-empty list")
    if not all(isinstance(row, list) for row in rows):
        return error("Each row must be a list")

    num_cols = len(rows[0])
    if num_cols == 0:
        return error("Rows cannot be empty")
    if not all(len(row) == num_cols for row in rows):
        return error("All rows must have the same number of columns")

    # Load presentation
    target_path = _resolve_under_root(file_path)

    if not os.path.exists(target_path):
        return error(f"File not found: {file_path}")

    try:
        with open(target_path, "rb") as f:
            presentation = Presentation(BytesIO(f.read()))
    except Exception as exc:
        return error(f"Failed to open presentation: {repr(exc)}")

    if slide_index < 0 or slide_index >= len(presentation.slides):
        return error(
            f"Slide index {slide_index} is out of range (0-{len(presentation.slides) - 1})"
        )

    slide = presentation.slides[slide_index]

    table_x, table_y = Inches(x), Inches(y)
    table_width, table_height = Inches(width), Inches(height)

    num_rows = len(rows)
    try:
        graphic_frame = slide.shapes.add_table(
            num_rows, num_cols, table_x, table_y, table_width, table_height
        )
        table = graphic_frame.table

        for r, row_values in enumerate(rows):
            for c, cell_value in enumerate(row_values):
                table.cell(r, c).text = (
                    str(cell_value) if cell_value is not None else ""
                )

        if header and num_rows > 0:
            for cell in table.rows[0].cells:
                for paragraph in cell.text_frame.paragraphs:
                    for run in paragraph.runs:
                        run.font.bold = True

    except Exception as exc:
        return error(f"Failed to create table: {repr(exc)}")

    try:
        presentation.save(target_path)
    except Exception as exc:
        return error(f"Failed to save presentation: {repr(exc)}")

    return InsertTableResponse(
        success=True,
        slide_index=slide_index,
        rows=num_rows,
        cols=num_cols,
    )
