import os


def get_fs_root() -> str:
    return os.environ.get("APP_FS_ROOT", "/filesystem")


class PathTraversalError(ValueError):
    pass


def resolve_under_root(path: str, *, root: str | None = None) -> str:
    """Safely resolve a sandbox-relative path, guarding against traversal attacks."""
    if root is None:
        root = get_fs_root()

    root = os.path.realpath(root)
    path = path.lstrip("/")
    full_path = os.path.normpath(os.path.join(root, path))
    resolved = os.path.realpath(full_path)

    if not resolved.startswith(root + os.sep) and resolved != root:
        raise PathTraversalError(f"Path resolves outside the sandbox: {path!r}")

    return resolved


def to_sandbox_path(absolute_path: str, root: str | None = None) -> str:
    """Convert an absolute (resolved) path back to a sandbox-relative path like /foo/bar."""
    if root is None:
        root = get_fs_root()
    real_root = os.path.realpath(root)
    if absolute_path == real_root:
        return "/"
    if absolute_path.startswith(real_root + os.sep):
        return absolute_path[len(real_root):]
    return absolute_path
