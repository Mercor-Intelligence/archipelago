import os
from typing import Annotated

from pydantic import Field
from utils.decorators import make_async_background

FS_ROOT = os.getenv("APP_FS_ROOT", "/filesystem")


def _is_path_within_sandbox(path: str) -> bool:
    """Check if a path is within the sandbox after resolving symlinks."""
    real_path = os.path.realpath(path)
    # Also resolve FS_ROOT in case it's a symlink or relative path
    real_fs_root = os.path.realpath(FS_ROOT)
    return real_path.startswith(real_fs_root + os.sep) or real_path == real_fs_root


def _resolve_under_root(path: str | None) -> str:
    """Map any incoming path to the sandbox root."""
    if not path or path == "/":
        return FS_ROOT
    rel = os.path.normpath(path).lstrip(os.sep)
    return os.path.join(FS_ROOT, rel)


def _build_tree(
    base_path: str,
    prefix: str,
    current_depth: int,
    max_depth: int,
    include_files: bool,
    show_size: bool,
) -> list[str]:
    """Recursively build directory tree lines."""
    lines = []

    if current_depth > max_depth:
        return lines

    try:
        entries = list(os.scandir(base_path))
    except PermissionError:
        lines.append(f"{prefix}[permission denied]")
        return lines
    except Exception as exc:
        lines.append(f"{prefix}[error: {repr(exc)}]")
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
        is_last = idx == total - 1
        connector = "└── " if is_last else "├── "
        child_prefix = "    " if is_last else "│   "

        if entry in dir_set:
            lines.append(f"{prefix}{connector}{entry.name}/")
            if current_depth < max_depth:
                lines.extend(
                    _build_tree(
                        entry.path,
                        prefix + child_prefix,
                        current_depth + 1,
                        max_depth,
                        include_files,
                        show_size,
                    )
                )
        else:
            # File
            if show_size:
                try:
                    # SECURITY: Use follow_symlinks=False to prevent sandbox escape
                    size = entry.stat(follow_symlinks=False).st_size
                    lines.append(f"{prefix}{connector}{entry.name} ({size} bytes)")
                except OSError:
                    lines.append(f"{prefix}{connector}{entry.name}")
            else:
                lines.append(f"{prefix}{connector}{entry.name}")

    return lines


@make_async_background
def get_directory_tree(
    path: Annotated[
        str,
        Field(
            description="Directory path to display tree for. Default: '/' (root). Example: /documents"
        ),
    ] = "/",
    max_depth: Annotated[
        int,
        Field(description="Maximum depth to traverse. Default: 3. Range: 1-10."),
    ] = 3,
    include_files: Annotated[
        bool,
        Field(
            description="Include files in the tree, not just directories. Default: True"
        ),
    ] = True,
    show_size: Annotated[
        bool,
        Field(description="Show file sizes in the tree. Default: False"),
    ] = False,
) -> str:
    """Display a directory tree structure."""
    # Validate and clamp max_depth
    if max_depth < 1:
        max_depth = 1
    elif max_depth > 10:
        max_depth = 10

    if not isinstance(path, str) or not path:
        raise ValueError("Path is required and must be a string")

    if not path.startswith("/"):
        raise ValueError("Path must start with /")

    base = _resolve_under_root(path)

    # SECURITY: Use lexists to check without following symlinks first
    if not os.path.lexists(base):
        return f"[not found: {path}]"

    # SECURITY: Validate path is within sandbox after resolving symlinks
    if not _is_path_within_sandbox(base):
        return f"[access denied: {path}]"

    # Check if it's actually a directory (use realpath for accurate check)
    real_base = os.path.realpath(base)
    if not os.path.isdir(real_base):
        return f"[not a directory: {path}]"

    # Start building the tree
    if path == "/":
        lines = ["/"]
    else:
        lines = [f"{path}/"]

    tree_lines = _build_tree(
        real_base,
        "",
        current_depth=1,
        max_depth=max_depth,
        include_files=include_files,
        show_size=show_size,
    )

    lines.extend(tree_lines)

    if not tree_lines:
        lines.append("(empty)")

    return "\n".join(lines)
