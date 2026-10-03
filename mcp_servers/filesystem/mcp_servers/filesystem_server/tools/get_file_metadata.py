import mimetypes
import os
import stat
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field
from utils.decorators import make_async_background
from utils.path_utils import (
    PathTraversalError,
)
from utils.path_utils import (
    display_path as _display_path,
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


class FileMetadataResult(BaseModel):
    """Structured form of the get_file_metadata text output.

    Each field mirrors one 'Key: value' line of the text and is present exactly
    when that line is. A symlink whose target is outside the sandbox, or is
    broken, stops after the symlink fields, as the text does.
    """

    path: str | None = Field(
        default=None,
        description="Resolved path, in the same form the other tools emit and accept ('Path:').",
    )
    type: Literal["file", "directory", "symlink"] | None = Field(
        default=None, description="Entry type ('Type:')."
    )
    symlink_target: str | None = Field(
        default=None,
        description="Symlinks only ('Symlink target:'): the link target, or '(hidden - outside sandbox)' / '(unreadable)'.",
    )
    target_outside_sandbox: bool | None = Field(
        default=None,
        description="Symlinks only: true when the target is outside the sandbox and access is denied.",
    )
    mime_type: str | None = Field(
        default=None,
        description="Non-directories only ('MIME type:'): guessed from the extension, or 'unknown'.",
    )
    size_bytes: int | None = Field(default=None, description="Size in bytes ('Size:').")
    size_human: str | None = Field(
        default=None, description="Human-readable size, e.g. '1.5 KB' ('Size:')."
    )
    permissions: str | None = Field(
        default=None, description="rwx permission string, e.g. 'rw-r--r--'."
    )
    permissions_octal: str | None = Field(
        default=None, description="Octal permission digits, e.g. '644'."
    )
    modified: str | None = Field(
        default=None, description="Last modification time, ISO 8601 UTC."
    )
    accessed: str | None = Field(
        default=None, description="Last access time, ISO 8601 UTC."
    )
    created_changed: str | None = Field(
        default=None,
        description="Creation / metadata-change time (st_ctime), ISO 8601 UTC ('Created/Changed:').",
    )
    inode: int | None = Field(default=None, description="Inode number.")
    device: int | None = Field(default=None, description="Device id.")
    hard_links: int | None = Field(default=None, description="Hard link count.")
    error: str | None = Field(
        default=None,
        description="Present only on failure: the same bracketed message as the text, e.g. '[not found: /path]'.",
    )


def _error(text: str) -> StructuredText[FileMetadataResult]:
    return StructuredText(text, FileMetadataResult(error=text))


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
            description="Absolute path to the file or directory within the sandbox filesystem. REQUIRED. Must start with '/'. Accepts either a '/'-rooted sandbox path or a path as reported by this server's other tools. Example: '/documents/report.pdf' or '/data/config.json'. Returns a newline-separated string containing: Path, Type (file/directory/symlink), MIME type (for files), Size (in bytes and human-readable), Permissions (rwx format and octal), Modified/Accessed/Created timestamps (ISO 8601), Inode, Device, Hard links count. Returns '[not found: path]' if path doesn't exist, '[access denied: path]' if outside sandbox, '[permission denied: path]' for permission errors."
        ),
    ],
) -> StructuredText[FileMetadataResult]:
    """Return metadata for a file or directory (path, type, size, MIME type, permissions, modified time). Use to check existence and type."""
    if not isinstance(file_path, str) or not file_path:
        raise ValueError("File path is required and must be a string")

    if not file_path.startswith("/"):
        raise ValueError("File path must start with /")

    try:
        target_path = _resolve_under_root(file_path)
    except PathTraversalError:
        return _error(f"[access denied: {file_path}]")

    try:
        if not _is_path_within_sandbox(target_path):
            return _error(f"[access denied: {file_path}]")

        # SECURITY: Use lstat to get info without following symlinks
        stat_result = os.lstat(target_path)
        is_link = stat.S_ISLNK(stat_result.st_mode)
        is_dir = stat.S_ISDIR(stat_result.st_mode)

        # Build metadata output. Report the resolved path in the form the other
        # tools emit and accept (physical when EXPOSE_PHYSICAL_PATHS is enabled,
        # virtual otherwise) rather than echoing the caller's input, which may
        # be in either form.
        lines = []
        meta = FileMetadataResult(path=_display_path(target_path))
        lines.append(f"Path: {meta.path}")

        if is_link:
            # SECURITY: Check if symlink target is within sandbox
            # Save the resolved real_path to prevent TOCTOU attacks
            real_path = os.path.realpath(target_path)
            is_within_sandbox = _is_path_within_sandbox(real_path)

            if not is_within_sandbox:
                lines.append("Type: symlink (target outside sandbox - access denied)")
                lines.append("Symlink target: (hidden - outside sandbox)")
                meta.type = "symlink"
                meta.symlink_target = "(hidden - outside sandbox)"
                meta.target_outside_sandbox = True
                return StructuredText("\n".join(lines), meta)
            try:
                link_target = os.readlink(target_path)
                if os.path.isabs(link_target):
                    link_target = _virtual_path_from_physical(link_target)
                lines.append("Type: symlink")
                lines.append(f"Symlink target: {link_target}")
                meta.symlink_target = link_target
            except OSError:
                lines.append("Type: symlink")
                lines.append("Symlink target: (unreadable)")
                meta.symlink_target = "(unreadable)"
            meta.type = "symlink"
            meta.target_outside_sandbox = False
            # For symlinks within sandbox, get stat of the resolved target
            # SECURITY: Use real_path (not target_path) to prevent TOCTOU attacks
            try:
                stat_result = os.stat(real_path)
                is_dir = os.path.isdir(real_path)
            except OSError:
                # Broken symlink - just show symlink info
                return StructuredText("\n".join(lines), meta)
        else:
            real_path = target_path  # For non-symlinks, real_path is target_path
            meta.type = "directory" if is_dir else "file"
            lines.append(f"Type: {meta.type}")

        if not is_dir:
            # Use real_path for MIME type to be consistent with other metadata
            # (for symlinks, this is the resolved target; for regular files, same as target_path)
            mimetype, _ = mimetypes.guess_type(real_path)
            meta.mime_type = mimetype or "unknown"
            lines.append(f"MIME type: {meta.mime_type}")

        meta.size_bytes = stat_result.st_size
        meta.size_human = _format_size(stat_result.st_size)
        lines.append(f"Size: {meta.size_bytes} bytes ({meta.size_human})")
        meta.permissions = _format_permissions(stat_result.st_mode)
        meta.permissions_octal = oct(stat_result.st_mode)[-3:]
        lines.append(f"Permissions: {meta.permissions} ({meta.permissions_octal})")
        meta.modified = _format_timestamp(stat_result.st_mtime)
        meta.accessed = _format_timestamp(stat_result.st_atime)
        meta.created_changed = _format_timestamp(stat_result.st_ctime)
        lines.append(f"Modified: {meta.modified}")
        lines.append(f"Accessed: {meta.accessed}")
        lines.append(f"Created/Changed: {meta.created_changed}")

        # Add inode and device info
        meta.inode = stat_result.st_ino
        meta.device = stat_result.st_dev
        lines.append(f"Inode: {meta.inode}")
        lines.append(f"Device: {meta.device}")

        # Add link count
        meta.hard_links = stat_result.st_nlink
        lines.append(f"Hard links: {meta.hard_links}")

        return StructuredText("\n".join(lines), meta)

    except FileNotFoundError:
        return _error(f"[not found: {file_path}]")
    except PermissionError:
        return _error(f"[permission denied: {file_path}]")
    except Exception as exc:
        return _error(f"[error: {repr(exc)}]")
