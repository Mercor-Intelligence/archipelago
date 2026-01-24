import os
from io import BytesIO

from models.response import AddImageResponse
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
def add_image(
    file_path: str,
    image_path: str,
    slide_index: int,
    x: float = 1.0,
    y: float = 1.5,
    width: float | None = None,
    height: float | None = None,
) -> AddImageResponse:
    """Add an image to a slide at the specified position."""

    def error(msg: str) -> AddImageResponse:
        return AddImageResponse(success=False, error=msg)

    if not isinstance(file_path, str) or not file_path:
        return error("File path is required")
    if not file_path.startswith("/"):
        return error("File path must start with /")
    if not file_path.lower().endswith(".pptx"):
        return error("File path must end with .pptx")

    if not isinstance(image_path, str) or not image_path:
        return error("Image path is required")
    if not image_path.startswith("/"):
        return error("Image path must start with /")

    image_ext = image_path.lower().split(".")[-1]
    if image_ext not in ("jpg", "jpeg", "png"):
        return error(
            f"Unsupported image format: {image_ext}. Supported formats: jpg, jpeg, png"
        )

    target_path = _resolve_under_root(file_path)

    if not os.path.exists(target_path):
        return error(f"File not found: {file_path}")

    try:
        with open(target_path, "rb") as f:
            presentation = Presentation(BytesIO(f.read()))
    except Exception as exc:
        return error(f"Failed to open presentation: {repr(exc)}")

    if slide_index < 0 or slide_index >= len(presentation.slides):
        if len(presentation.slides) == 0:
            return error(
                f"Slide index {slide_index} is invalid: presentation has no slides"
            )
        return error(
            f"Slide index {slide_index} is out of range (0-{len(presentation.slides) - 1})"
        )

    slide = presentation.slides[slide_index]

    image_full_path = _resolve_under_root(image_path)

    if not os.path.exists(image_full_path):
        return error(f"Image file not found: {image_path}")

    try:
        left = Inches(x)
        top = Inches(y)

        if width is not None and height is not None:
            slide.shapes.add_picture(
                image_full_path, left, top, width=Inches(width), height=Inches(height)
            )
        elif width is not None:
            slide.shapes.add_picture(image_full_path, left, top, width=Inches(width))
        elif height is not None:
            slide.shapes.add_picture(image_full_path, left, top, height=Inches(height))
        else:
            slide.shapes.add_picture(image_full_path, left, top)

    except Exception as exc:
        return error(f"Failed to add image: {repr(exc)}")

    try:
        presentation.save(target_path)
    except Exception as exc:
        return error(f"Failed to save presentation: {repr(exc)}")

    return AddImageResponse(
        success=True,
        slide_index=slide_index,
        position=(x, y),
    )
