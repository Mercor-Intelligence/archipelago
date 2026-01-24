import os
from io import BytesIO

from models.response import ReadDeckResponse, SlideOverviewData
from pptx import Presentation
from pptx.shapes.autoshape import Shape
from utils.decorators import make_async_background

SLIDES_ROOT = os.getenv("APP_SLIDES_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> str:
    """Map path to the slides root."""
    path = path.lstrip("/")
    full_path = os.path.join(SLIDES_ROOT, path)
    return os.path.normpath(full_path)


@make_async_background
def read_completedeck(file_path: str) -> ReadDeckResponse:
    """Read all slides from a presentation and return overview with indices."""

    def error(msg: str) -> ReadDeckResponse:
        return ReadDeckResponse(success=False, error=msg)

    if not isinstance(file_path, str) or not file_path:
        return error("File path is required")
    if not file_path.startswith("/"):
        return error("File path must start with /")
    if not file_path.lower().endswith(".pptx"):
        return error("File path must end with .pptx")

    target_path = _resolve_under_root(file_path)

    try:
        if not os.path.exists(target_path):
            return error(f"File not found: {file_path}")
        if not os.path.isfile(target_path):
            return error(f"Not a file: {file_path}")

        with open(target_path, "rb") as f:
            file_bytes = f.read()
    except Exception as exc:
        return error(f"Failed to read presentation: {repr(exc)}")

    try:
        presentation = Presentation(BytesIO(file_bytes))

        slides_data = []

        for slide_index, slide in enumerate(presentation.slides):
            # Extract title (usually from the first placeholder)
            title = ""
            content_parts = []

            for shape in slide.shapes:
                # Only process shapes that have text
                if not isinstance(shape, Shape) or not shape.has_text_frame:
                    continue

                if hasattr(shape, "text") and shape.text.strip():
                    text = shape.text.strip()

                    # Try to detect if this is the title
                    if (
                        not title
                        and hasattr(shape, "is_placeholder")
                        and shape.is_placeholder
                    ):
                        try:
                            if shape.placeholder_format.type in (0, 2):
                                title = text
                                continue
                        except (AttributeError, ValueError):
                            pass

                    if not title and not content_parts:
                        title = text
                    else:
                        content_parts.append(text)

            if not title:
                title = f"Slide {slide_index}"

            content = "\n".join(content_parts) if content_parts else "(No content)"

            slides_data.append(
                SlideOverviewData(
                    slide_index=slide_index,
                    title=title,
                    content=content,
                )
            )

        return ReadDeckResponse(
            success=True,
            total_slides=len(presentation.slides),
            slides=slides_data,
        )

    except Exception as exc:
        return error(f"Failed to parse presentation: {repr(exc)}")
