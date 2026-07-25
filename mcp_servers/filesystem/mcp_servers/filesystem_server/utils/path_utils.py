import os
from os import PathLike

from mcp_actor import paths as actor_paths

PathTraversalError = actor_paths.ActorPathError


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


def is_path_within_sandbox(path: str | PathLike[str], root: str | None = None) -> bool:
    path_str = str(path)
    try:
        if os.path.isabs(path_str):
            return actor_paths.is_path_within_active_root(path_str, root=root)
        resolve_under_root(path_str, root=root)
    except Exception:
        return False
    return True


def validate_real_path(path: str | PathLike[str], root: str | None = None) -> str:
    real_path = os.path.realpath(path)
    if not is_path_within_sandbox(real_path, root=root):
        raise ValueError("Access denied: path resolves outside sandbox")
    return real_path


def virtual_path_from_physical(
    path: str | PathLike[str], root: str | None = None
) -> str:
    return actor_paths.virtual_path_from_physical(path, root=root)
