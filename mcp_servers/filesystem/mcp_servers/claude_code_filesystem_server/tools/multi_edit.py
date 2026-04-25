import os
from typing import Annotated

from pydantic import BaseModel, Field
from utils.decorators import make_async_background
from utils.path_utils import PathTraversalError, resolve_under_root


class EditOperation(BaseModel):
    old_string: str = Field(
        description=(
            "The exact string to find. Must appear exactly once in the file at the time "
            "this edit is applied. Include enough surrounding context to make it unique."
        )
    )
    new_string: str = Field(
        description="The string to replace old_string with. May be empty to delete the matched text."
    )


@make_async_background
def multi_edit(
    file_path: Annotated[
        str,
        Field(
            description=(
                "Absolute path to the file to edit within the sandbox filesystem. Must start with '/'. "
                "Example: '/src/main.py'."
            )
        ),
    ],
    edits: Annotated[
        list[EditOperation],
        Field(
            description=(
                "Ordered list of edit operations to apply. Each edit is applied sequentially to the "
                "result of the previous one, so later edits should reference the post-edit text. "
                "All edits are validated before any are written; if any old_string is not unique the "
                "entire operation is rejected."
            ),
            min_length=1,
        ),
    ],
) -> str:
    """Apply multiple find-and-replace edits to a file atomically. Edits are applied in order."""
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

    # Apply edits sequentially, validating each against the running content.
    for i, op in enumerate(edits):
        count = content.count(op.old_string)
        if count == 0:
            raise ValueError(f"Edit {i}: old_string not found in {file_path}")
        if count > 1:
            raise ValueError(
                f"Edit {i}: old_string appears {count} times in {file_path}. "
                "Add more surrounding context to make it unique."
            )
        content = content.replace(op.old_string, op.new_string, 1)

    try:
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as exc:
        raise RuntimeError(f"Failed to write file: {exc}") from exc

    return f"Applied {len(edits)} edit(s) to {file_path}"
