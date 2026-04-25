import os
from typing import Annotated

from pydantic import Field
from utils.decorators import make_async_background
from utils.path_utils import PathTraversalError, resolve_under_root

MAX_LINES = 2000


@make_async_background
def read(
    file_path: Annotated[
        str,
        Field(description="The absolute path to the file to read"),
    ],
    offset: Annotated[
        int | None,
        Field(
            description="The line number to start reading from. Only provide if the file is too large to read at once",
            ge=1,
        ),
    ] = None,
    limit: Annotated[
        int | None,
        Field(
            description="The number of lines to read. Only provide if the file is too large to read at once.",
            ge=1,
            le=10000,
        ),
    ] = None,
    pages: Annotated[
        str | None,
        Field(
            description='Page range for PDF files (e.g., "1-5", "3", "10-20"). Only applicable to PDF files. Maximum 20 pages per request.',
        ),
    ] = None,
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

    effective_offset = offset if offset is not None else 1
    effective_limit = limit if limit is not None else MAX_LINES

    try:
        with open(resolved, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except Exception as exc:
        raise RuntimeError(f"Failed to read file: {exc}") from exc

    total = len(all_lines)
    start = effective_offset - 1  # convert to 0-indexed
    end = min(start + effective_limit, total)
    selected = all_lines[start:end]

    lines_out = []
    for i, line in enumerate(selected, start=effective_offset):
        lines_out.append(f"{i}\t{line}")

    result = "".join(lines_out)

    if end < total:
        result += f"\n(showing lines {effective_offset}–{end} of {total}; use offset={end + 1} to read more)"

    return result
