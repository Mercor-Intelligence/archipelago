import os
from typing import Annotated

from pydantic import Field
from utils.decorators import make_async_background
from utils.path_utils import PathTraversalError, resolve_under_root

FS_ROOT = os.getenv("APP_FS_ROOT", "/filesystem")
MAX_LINES = 2000


@make_async_background
def read(
    file_path: Annotated[
        str,
        Field(
            description=(
                "Absolute path to the file within the sandbox filesystem. Must start with '/'. "
                "Example: '/documents/report.txt' or '/src/main.py'."
            )
        ),
    ],
    offset: Annotated[
        int,
        Field(
            description="Line number to start reading from (1-indexed). Default: 1 (start of file).",
            ge=1,
        ),
    ] = 1,
    limit: Annotated[
        int,
        Field(
            description=f"Maximum number of lines to read. Default: {MAX_LINES}.",
            ge=1,
            le=10000,
        ),
    ] = MAX_LINES,
) -> str:
    """Read the contents of a file with optional line range. Returns content with line numbers."""
    if not file_path.startswith("/"):
        raise ValueError("file_path must start with '/'")

    try:
        resolved = resolve_under_root(file_path)
    except PathTraversalError as exc:
        raise ValueError(str(exc)) from exc

    if not os.path.exists(resolved):
        raise FileNotFoundError(f"File not found: {file_path}")
    if not os.path.isfile(resolved):
        raise ValueError(f"Not a file: {file_path}")

    try:
        with open(resolved, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except Exception as exc:
        raise RuntimeError(f"Failed to read file: {exc}") from exc

    total = len(all_lines)
    start = offset - 1  # convert to 0-indexed
    end = min(start + limit, total)
    selected = all_lines[start:end]

    lines_out = []
    for i, line in enumerate(selected, start=offset):
        lines_out.append(f"{i}\t{line}")

    result = "".join(lines_out)

    if end < total:
        result += f"\n(showing lines {offset}–{end} of {total}; use offset={end + 1} to read more)"

    return result
