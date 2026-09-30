import os
from os import PathLike

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

    # Strip slashes from directory
    directory = directory.strip("/")

    # Combine directory and filename
    if directory:
        path = f"{directory}/{filename}"
    else:
        path = filename

    return resolve_under_root(path, root=root)


def virtual_path_from_physical(
    path: str | PathLike[str], root: str | None = None
) -> str:
    return actor_paths.virtual_path_from_physical(path, root=root)
