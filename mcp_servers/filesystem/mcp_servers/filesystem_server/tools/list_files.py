import mimetypes
import os
from typing import Annotated

from pydantic import Field
from utils.decorators import make_async_background

FS_ROOT = os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(p: str | None) -> str:
    """Map any incoming path to the sandbox root."""
    if not p or p == "/":
        return FS_ROOT
    rel = os.path.normpath(p).lstrip(os.sep)
    return os.path.join(FS_ROOT, rel)


@make_async_background
def list_files(
    path: Annotated[
        str,
        Field(
            description="Directory path to list. Default: '/' (root). Example: /documents"
        ),
    ] = "/",
) -> str:
    """List files and folders in the given path."""
    base = _resolve_under_root(path)
    items = ""
    try:
        with os.scandir(base) as entries:
            for entry in entries:
                if entry.is_dir():
                    items += f"'{entry.name}' (folder)\n"
                elif entry.is_file():
                    mimetype, _ = mimetypes.guess_type(entry.path)
                    stat_result = entry.stat()
                    items += f"'{entry.name}' ({mimetype or 'unknown'} file) {stat_result.st_size} bytes\n"
    except FileNotFoundError:
        items = f"[not found: {path}]\n"
    except PermissionError:
        items = f"[permission denied: {path}]\n"
    except NotADirectoryError:
        items = f"[not a directory: {path}]\n"

    if not items:
        items = "No items found"

    return items
