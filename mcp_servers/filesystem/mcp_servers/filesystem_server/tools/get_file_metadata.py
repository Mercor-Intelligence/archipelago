import mimetypes
import os
import stat
from datetime import UTC, datetime
from typing import Annotated

from pydantic import Field
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


def _format_permissions(mode: int) -> str:
    """Convert file mode to human-readable permissions string."""
    perms = ""
    # Owner
    perms += "r" if mode & stat.S_IRUSR else "-"
    perms += "w" if mode & stat.S_IWUSR else "-"
    perms += "x" if mode & stat.S_IXUSR else "-"
    # Group
    perms += "r" if mode & stat.S_IRGRP else "-"
    perms += "w" if mode & stat.S_IWGRP else "-"
    perms += "x" if mode & stat.S_IXGRP else "-"
    # Other
    perms += "r" if mode & stat.S_IROTH else "-"
    perms += "w" if mode & stat.S_IWOTH else "-"
    perms += "x" if mode & stat.S_IXOTH else "-"
    return perms


def _format_size(size: int) -> str:
    """Format size in human-readable form."""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    else:
        return f"{size / (1024 * 1024 * 1024):.1f} GB"


def _format_timestamp(timestamp: float) -> str:
    """Format timestamp to ISO 8601 format."""
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


@make_async_background
def get_file_metadata(
    file_path: Annotated[
        str,
        Field(
            description="Absolute path to the file or directory within the sandbox filesystem. REQUIRED. Must start with '/'. The path is relative to the sandbox root, not the host system. Example: '/documents/report.pdf' or '/data/config.json'. Returns a newline-separated string containing: Path, Type (file/directory/symlink), MIME type (for files), Size (in bytes and human-readable), Permissions (rwx format and octal), Modified/Accessed/Created timestamps (ISO 8601), Inode, Device, Hard links count. Returns '[not found: path]' if path doesn't exist, '[access denied: path]' if outside sandbox, '[permission denied: path]' for permission errors."
        ),
    ],
) -> str:
    """Return metadata for a file or directory (path, type, size, MIME type, permissions, modified time). Use to check existence and type."""
    if not isinstance(file_path, str) or not file_path:
        raise ValueError("File path is required and must be a string")

    if not file_path.startswith("/"):
        raise ValueError("File path must start with /")

    try:
        target_path = _resolve_under_root(file_path)
    except PathTraversalError:
        return f"[access denied: {file_path}]"

    try:
        if not _is_path_within_sandbox(target_path):
            return f"[access denied: {file_path}]"

        # SECURITY: Use lstat to get info without following symlinks
        stat_result = os.lstat(target_path)
        is_link = stat.S_ISLNK(stat_result.st_mode)
        is_dir = stat.S_ISDIR(stat_result.st_mode)

        # Build metadata output
        lines = []
        lines.append(f"Path: {file_path}")

        if is_link:
            # SECURITY: Check if symlink target is within sandbox
            # Save the resolved real_path to prevent TOCTOU attacks
            real_path = os.path.realpath(target_path)
            is_within_sandbox = _is_path_within_sandbox(real_path)

            if not is_within_sandbox:
                lines.append("Type: symlink (target outside sandbox - access denied)")
                lines.append("Symlink target: (hidden - outside sandbox)")
                return "\n".join(lines)
            try:
                link_target = os.readlink(target_path)
                if os.path.isabs(link_target):
                    link_target = _virtual_path_from_physical(link_target)
                lines.append("Type: symlink")
                lines.append(f"Symlink target: {link_target}")
            except OSError:
                lines.append("Type: symlink")
                lines.append("Symlink target: (unreadable)")
            # For symlinks within sandbox, get stat of the resolved target
            # SECURITY: Use real_path (not target_path) to prevent TOCTOU attacks
            try:
                stat_result = os.stat(real_path)
                is_dir = os.path.isdir(real_path)
            except OSError:
                # Broken symlink - just show symlink info
                return "\n".join(lines)
        else:
            real_path = target_path  # For non-symlinks, real_path is target_path
            lines.append(f"Type: {'directory' if is_dir else 'file'}")

        if not is_dir:
            # Use real_path for MIME type to be consistent with other metadata
            # (for symlinks, this is the resolved target; for regular files, same as target_path)
            mimetype, _ = mimetypes.guess_type(real_path)
            lines.append(f"MIME type: {mimetype or 'unknown'}")

        lines.append(
            f"Size: {stat_result.st_size} bytes ({_format_size(stat_result.st_size)})"
        )
        lines.append(
            f"Permissions: {_format_permissions(stat_result.st_mode)} ({oct(stat_result.st_mode)[-3:]})"
        )
        lines.append(f"Modified: {_format_timestamp(stat_result.st_mtime)}")
        lines.append(f"Accessed: {_format_timestamp(stat_result.st_atime)}")
        lines.append(f"Created/Changed: {_format_timestamp(stat_result.st_ctime)}")

        # Add inode and device info
        lines.append(f"Inode: {stat_result.st_ino}")
        lines.append(f"Device: {stat_result.st_dev}")

        # Add link count
        lines.append(f"Hard links: {stat_result.st_nlink}")

        return "\n".join(lines)

    except FileNotFoundError:
        return f"[not found: {file_path}]"
    except PermissionError:
        return f"[permission denied: {file_path}]"
    except Exception as exc:
        return f"[error: {repr(exc)}]"
