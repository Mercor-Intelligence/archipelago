import os
from os import PathLike
from pathlib import Path

from mcp_actor import paths as actor_paths

PathTraversalError = actor_paths.ActorPathError


def get_docs_root() -> str:
    return actor_paths.active_filesystem_root()


def resolve_under_root(
    path: str,
    *,
    root: str | None = None,
    check_exists: bool = False,
    must_be_file: bool = False,
    must_be_dir: bool = False,
) -> str:
    return actor_paths.resolve_virtual_path(
        path,
        root=root,
        check_exists=check_exists,
        must_be_file=must_be_file,
        must_be_dir=must_be_dir,
    )


def resolve_file_under_root(
    path: str,
    *,
    root: str | None = None,
    check_exists: bool = False,
) -> str:
    """Resolve a file path under the sandbox root.

    Convenience wrapper around resolve_under_root for file paths.
    If check_exists is True, also validates that the path is a file.
    """
    return resolve_under_root(
        path,
        root=root,
        check_exists=check_exists,
        must_be_file=check_exists,  # Only check file type if checking existence
    )


def resolve_dir_under_root(
    path: str,
    *,
    root: str | None = None,
    check_exists: bool = False,
) -> str:
    """Resolve a directory path under the sandbox root.

    Convenience wrapper around resolve_under_root for directory paths.
    If check_exists is True, also validates that the path is a directory.
    """
    return resolve_under_root(
        path,
        root=root,
        check_exists=check_exists,
        must_be_dir=check_exists,  # Only check dir type if checking existence
    )


def resolve_new_file_path(
    directory: str,
    filename: str,
    *,
    root: str | None = None,
) -> str:
    """Resolve a path for a new file to be created.

    This combines a directory path and filename, ensuring the result
    stays within the sandbox.

    Args:
        directory: Directory path (may include leading slash)
        filename: The filename (should not include path separators)
        root: Override the root directory

    Returns:
        The fully resolved path for the new file

    Raises:
        PathTraversalError: If the resolved path escapes the sandbox
        ValueError: If filename contains path separators
    """
    # Validate filename doesn't contain path separators
    if os.sep in filename or (os.altsep and os.altsep in filename):
        raise ValueError(f"Filename cannot contain path separators: {filename}")

    # Keep a leading slash so a mount-spelled directory ("/filesystem/reports")
    # is resolved as the mount, not re-rooted to <root>/filesystem/reports
    # (ENV-494). A '/'-rooted virtual directory resolves the same either way.
    directory = directory.rstrip("/")

    # Combine directory and filename
    if directory:
        path = f"{directory}/{filename}"
    else:
        path = filename

    return resolve_under_root(path, root=root)


def virtual_path_from_physical(
    path: str | PathLike[str], root: str | None = None
) -> str:
    return mount_safe_virtual_path(
        actor_paths.virtual_path_from_physical(path, root=root), root=root
    )


def _is_public_root(root_path: Path) -> bool:
    """Whether ``root_path`` is the shared target-agent/coordinator mount.

    That mount (``/filesystem``) is not secret: mercor-mcp-shared stopped
    redacting it from target-agent output (ENV-494). A VCA root is private and
    never qualifies.
    """
    public = Path(actor_paths.TARGET_AGENT_FILESYSTEM_ROOT).absolute()
    return root_path == public or root_path.resolve() == public.resolve()


def mount_safe_virtual_path(virtual: str, root: str | None = None) -> str:
    """Spell a virtual path so the resolver maps it back to the same file.

    Input paths are resolved in ``mcp_actor``'s default "physical" mode, where a
    leading mount spelling is stripped: ``/filesystem/x`` and ``/x`` both name
    ``<root>/x``. A seeded tree can hold a top-level folder spelled like the
    mount (ENV-494: ``/filesystem/filesystem/...``), and its virtual path
    ``/filesystem/...`` would then be read back as ``<root>/...``, a different
    or nonexistent file. For exactly those paths the mount prefix is kept,
    which is also the real path the code-execution app sees. Every other path
    is returned unchanged.

    Purely lexical, and only for the public mount, so a VCA's private root is
    never prepended to its output. Paths that are not '/'-rooted (including
    the ``OUTSIDE_ACTOR_ROOT`` sentinel) pass through untouched.
    """
    if not virtual.startswith("/"):
        return virtual
    root_path = Path(root or actor_paths.active_filesystem_root()).absolute()
    if not _is_public_root(root_path):
        return virtual
    spellings = {str(root_path).rstrip("/"), str(root_path.resolve()).rstrip("/")}
    for spelling in spellings:
        if spelling and (virtual == spelling or virtual.startswith(spelling + "/")):
            return str(root_path).rstrip("/") + virtual
    return virtual
