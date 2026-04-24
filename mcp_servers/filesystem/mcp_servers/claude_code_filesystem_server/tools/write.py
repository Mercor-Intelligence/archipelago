import os
from typing import Annotated

from pydantic import Field
from utils.decorators import make_async_background
from utils.path_utils import PathTraversalError, resolve_under_root


@make_async_background
def write(
    file_path: Annotated[
        str,
        Field(
            description=(
                "Absolute path for the file to create or overwrite within the sandbox filesystem. "
                "Must start with '/'. Parent directories are created automatically. "
                "Example: '/output/report.txt'."
            )
        ),
    ],
    content: Annotated[
        str,
        Field(description="The full content to write to the file. Overwrites any existing content."),
    ],
) -> str:
    """Write content to a file, creating it (and any parent directories) if it doesn't exist."""
    if not file_path.startswith("/"):
        raise ValueError("file_path must start with '/'")

    try:
        resolved = resolve_under_root(file_path)
    except PathTraversalError as exc:
        raise ValueError(str(exc)) from exc

    parent = os.path.dirname(resolved)
    try:
        os.makedirs(parent, exist_ok=True)
    except Exception as exc:
        raise RuntimeError(f"Failed to create directories for {file_path}: {exc}") from exc

    try:
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as exc:
        raise RuntimeError(f"Failed to write file: {exc}") from exc

    size = len(content.encode("utf-8"))
    return f"Written {size:,} bytes to {file_path}"
