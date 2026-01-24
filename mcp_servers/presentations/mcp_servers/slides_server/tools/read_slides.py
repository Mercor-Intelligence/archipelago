import os
from io import BytesIO

from markitdown import MarkItDown
from models.response import ReadRangeResponse
from utils.decorators import make_async_background

SLIDES_ROOT = os.getenv("APP_SLIDES_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")

md = MarkItDown()


def _resolve_under_root(path: str) -> str:
    """Map path to the slides root."""
    path = path.lstrip("/")
    full_path = os.path.join(SLIDES_ROOT, path)
    return os.path.normpath(full_path)


@make_async_background
def read_slides(
    file_path: str, start: int | None = None, end: int | None = None
) -> ReadRangeResponse:
    """Read a character range from a Presentations presentation's text content."""

    def error(msg: str) -> ReadRangeResponse:
        return ReadRangeResponse(success=False, error=msg)

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

    document = md.convert(BytesIO(file_bytes))

    text_content = document.text_content

    # Check for negative values BEFORE applying defaults
    if (start is not None and start < 0) or (end is not None and end < 0):
        return error("Invalid range: start and end must be non-negative")

    # Apply defaults if not provided
    if start is None:
        start = 0
    if end is None:
        end = 500

    if end <= start:
        return error("Invalid range: end must be greater than start")

    if end - start > 10000:
        return error("Invalid range: maximum range is 10,000 characters")

    return ReadRangeResponse(
        success=True,
        content=text_content[start:end],
        start=start,
        end=end,
        total_length=len(text_content),
    )
