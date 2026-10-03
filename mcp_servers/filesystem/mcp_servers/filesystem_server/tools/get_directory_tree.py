import os
from typing import Annotated, Literal

from pydantic import BaseModel, Field
from utils.decorators import make_async_background
from utils.path_utils import (
    PathTraversalError,
)
from utils.path_utils import (
    is_path_within_sandbox as _is_path_within_sandbox,
)
from utils.path_utils import (
    resolve_under_root as _resolve_under_root,
)
from utils.path_utils import (
    virtual_path_from_physical as _virtual_path_from_physical,
)
from utils.structured_output import StructuredText

MAX_TREE_ENTRIES = 500


class TreeEntry(BaseModel):
    """One file or directory line of the tree."""

    name: str = Field(
        description="Entry name (not a path); directories have no trailing '/'."
    )
    type: Literal["directory", "file"] = Field(description="'directory' or 'file'.")
    depth: int = Field(
        description="Nesting depth: 1 for immediate children of the root, 2 for their children, and so on. Entries are listed in tree (pre-order) order, so an entry's parent is the closest preceding directory with depth one less."
    )
    size_bytes: int | None = Field(
        default=None,
        description="Files only, when show_size is true and the size could be read.",
    )


class TreeIssue(BaseModel):
    """A directory inside the tree that could not be listed."""

    depth: int = Field(
        description="Depth the unlisted directory's children would have had."
    )
    message: str = Field(
        description="The bracketed text shown in the tree, e.g. '[permission denied]'."
    )


class DirectoryTreeResult(BaseModel):
    """Structured form of the get_directory_tree text output."""

    root: str | None = Field(
        default=None,
        description="The tree's root path, as on the first line of the text (directories other than '/' keep their trailing '/').",
    )
    entries: list[TreeEntry] = Field(
        default_factory=list,
        description="Every file and directory line of the tree, in the order the text shows them. Empty when the directory is empty or on error.",
    )
    issues: list[TreeIssue] = Field(
        default_factory=list,
        description="Directories within the tree that could not be listed.",
    )
    truncated: bool = Field(
        default=False,
        description=f"True when the tree stopped at {MAX_TREE_ENTRIES} entries ('... (truncated at {MAX_TREE_ENTRIES} entries)').",
    )
    error: str | None = Field(
        default=None,
        description="Present only on failure: the same bracketed message as the text, e.g. '[not found: /path]'.",
    )


def _error(text: str) -> StructuredText[DirectoryTreeResult]:
    return StructuredText(text, DirectoryTreeResult(error=text))


def _build_tree(
    base_path: str,
    prefix: str,
    current_depth: int,
    max_depth: int,
    include_files: bool,
    show_size: bool,
    _counter: list[int] | None = None,
    _data: DirectoryTreeResult | None = None,
) -> list[str]:
    """Recursively build directory tree lines.

    When ``_data`` is given, each line is also recorded on it in structured
    form.
    """
    if _counter is None:
        _counter = [0]
    if _data is None:
        _data = DirectoryTreeResult()

    lines: list[str] = []

    if current_depth > max_depth:
        return lines

    if _counter[0] >= MAX_TREE_ENTRIES:
        return lines

    try:
        entries = list(os.scandir(base_path))
    except PermissionError:
        lines.append(f"{prefix}[permission denied]")
        _data.issues.append(
            TreeIssue(depth=current_depth, message="[permission denied]")
        )
        return lines
    except Exception as exc:
        message = f"[error: {repr(exc)}]"
        lines.append(f"{prefix}{message}")
        _data.issues.append(TreeIssue(depth=current_depth, message=message))
        return lines

    # Separate directories and files, sort each
    # Note: is_dir()/is_file() can raise OSError on some filesystems
    # SECURITY: Use follow_symlinks=False to prevent symlinks from escaping sandbox
    dirs = []
    files = []
    for e in entries:
        try:
            if e.is_dir(follow_symlinks=False):
                dirs.append(e)
            elif e.is_file(follow_symlinks=False):
                files.append(e)
            # Symlinks are intentionally skipped to prevent sandbox escape
        except OSError:
            continue
    dirs.sort(key=lambda e: e.name.lower())
    files.sort(key=lambda e: e.name.lower())

    # Combine: directories first, then files
    all_entries = dirs + (files if include_files else [])
    total = len(all_entries)
    dir_set = set(dirs)

    for idx, entry in enumerate(all_entries):
        if _counter[0] >= MAX_TREE_ENTRIES:
            lines.append(f"{prefix}... (truncated at {MAX_TREE_ENTRIES} entries)")
            _data.truncated = True
            break

        is_last = idx == total - 1
        connector = "└── " if is_last else "├── "
        child_prefix = "    " if is_last else "│   "

        _counter[0] += 1

        if entry in dir_set:
            lines.append(f"{prefix}{connector}{entry.name}/")
            _data.entries.append(
                TreeEntry(name=entry.name, type="directory", depth=current_depth)
            )
            if current_depth < max_depth:
                lines.extend(
                    _build_tree(
                        entry.path,
                        prefix + child_prefix,
                        current_depth + 1,
                        max_depth,
                        include_files,
                        show_size,
                        _counter,
                        _data,
                    )
                )
        else:
            # File
            size: int | None = None
            if show_size:
                try:
                    # SECURITY: Use follow_symlinks=False to prevent sandbox escape
                    size = entry.stat(follow_symlinks=False).st_size
                    lines.append(f"{prefix}{connector}{entry.name} ({size} bytes)")
                except OSError:
                    lines.append(f"{prefix}{connector}{entry.name}")
            else:
                lines.append(f"{prefix}{connector}{entry.name}")
            _data.entries.append(
                TreeEntry(
                    name=entry.name, type="file", depth=current_depth, size_bytes=size
                )
            )

    return lines


@make_async_background
def get_directory_tree(
    path: Annotated[
        str,
        Field(
            description="Directory path within the sandbox to display as a tree. Must start with '/'. Default: '/' (sandbox root). Example: '/documents' or '/project/src'. Returns an ASCII tree representation using box-drawing characters. Format: root path on first line, then indented entries with connectors ('+-- ' for last items, '|-- ' for others). Directories end with '/'. Files show '(N bytes)' suffix when show_size is true. Returns '[not found: path]', '[access denied: path]', '[not a directory: path]' for errors, or '(empty)' for empty directories."
        ),
    ] = "/",
    max_depth: Annotated[
        int,
        Field(
            description="Maximum directory depth to traverse, where 1 shows only immediate children. Range: 1-10 (values outside this range are clamped). Default: 3. Higher values show more nested structure but take longer."
        ),
    ] = 3,
    include_files: Annotated[
        bool,
        Field(
            description="Include files in the tree output. Default: true. When false, only directories are shown. Directories are always included regardless of this setting."
        ),
    ] = True,
    show_size: Annotated[
        bool,
        Field(
            description="Show file sizes in bytes next to each file. Default: false. Format: 'filename (N bytes)'. Only applies when include_files is true."
        ),
    ] = False,
) -> StructuredText[DirectoryTreeResult]:
    """Display a directory tree structure with ASCII tree visualization."""
    # Validate and clamp max_depth
    if max_depth < 1:
        max_depth = 1
    elif max_depth > 10:
        max_depth = 10

    if not isinstance(path, str) or not path:
        raise ValueError("Path is required and must be a string")

    if not path.startswith("/"):
        raise ValueError("Path must start with /")

    try:
        base = _resolve_under_root(path)
    except PathTraversalError:
        return _error(f"[access denied: {path}]")

    # SECURITY: Use lexists to check without following symlinks first
    if not os.path.lexists(base):
        return _error(f"[not found: {path}]")

    # SECURITY: Validate path is within sandbox after resolving symlinks
    if not _is_path_within_sandbox(base):
        return _error(f"[access denied: {path}]")

    # Check if it's actually a directory (use realpath for accurate check)
    real_base = os.path.realpath(base)
    if not os.path.isdir(real_base):
        return _error(f"[not a directory: {path}]")

    # Start building the tree. Derive the header from the resolved base so it
    # shows the path form the other tools accept (physical when
    # EXPOSE_PHYSICAL_PATHS is enabled, virtual otherwise).
    display_root = _virtual_path_from_physical(real_base)
    header = display_root if display_root == "/" else f"{display_root}/"
    data = DirectoryTreeResult(root=header)
    lines = [header]

    tree_lines = _build_tree(
        real_base,
        "",
        current_depth=1,
        max_depth=max_depth,
        include_files=include_files,
        show_size=show_size,
        _data=data,
    )

    lines.extend(tree_lines)

    if not tree_lines:
        if path == "/":
            lines.append(
                "(empty - the sandbox root contains no visible files or folders. "
                "Try a more specific path like '/documents' or '/data'.)"
            )
        else:
            lines.append("(empty)")

    return StructuredText("\n".join(lines), data)
