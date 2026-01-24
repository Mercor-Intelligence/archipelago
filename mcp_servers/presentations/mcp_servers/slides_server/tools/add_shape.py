import os
from io import BytesIO
from typing import Literal

from models.response import AddShapeResponse
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Inches, Pt
from utils.decorators import make_async_background

SLIDES_ROOT = os.getenv("APP_SLIDES_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")

ShapeType = Literal[
    "rectangle",
    "rounded_rectangle",
    "oval",
    "triangle",
    "right_arrow",
    "left_arrow",
    "up_arrow",
    "down_arrow",
    "pentagon",
    "hexagon",
    "star",
    "heart",
    "lightning_bolt",
    "cloud",
]

SHAPE_TYPE_MAP = {
    "rectangle": MSO_SHAPE.RECTANGLE,
    "rounded_rectangle": MSO_SHAPE.ROUNDED_RECTANGLE,
    "oval": MSO_SHAPE.OVAL,
    "triangle": MSO_SHAPE.ISOSCELES_TRIANGLE,
    "right_arrow": MSO_SHAPE.RIGHT_ARROW,
    "left_arrow": MSO_SHAPE.LEFT_ARROW,
    "up_arrow": MSO_SHAPE.UP_ARROW,
    "down_arrow": MSO_SHAPE.DOWN_ARROW,
    "pentagon": MSO_SHAPE.PENTAGON,
    "hexagon": MSO_SHAPE.HEXAGON,
    "star": MSO_SHAPE.STAR_5_POINT,
    "heart": MSO_SHAPE.HEART,
    "lightning_bolt": MSO_SHAPE.LIGHTNING_BOLT,
    "cloud": MSO_SHAPE.CLOUD,
}


def _resolve_under_root(path: str) -> str:
    """Map path to the slides root."""
    path = path.lstrip("/")
    full_path = os.path.join(SLIDES_ROOT, path)
    return os.path.normpath(full_path)


def _parse_color(value: str) -> RGBColor:
    """Parse a hex color string into an RGBColor object."""
    s = value.strip().lstrip("#").upper()
    r = int(s[0:2], 16)
    g = int(s[2:4], 16)
    b = int(s[4:6], 16)
    return RGBColor(r, g, b)


@make_async_background
def add_shape(
    file_path: str,
    slide_index: int,
    shape_type: ShapeType,
    x: float = 1.0,
    y: float = 1.0,
    width: float = 2.0,
    height: float = 2.0,
    fill_color: str | None = None,
    line_color: str | None = None,
    line_width: float | None = None,
    text: str | None = None,
    text_color: str | None = None,
    font_size: float | None = None,
) -> AddShapeResponse:
    """Add a shape to a slide with optional fill, line, and text styling."""

    def error(msg: str) -> AddShapeResponse:
        return AddShapeResponse(success=False, error=msg)

    # Validate file path
    if not isinstance(file_path, str) or not file_path:
        return error("File path is required")
    if not file_path.startswith("/"):
        return error("File path must start with /")
    if not file_path.lower().endswith(".pptx"):
        return error("File path must end with .pptx")

    # Validate shape type
    valid_shapes = set(SHAPE_TYPE_MAP.keys())
    if shape_type not in valid_shapes:
        return error(f"Shape type must be one of: {sorted(valid_shapes)}")

    # Validate dimensions
    if width <= 0 or height <= 0:
        return error("Width and height must be positive numbers")

    # Validate font_size if provided
    if font_size is not None and font_size <= 0:
        return error("font_size must be a positive number")

    # Validate line_width if provided
    if line_width is not None and line_width <= 0:
        return error("line_width must be a positive number")

    # Validate colors if provided
    def validate_color(color: str | None, name: str) -> str | None:
        if color is None:
            return None
        c = color.strip().lstrip("#").upper()
        if len(c) != 6:
            return f"{name} must be a 6-hex RGB string like 'FF0000' or '#FF0000'"
        try:
            int(c, 16)
        except ValueError:
            return f"{name} must be a valid hex string like 'FF0000' or '#FF0000'"
        return None

    fill_error = validate_color(fill_color, "fill_color")
    if fill_error:
        return error(fill_error)

    line_error = validate_color(line_color, "line_color")
    if line_error:
        return error(line_error)

    text_color_error = validate_color(text_color, "text_color")
    if text_color_error:
        return error(text_color_error)

    # Load presentation
    target_path = _resolve_under_root(file_path)

    if not os.path.exists(target_path):
        return error(f"File not found: {file_path}")

    try:
        with open(target_path, "rb") as f:
            presentation = Presentation(BytesIO(f.read()))
    except Exception as exc:
        return error(f"Failed to open presentation: {repr(exc)}")

    # Validate slide index
    if slide_index < 0 or slide_index >= len(presentation.slides):
        if len(presentation.slides) == 0:
            return error(
                f"Slide index {slide_index} is invalid: presentation has no slides"
            )
        return error(
            f"Slide index {slide_index} is out of range (0-{len(presentation.slides) - 1})"
        )

    slide = presentation.slides[slide_index]

    # Add shape
    try:
        mso_shape = SHAPE_TYPE_MAP[shape_type]
        shape = slide.shapes.add_shape(
            mso_shape,
            Inches(x),
            Inches(y),
            Inches(width),
            Inches(height),
        )

        # Apply fill color
        if fill_color:
            shape.fill.solid()
            shape.fill.fore_color.rgb = _parse_color(fill_color)

        # Apply line color and width
        if line_color:
            shape.line.color.rgb = _parse_color(line_color)
        if line_width is not None:
            shape.line.width = Pt(line_width)

        # Add text if provided
        if text:
            shape.text = text
            if shape.text_frame.paragraphs and (text_color or font_size):
                paragraph = shape.text_frame.paragraphs[0]
                # Ensure runs exist for styling (follow pattern from edit_slides.py)
                if not paragraph.runs:
                    paragraph.add_run("")
                run = paragraph.runs[0]
                if text_color:
                    run.font.color.rgb = _parse_color(text_color)
                if font_size:
                    run.font.size = Pt(font_size)

    except Exception as exc:
        return error(f"Failed to add shape: {repr(exc)}")

    # Save presentation
    try:
        presentation.save(target_path)
    except Exception as exc:
        return error(f"Failed to save presentation: {repr(exc)}")

    return AddShapeResponse(
        success=True,
        slide_index=slide_index,
        shape_type=shape_type,
        position=(x, y),
    )
