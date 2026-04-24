import os
from typing import Annotated

from pydantic import Field
from utils.decorators import make_async_background
from utils.path_utils import PathTraversalError, resolve_under_root


@make_async_background
def edit(
    file_path: Annotated[
        str,
        Field(
            description=(
                "Absolute path to the file to edit within the sandbox filesystem. Must start with '/'. "
                "Example: '/src/main.py'."
            )
        ),
    ],
    old_string: Annotated[
        str,
        Field(
            description=(
                "The exact string to find in the file. Must appear exactly once — "
                "if it appears zero or multiple times the edit is rejected. "
                "Include enough surrounding context to make the match unique."
            )
        ),
    ],
    new_string: Annotated[
        str,
        Field(description="The string to replace old_string with. May be empty to delete the matched text."),
    ],
) -> str:
    """Find and replace a unique string in a file. old_string must appear exactly once."""
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
    if count > 1:
        raise ValueError(
            f"old_string appears {count} times in {file_path}. "
            "Add more surrounding context to make it unique."
        )

    new_content = content.replace(old_string, new_string, 1)

    try:
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as exc:
        raise RuntimeError(f"Failed to write file: {exc}") from exc

    return f"Replaced 1 occurrence in {file_path}"
