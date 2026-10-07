import os
from os import PathLike
from pathlib import Path

from mcp_actor import paths as actor_paths

PathTraversalError = actor_paths.ActorPathError

PHYSICAL_PATHS_ENV_VAR = "EXPOSE_PHYSICAL_PATHS"
_TRUTHY = {"1", "true", "yes", "on"}


def physical_paths_enabled() -> bool:
    """Whether tool outputs should report physical shared-mount paths.

    Opt-in via the EXPOSE_PHYSICAL_PATHS runtime env var, and only for the
    target agent / coordinator, whose root is the shared /filesystem mount —
    the same directory the code-execution app uses as its working directory.
    VCA actors always keep virtualized paths so their per-actor roots under
    the coordinator state directory never leak.
    """
    if os.getenv(PHYSICAL_PATHS_ENV_VAR, "false").strip().lower() not in _TRUTHY:
        return False
    return actor_paths.get_current_actor_id() in {
        actor_paths.TARGET_AGENT_ACTOR_ID,
        actor_paths.COORDINATOR_ACTOR_ID,
    }


def resolve_under_root(
    path: str,
    *,
    root: str | None = None,
    check_exists: bool = False,
    must_be_file: bool = False,
    must_be_dir: bool = False,
) -> str:
    """Resolve an agent-supplied path inside the active actor root.

    Uses ``mcp_actor``'s default "physical" path mode: a leading mount spelling
    (``/filesystem/x``) and a plain '/'-rooted path (``/x``) both name
    ``<root>/x``, and host-absolute aliases of the root (``/app/files`` ->
    ``/filesystem``) are de-aliased by ``realpath`` inside the library.
    Containment (traversal and symlink escapes) is enforced there too.

    This server used to de-alias here first. That pre-pass returned a
    '/'-prefixed root-relative path, which the library then stripped a second
    time, so ``/filesystem/filesystem/x`` (a seeded top-level ``filesystem/``
    folder) resolved to ``<root>/x`` (ENV-494). The library's own pass
    returns a relative path and does not have that problem.
    """
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


def display_path(path: str | PathLike[str], root: str | None = None) -> str:
    """Render an in-root physical path for tool output, honoring the path mode.

    Unlike :func:`virtual_path_from_physical` this is purely lexical and never
    follows symlinks, so a symlink keeps its own path instead of collapsing
    onto its target — callers that need the target report it separately.

    Only ever called with a path already resolved into the active actor root by
    :func:`resolve_under_root` and checked by :func:`is_path_within_sandbox`.
    Anything that still lands outside the root is reported as the
    ``OUTSIDE_ACTOR_ROOT`` sentinel rather than echoed back.
    """
    root_path = Path(root or actor_paths.active_filesystem_root()).absolute()
    abs_path = Path(os.path.normpath(path))
    try:
        relative = abs_path.relative_to(root_path)
    except ValueError:
        return actor_paths.OUTSIDE_ACTOR_ROOT
    if physical_paths_enabled():
        return str(abs_path)
    if str(relative) == ".":
        return "/"
    return mount_safe_virtual_path("/" + relative.as_posix(), root=root)


def virtual_path_from_physical(
    path: str | PathLike[str], root: str | None = None
) -> str:
    if physical_paths_enabled():
        root_path = Path(root or actor_paths.active_filesystem_root()).absolute()
        resolved_path = Path(path).resolve()
        try:
            _ = resolved_path.relative_to(root_path.resolve())
        except ValueError:
            return actor_paths.OUTSIDE_ACTOR_ROOT
        return str(resolved_path)
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
    mount (ENV-494: ``/filesystem/filesystem/07_RandD/...``), and its virtual
    path ``/filesystem/07_RandD/...`` would then be read back as
    ``<root>/07_RandD/...``, a file that does not exist. For exactly those
    paths the mount prefix is kept, which is also the real path the
    code-execution app sees. Every other path is returned unchanged.

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
