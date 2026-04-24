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
        Field(
            description=(
                "Glob pattern to match file paths. Supports wildcards: '*' (any chars in one segment), "
                "'**' (any number of path segments, for recursive matching), '?' (single char). "
                "Examples: '**/*.py' (all Python files), 'docs/*.md' (markdown in docs/), "
                "'**/test_*.py' (all test files). Pattern is matched against sandbox-relative paths."
            )
        ),
    ],
    path: Annotated[
        str,
        Field(
            description="Sandbox-relative directory to search in. Default: '/' (sandbox root). Example: '/src'."
        ),
    ] = "/",
) -> str:
    """Find files matching a glob pattern. Supports ** for recursive matching."""
    if not path.startswith("/"):
        raise ValueError("path must start with '/'")

    try:
        root = resolve_under_root(path)
    except PathTraversalError as exc:
        raise ValueError(str(exc)) from exc

    if not os.path.exists(root):
        return f"[not found: {path}]"
    if not os.path.isdir(root):
        return f"[not a directory: {path}]"

    fs_root = get_fs_root()
    real_fs_root = os.path.realpath(fs_root)

    try:
        matches: list[str] = []
        for match in Path(root).glob(pattern):
            real_match = os.path.realpath(match)
            # Security: skip anything that resolved outside the sandbox
            if not real_match.startswith(real_fs_root + os.sep) and real_match != real_fs_root:
                continue
            if match.is_file():
                matches.append(to_sandbox_path(str(real_match)))
            if len(matches) >= MAX_RESULTS:
                break
    except Exception as exc:
        return f"[error: {exc}]"

    if not matches:
        return f"No files matching '{pattern}' found in {path}"

    result = "\n".join(sorted(matches))
    if len(matches) >= MAX_RESULTS:
        result += f"\n\n(Results limited to {MAX_RESULTS})"
    return result
