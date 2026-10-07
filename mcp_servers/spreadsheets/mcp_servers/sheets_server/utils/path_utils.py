import os

from mcp_actor import paths as actor_paths

PathTraversalError = actor_paths.ActorPathError


def get_sheets_root() -> str:
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


def resolve_new_file_path(
    directory: str,
    filename: str,
    *,
    root: str | None = None,
) -> str:
    """Resolve a path for a new file to be created.

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
    if os.sep in filename or (os.altsep and os.altsep in filename):
        raise ValueError(f"Filename cannot contain path separators: {filename}")

    directory = directory.rstrip("/")
    if directory:
        path = f"{directory}/{filename}"
    else:
        path = filename

    return resolve_under_root(path, root=root)
