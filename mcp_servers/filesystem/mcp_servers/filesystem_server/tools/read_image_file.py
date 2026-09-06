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

_EXT_TO_FORMAT = {
    "png": "png",
    "jpg": "jpeg",
    "jpeg": "jpeg",
    "gif": "gif",
    "webp": "webp",
}


def _sniff_image_format(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
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

        image_format = _sniff_image_format(image_data) or _EXT_TO_FORMAT[file_ext]

        return Image(data=image_data, format=image_format)

    except Exception as exc:
        raise RuntimeError(f"Failed to read image file: {repr(exc)}") from exc
