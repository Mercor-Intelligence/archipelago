import os
from typing import Annotated

from fastmcp.utilities.types import Image
from pydantic import Field
from utils.decorators import make_async_background

FS_ROOT = os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> str:
    """Map any incoming path to the sandbox root."""
    if not path or path == "/":
        return FS_ROOT
    rel = os.path.normpath(path).lstrip(os.sep)
    return os.path.join(FS_ROOT, rel)


def _validate_real_path(target_path: str) -> str:
    """Resolve symlinks and validate the real path is within the sandbox.

    Returns the resolved real path if valid, raises ValueError if path escapes sandbox.
    """
    # Resolve any symlinks to get the real path
    real_path = os.path.realpath(target_path)
    # Also resolve FS_ROOT in case it's a symlink or relative path
    real_fs_root = os.path.realpath(FS_ROOT)
    # Ensure the real path is within the sandbox
    if not real_path.startswith(real_fs_root + os.sep) and real_path != real_fs_root:
        raise ValueError("Access denied: path resolves outside sandbox")
    return real_path


@make_async_background
def read_image_file(
    file_path: Annotated[
        str,
        Field(
            description="Path to the image file. REQUIRED. Example: /path/to/image.png"
        ),
    ],
) -> Image:
    """Read an image file from the filesystem."""
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

    target_path = _resolve_under_root(file_path)

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

        # Determine image format
        image_format = {
            "png": "png",
            "jpg": "jpeg",
            "jpeg": "jpeg",
            "gif": "gif",
            "webp": "webp",
        }[file_ext]

        return Image(data=image_data, format=image_format)

    except Exception as exc:
        raise RuntimeError(f"Failed to read image file: {repr(exc)}") from exc
