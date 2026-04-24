import mimetypes
import os
from typing import Annotated

from pydantic import Field
from utils.decorators import make_async_background
from utils.path_utils import PathTraversalError, resolve_under_root


@make_async_background
def ls(
    path: Annotated[
        str,
        Field(
            description=(
                "Sandbox-relative directory path to list. Must start with '/'. "
                "Default: '/' (sandbox root). Example: '/documents' or '/src/utils'."
            )
        ),
    ] = "/",
) -> str:
    """List files and directories at the given path. Shows name, type, and size for files."""
    try:
        resolved = resolve_under_root(path)
    except PathTraversalError as exc:
        raise ValueError(str(exc)) from exc

    if not os.path.exists(resolved):
        return f"[not found: {path}]"
    if not os.path.isdir(resolved):
        return f"[not a directory: {path}]"

    try:
        entries = sorted(os.scandir(resolved), key=lambda e: (not e.is_dir(), e.name.lower()))
    except PermissionError:
        return f"[permission denied: {path}]"
    except Exception as exc:
        return f"[error: {exc}]"

    if not entries:
        return f"(empty directory: {path})"

    lines: list[str] = []
    for entry in entries:
        if entry.is_dir(follow_symlinks=False):
            lines.append(f"{entry.name}/")
        elif entry.is_file(follow_symlinks=False):
            try:
                size = entry.stat().st_size
                mime, _ = mimetypes.guess_type(entry.path)
                mime_str = f" [{mime}]" if mime else ""
                lines.append(f"{entry.name}{mime_str} ({size:,} bytes)")
            except OSError:
                lines.append(entry.name)
        else:
            lines.append(f"{entry.name} (symlink)")

    return "\n".join(lines)
