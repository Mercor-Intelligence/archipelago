import os
from typing import Annotated

from fastmcp.utilities.types import Image
from pydantic import Field
from utils.decorators import make_async_background
from utils.path_utils import (
    PathTraversalError,
)
from utils.path_utils import (
    resolve_under_root as _resolve_under_root,
)
from utils.path_utils import (
    validate_real_path as _validate_real_path,
)


def _sniff_image_format(image_data: bytes) -> str | None:
    """Detect the image format from magic bytes, not the filename.

    The filename extension cannot be trusted: the shipped
    ``examples/simple_task`` fixtures were JPEG bytes named ``.png``, and
    sending them as ``image/png`` is rejected by Anthropic (issue #205).
    Returns "png", "jpeg", "gif", or "webp", or None if the bytes do not
    match a known signature.
    """
    if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if image_data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if image_data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if image_data.startswith(b"RIFF") and image_data[8:12] == b"WEBP":
        return "webp"
    return None


@make_async_background
def read_image_file(
    file_path: Annotated[
        str,
        Field(
            description="Absolute path to the image file within the sandbox filesystem. REQUIRED. Must start with '/'. Supported formats: PNG, JPG, JPEG, GIF, WEBP (case-insensitive). Example: '/images/screenshot.png' or '/uploads/photo.jpg'. Returns an Image object with 'data' (binary image content) and 'format' (string: 'png', 'jpeg', 'gif', or 'webp'). Raises FileNotFoundError if file doesn't exist, ValueError for unsupported formats or non-file paths, RuntimeError for read failures."
        ),
    ],
) -> Image:
    """Read an image file and return it for vision APIs. Use to pass images to vision-capable agents."""
    if not isinstance(file_path, str) or not file_path:
        raise ValueError("File path is required and must be a string")

    if not file_path.startswith("/"):
        raise ValueError("File path must start with /")

    # Validate file extension
    file_ext = file_path.lower().split(".")[-1]
    if file_ext not in ("png", "jpg", "jpeg", "gif", "webp"):
        raise ValueError(
            f"Unsupported image format: {file_ext}. Supported formats: png, jpg, jpeg, gif, webp"
        )

    try:
        target_path = _resolve_under_root(file_path)
    except PathTraversalError as exc:
        raise ValueError(f"Access denied: {file_path}") from exc

    # SECURITY: Use lstat to check existence without following symlinks
    if not os.path.lexists(target_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    # SECURITY: Validate real path is within sandbox before any file operations
    real_path = _validate_real_path(target_path)

    if not os.path.isfile(real_path):
        raise ValueError(f"Not a file: {file_path}")

    try:
        with open(real_path, "rb") as f:
            image_data = f.read()

        # Determine image format from the actual bytes first: the extension
        # may not match the content (issue #205). Fall back to the extension
        # only when the bytes carry no recognizable signature.
        image_format = (
            _sniff_image_format(image_data)
            or {
                "png": "png",
                "jpg": "jpeg",
                "jpeg": "jpeg",
                "gif": "gif",
                "webp": "webp",
            }[file_ext]
        )

        return Image(data=image_data, format=image_format)

    except Exception as exc:
        raise RuntimeError(f"Failed to read image file: {repr(exc)}") from exc
