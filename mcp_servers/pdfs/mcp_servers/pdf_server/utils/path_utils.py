from os import PathLike

from mcp_actor import paths as actor_paths

PathTraversalError = actor_paths.ActorPathError


def get_pdf_root() -> str:
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


def is_path_within_sandbox(path: str, root: str | None = None) -> bool:
    """Check if a path is within the sandbox without raising exceptions."""
    try:
        resolve_under_root(path, root=root)
        return True
    except (PathTraversalError, ValueError):
        return False


def virtual_path_from_physical(
    path: str | PathLike[str], root: str | None = None
) -> str:
    return actor_paths.virtual_path_from_physical(path, root=root)
