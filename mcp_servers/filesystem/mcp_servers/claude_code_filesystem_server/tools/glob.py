import os
from pathlib import Path
from typing import Annotated

from pydantic import Field
from utils.decorators import make_async_background
from utils.path_utils import PathTraversalError, get_fs_root, resolve_under_root, to_sandbox_path

MAX_RESULTS = 1000


@make_async_background
def glob(
    pattern: Annotated[
        str,
        Field(description="The glob pattern to match files against"),
    ],
    path: Annotated[
        str | None,
        Field(
            description=(
                "The directory to search in. If not specified, the current working directory will be used. "
                "IMPORTANT: Omit this field to use the default directory. "
                "DO NOT enter 'undefined' or 'null' - simply omit it for the default behavior. "
                "Must be a valid directory path if provided."
            )
        ),
    ] = None,
) -> str:
    """Find files matching a glob pattern. Supports ** for recursive matching."""
    search_path = path if path is not None else "/"

    if not search_path.startswith("/"):
        raise ValueError("path must start with '/'")

    try:
        root = resolve_under_root(search_path)
    except PathTraversalError as exc:
        raise ValueError(str(exc)) from exc

    if not os.path.exists(root):
        return f"[not found: {search_path}]"
    if not os.path.isdir(root):
        return f"[not a directory: {search_path}]"

    fs_root = get_fs_root()
    real_fs_root = os.path.realpath(fs_root)

    try:
        matches: list[str] = []
        for match in Path(root).glob(pattern):
            real_match = os.path.realpath(match)
            if not real_match.startswith(real_fs_root + os.sep) and real_match != real_fs_root:
                continue
            if match.is_file():
                matches.append(to_sandbox_path(str(real_match)))
            if len(matches) >= MAX_RESULTS:
                break
    except Exception as exc:
        return f"[error: {exc}]"

    if not matches:
        return f"No files matching '{pattern}' found in {search_path}"

    result = "\n".join(sorted(matches))
    if len(matches) >= MAX_RESULTS:
        result += f"\n\n(Results limited to {MAX_RESULTS})"
    return result
