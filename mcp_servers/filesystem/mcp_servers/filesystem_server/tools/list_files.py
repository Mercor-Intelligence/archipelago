import mimetypes
import os
from typing import Annotated, Literal

from pydantic import BaseModel, Field
from utils.decorators import make_async_background
from utils.path_utils import PathTraversalError
from utils.path_utils import resolve_under_root as _resolve_under_root
from utils.structured_output import StructuredText


class ListedEntry(BaseModel):
    """One directory entry, as listed on one line of the text output."""

    name: str = Field(description="Entry name (not a path).")
    type: Literal["folder", "file"] = Field(description="'folder' or 'file'.")
    mime_type: str | None = Field(
        default=None,
        description="Files only: MIME type guessed from the extension, or 'unknown'.",
    )
    size_bytes: int | None = Field(
        default=None, description="Files only: size in bytes."
    )


class ListFilesResult(BaseModel):
    """Structured form of the list_files text output."""

    entries: list[ListedEntry] = Field(
        default_factory=list,
        description="Entries in the listed directory, in the order the text lists them. Empty when the directory is empty or on error.",
    )
    error: str | None = Field(
        default=None,
        description="Present only on failure: the same bracketed message as the text, e.g. '[not found: /path]'.",
    )


def _error(text: str) -> StructuredText[ListFilesResult]:
    return StructuredText(text, ListFilesResult(error=text.rstrip("\n")))


@make_async_background
def list_files(
    path: Annotated[
        str,
        Field(
            description="Absolute path within the sandbox filesystem to list. Must start with '/'. Accepts either a '/'-rooted sandbox path or a path as reported by this server's other tools. Default: '/' (sandbox root). Example: '/documents' or '/data/uploads'. Returns a newline-separated string where each line describes one entry: \"'name' (folder)\\n\" for directories, \"'name' (mime/type file) N bytes\\n\" for files. MIME type is guessed from extension ('unknown' if undetectable). Returns '[not found: path]', '[permission denied: path]', or '[not a directory: path]' for errors. Returns 'No items found' for empty directories."
        ),
    ] = "/",
) -> StructuredText[ListFilesResult]:
    """List files and folders in a path; each entry shows name and type (file/folder). Use to browse a directory."""
    try:
        base = _resolve_under_root(path)
    except PathTraversalError:
        return _error(f"[access denied: {path}]\n")

    if not os.path.exists(base):
        return _error(f"[not found: {path}]\n")
    if not os.path.isdir(base):
        return _error(f"[not a directory: {path}]\n")

    items = ""
    entries_data: list[ListedEntry] = []
    try:
        with os.scandir(base) as entries:
            for entry in entries:
                if entry.is_dir():
                    items += f"'{entry.name}' (folder)\n"
                    entries_data.append(ListedEntry(name=entry.name, type="folder"))
                elif entry.is_file():
                    mimetype, _ = mimetypes.guess_type(entry.path)
                    stat_result = entry.stat()
                    items += f"'{entry.name}' ({mimetype or 'unknown'} file) {stat_result.st_size} bytes\n"
                    entries_data.append(
                        ListedEntry(
                            name=entry.name,
                            type="file",
                            mime_type=mimetype or "unknown",
                            size_bytes=stat_result.st_size,
                        )
                    )
    except FileNotFoundError:
        return _error(f"[not found: {path}]\n")
    except PermissionError:
        return _error(f"[permission denied: {path}]\n")
    except NotADirectoryError:
        return _error(f"[not a directory: {path}]\n")

    if not items:
        if not path or path == "/":
            items = (
                "Directory is empty. If you expected files here, use a more specific "
                "path (e.g., '/documents', '/data'). The root '/' maps to the sandbox "
                "root which may not contain files at the top level."
            )
        else:
            items = f"No items found in '{path}'"

    return StructuredText(items, ListFilesResult(entries=entries_data))
