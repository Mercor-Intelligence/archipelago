"""
Filesystem Tools for MCP Servers.

Provides read-only filesystem access tools for browsing and downloading files.
These tools enable UI file browsers to navigate configured filesystem roots.
"""

import base64
import mimetypes
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# ============================================================================
# Pydantic Models
# ============================================================================


class FsRoot(BaseModel):
    """A configured filesystem root."""

    alias: str = Field(..., description="Human-readable alias for this root")
    path: str = Field(..., description="Absolute filesystem path")
    readonly: bool = Field(default=True, description="Whether this root is read-only")


class ListFsRootsRequest(BaseModel):
    """Request to list available filesystem roots."""

    pass  # No parameters needed


class ListFsRootsResponse(BaseModel):
    """List of available filesystem roots."""

    roots: list[FsRoot] = Field(
        default_factory=list,
        description="List of configured filesystem roots",
    )


class FileInfo(BaseModel):
    """Information about a file or directory."""

    name: str = Field(..., description="File or directory name")
    path: str = Field(..., description="Relative path from root")
    is_directory: bool = Field(..., description="True if this is a directory")
    size_bytes: int | None = Field(None, description="File size in bytes (null for directories)")
    modified_at: str | None = Field(None, description="ISO 8601 modification timestamp")
    mime_type: str | None = Field(None, description="MIME type (null for directories)")


class ListFolderRequest(BaseModel):
    """Request to list folder contents."""

    root: str = Field(..., description="Root alias to browse")
    path: str = Field(default="", description="Relative path within the root (empty for root)")


class ListFolderResponse(BaseModel):
    """List of files and directories in a folder."""

    items: list[FileInfo] = Field(
        default_factory=list,
        description="List of files and directories in the folder",
    )
    parent_path: str | None = Field(
        None,
        description="Relative path to parent directory (null if at root)",
    )


class DownloadFileRequest(BaseModel):
    """Request to download a file."""

    root: str = Field(..., description="Root alias containing the file")
    path: str = Field(..., description="Relative path to the file within the root")


class DownloadFileResponse(BaseModel):
    """Downloaded file content."""

    content_base64: str = Field(..., description="Base64-encoded file content")
    mime_type: str | None = Field(None, description="MIME type of the file")
    file_name: str = Field(..., description="Original filename")


# ============================================================================
# Helper Functions
# ============================================================================

# Module-level storage for custom roots registered via create_filesystem_tools
_custom_roots: list[FsRoot] = []


def register_fs_roots(roots: list[FsRoot]) -> None:
    """Register custom filesystem roots.

    Args:
        roots: List of FsRoot objects to register
    """
    global _custom_roots
    _custom_roots = list(roots)


def _get_fs_roots() -> list[FsRoot]:
    """Get the list of configured filesystem roots.

    Returns roots from:
    1. Custom roots registered via register_fs_roots() or create_filesystem_tools()
    2. STATE_LOCATION environment variable (alias: "data")
    3. APP_FS_ROOT environment variable (alias: "files")

    Returns empty list if no roots are configured.
    """
    roots: list[FsRoot] = []
    seen_paths: set[str] = set()

    # First add custom roots (highest priority)
    for root in _custom_roots:
        abs_path = os.path.abspath(root.path)
        if os.path.isdir(abs_path) and abs_path not in seen_paths:
            roots.append(
                FsRoot(
                    alias=root.alias,
                    path=abs_path,
                    readonly=root.readonly,
                )
            )
            seen_paths.add(abs_path)

    # STATE_LOCATION - primary location for app data
    state_location = os.getenv("STATE_LOCATION")
    if state_location and os.path.isdir(state_location):
        abs_path = os.path.abspath(state_location)
        if abs_path not in seen_paths:
            roots.append(
                FsRoot(
                    alias="STATE_LOCATION",
                    path=abs_path,
                    readonly=True,
                )
            )
            seen_paths.add(abs_path)

    # APP_FS_ROOT - alternative/additional location
    app_fs_root = os.getenv("APP_FS_ROOT")
    if app_fs_root and os.path.isdir(app_fs_root):
        abs_path = os.path.abspath(app_fs_root)
        if abs_path not in seen_paths:
            roots.append(
                FsRoot(
                    alias="APP_FS_ROOT",
                    path=abs_path,
                    readonly=True,
                )
            )
            seen_paths.add(abs_path)

    return roots


def _resolve_path(root_alias: str, relative_path: str) -> tuple[Path, FsRoot]:
    """Resolve a relative path within a root to an absolute path.

    Args:
        root_alias: The root alias to use
        relative_path: Relative path within the root

    Returns:
        Tuple of (resolved_absolute_path, root)

    Raises:
        ValueError: If root not found or path is invalid/outside root
    """
    roots = _get_fs_roots()
    root = next((r for r in roots if r.alias == root_alias), None)
    if not root:
        available = ", ".join(r.alias for r in roots) or "(none)"
        raise ValueError(f"Unknown root '{root_alias}'. Available roots: {available}")

    # Normalize the relative path to prevent directory traversal
    # Remove leading slashes and normalize
    clean_path = relative_path.lstrip("/").lstrip("\\")
    if clean_path:
        resolved = Path(root.path) / clean_path
    else:
        resolved = Path(root.path)

    # Ensure the resolved path is within the root
    try:
        resolved = resolved.resolve()
        root_resolved = Path(root.path).resolve()
        resolved.relative_to(root_resolved)
    except ValueError:
        raise ValueError(f"Path '{relative_path}' is outside the allowed root")

    return resolved, root


def _get_mime_type(path: Path) -> str | None:
    """Get MIME type for a file."""
    mime_type, _ = mimetypes.guess_type(str(path))
    return mime_type


def _format_iso_datetime(timestamp: float) -> str:
    """Format a Unix timestamp as ISO 8601 UTC string."""
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


# ============================================================================
# Filesystem Tool Implementations
# ============================================================================


async def list_fs_roots_impl(request: ListFsRootsRequest) -> ListFsRootsResponse:
    """List available filesystem roots.

    Returns the configured filesystem roots that can be browsed.
    Roots are configured via STATE_LOCATION and APP_FS_ROOT environment variables.
    """
    roots = _get_fs_roots()
    return ListFsRootsResponse(roots=roots)


async def list_folder_impl(request: ListFolderRequest) -> ListFolderResponse:
    """List contents of a folder within a filesystem root.

    Returns files and directories at the specified path, sorted with
    directories first, then files, both alphabetically.
    """
    resolved_path, root = _resolve_path(request.root, request.path)

    if not resolved_path.exists():
        raise ValueError(f"Path does not exist: {request.path}")

    if not resolved_path.is_dir():
        raise ValueError(f"Path is not a directory: {request.path}")

    items: list[FileInfo] = []
    root_path = Path(root.path).resolve()

    for entry in resolved_path.iterdir():
        try:
            stat = entry.stat()
            relative = entry.relative_to(root_path)

            if entry.is_dir():
                items.append(
                    FileInfo(
                        name=entry.name,
                        path=str(relative),
                        is_directory=True,
                        size_bytes=None,
                        modified_at=_format_iso_datetime(stat.st_mtime),
                        mime_type=None,
                    )
                )
            else:
                items.append(
                    FileInfo(
                        name=entry.name,
                        path=str(relative),
                        is_directory=False,
                        size_bytes=stat.st_size,
                        modified_at=_format_iso_datetime(stat.st_mtime),
                        mime_type=_get_mime_type(entry),
                    )
                )
        except (PermissionError, OSError):
            # Skip entries we can't access
            continue

    # Sort: directories first, then alphabetically by name
    items.sort(key=lambda x: (not x.is_directory, x.name.lower()))

    # Determine parent path
    parent_path: str | None = None
    if request.path:
        parent = Path(request.path).parent
        if parent != Path("."):
            parent_path = str(parent)
        else:
            parent_path = ""

    return ListFolderResponse(items=items, parent_path=parent_path)


async def download_file_impl(request: DownloadFileRequest) -> DownloadFileResponse:
    """Download a file from a filesystem root.

    Returns the file content as base64-encoded data along with
    MIME type information for proper handling.
    """
    resolved_path, _ = _resolve_path(request.root, request.path)

    if not resolved_path.exists():
        raise ValueError(f"File does not exist: {request.path}")

    if not resolved_path.is_file():
        raise ValueError(f"Path is not a file: {request.path}")

    # Check file size before reading (limit: 100 MB)
    max_file_size = 100 * 1024 * 1024  # 100 MB
    try:
        file_size = resolved_path.stat().st_size
        if file_size > max_file_size:
            raise ValueError(
                f"File too large: {file_size / (1024 * 1024):.1f} MB exceeds "
                f"limit of {max_file_size / (1024 * 1024):.0f} MB"
            )
    except OSError as e:
        raise ValueError(f"Error checking file size: {e}")

    # Read file content
    try:
        with open(resolved_path, "rb") as f:
            content = f.read()
    except PermissionError:
        raise ValueError(f"Permission denied reading file: {request.path}")
    except OSError as e:
        raise ValueError(f"Error reading file: {e}")

    # Encode as base64
    content_base64 = base64.b64encode(content).decode("utf-8")
    mime_type = _get_mime_type(resolved_path)

    return DownloadFileResponse(
        content_base64=content_base64,
        mime_type=mime_type,
        file_name=resolved_path.name,
    )


# ============================================================================
# Tool Registration for MCP Servers
# ============================================================================


def create_filesystem_tools(mcp, public_tool=None, roots: list[FsRoot] | None = None) -> int:
    """
    Register filesystem tools with an MCP server.

    Args:
        mcp: FastMCP instance to register tools with
        public_tool: Optional decorator from mcp_auth. If not provided, tools are
                     registered without auth decoration.
        roots: Optional list of FsRoot objects to register. If provided, these
               roots will be available in addition to environment-based roots.

    Returns:
        Number of tools registered

    Example:
        # Register with custom roots
        create_filesystem_tools(mcp, roots=[
            FsRoot(alias="exports", path="/app/exports", readonly=True),
            FsRoot(alias="uploads", path="/app/uploads", readonly=False),
        ])
    """
    # Register custom roots if provided
    if roots:
        register_fs_roots(roots)

    # Default to no-op decorator if public_tool not provided
    if public_tool is None:
        public_tool = lambda fn: fn  # noqa: E731

    @mcp.tool(name="list_fs_roots")
    @public_tool
    async def list_fs_roots_tool(request: ListFsRootsRequest) -> ListFsRootsResponse:
        """List available filesystem roots for browsing."""
        return await list_fs_roots_impl(request)

    @mcp.tool(name="list_folder")
    @public_tool
    async def list_folder_tool(request: ListFolderRequest) -> ListFolderResponse:
        """List files and directories in a folder."""
        return await list_folder_impl(request)

    @mcp.tool(name="download_file")
    @public_tool
    async def download_file_tool(request: DownloadFileRequest) -> DownloadFileResponse:
        """Download a file as base64-encoded content."""
        return await download_file_impl(request)

    return 3  # Number of tools registered


# ============================================================================
# Legacy Tool Registration (for REST bridge)
# ============================================================================


def get_filesystem_tools() -> dict[str, dict[str, Any]]:
    """
    Get filesystem tools for registration with REST bridge.

    Returns:
        Dictionary of tool name -> tool metadata
    """

    async def list_fs_roots_wrapper(request: ListFsRootsRequest) -> ListFsRootsResponse:
        return await list_fs_roots_impl(request)

    async def list_folder_wrapper(request: ListFolderRequest) -> ListFolderResponse:
        return await list_folder_impl(request)

    async def download_file_wrapper(request: DownloadFileRequest) -> DownloadFileResponse:
        return await download_file_impl(request)

    return {
        "list_fs_roots": {
            "function": list_fs_roots_wrapper,
            "input_model": ListFsRootsRequest,
            "output_model": ListFsRootsResponse,
            "description": "List available filesystem roots for browsing",
        },
        "list_folder": {
            "function": list_folder_wrapper,
            "input_model": ListFolderRequest,
            "output_model": ListFolderResponse,
            "description": "List files and directories in a folder",
        },
        "download_file": {
            "function": download_file_wrapper,
            "input_model": DownloadFileRequest,
            "output_model": DownloadFileResponse,
            "description": "Download a file as base64-encoded content",
        },
    }
