import os
from typing import Annotated

from pydantic import Field
from utils.decorators import make_async_background
from utils.path_utils import PathTraversalError, resolve_under_root


@make_async_background
def edit(
    file_path: Annotated[
        str,
        Field(description="The absolute path to the file to modify"),
    ],
    old_string: Annotated[
        str,
        Field(description="The text to replace"),
    ],
    new_string: Annotated[
        str,
        Field(description="The text to replace it with (must be different from old_string)"),
    ],
    replace_all: Annotated[
        bool,
        Field(description="Replace all occurrences of old_string (default false)"),
    ] = False,
) -> str:
    """Find and replace a string in a file. By default old_string must appear exactly once."""
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
        with open(resolved, encoding="utf-8") as f:
            content = f.read()
    except Exception as exc:
        raise RuntimeError(f"Failed to read file: {exc}") from exc

    count = content.count(old_string)
    if count == 0:
        raise ValueError(f"old_string not found in {file_path}")
    if not replace_all and count > 1:
        raise ValueError(
            f"old_string appears {count} times in {file_path}. "
            "Add more surrounding context to make it unique, or set replace_all=true."
        )

    new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)

    try:
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as exc:
        raise RuntimeError(f"Failed to write file: {exc}") from exc

    return f"Replaced {count if replace_all else 1} occurrence(s) in {file_path}"
