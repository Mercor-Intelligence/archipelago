import os
from os import PathLike
from pathlib import Path

from mcp_actor import paths as actor_paths

PathTraversalError = actor_paths.ActorPathError

PHYSICAL_PATHS_ENV_VAR = "EXPOSE_PHYSICAL_PATHS"
_TRUTHY = {"1", "true", "yes", "on"}


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
    return actor_paths.virtual_path_from_physical(path, root=root)
