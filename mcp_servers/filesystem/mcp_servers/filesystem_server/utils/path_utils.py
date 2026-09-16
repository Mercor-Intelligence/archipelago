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


def _dealias_root_prefix(path: str, root: str | None = None) -> str:
    """Rewrite a host-absolute path that ALIASES the root into its virtual form.

    ``resolve_virtual_path`` already strips the root prefix, but only matches the
    root's own string and its ``.resolve()``. Delivered worlds expose the same
    directory under a second name — ``/app/files`` is a symlink to ``/filesystem``
    (the ``APP_FS_ROOT``) — and the task prompt names that alias, so an agent that
    follows the prompt sends ``/app/files/report.docx``. That shares no textual
    prefix with the root, so it was re-rooted to
    ``/filesystem/app/files/report.docx`` and reported as ``[not found]`` — the
    one path the prompt teaches was the one that could not be used.

    Matching on ``realpath`` instead of on the string catches every alias
    (symlink, ``..`` chain, or trailing slash) without enumerating them.

    Containment is unchanged: the rewrite happens ONLY when the resolved path
    lands inside the resolved root, and the result is a root-relative virtual
    path that ``resolve_virtual_path`` re-validates. A path resolving anywhere
    else is returned untouched and is re-rooted (and so rejected) exactly as
    before — notably ``/app/tools/...``, which holds the seeded apps' admin
    credentials and must stay unreachable. This deliberately does NOT fall back
    to "use the caller's path as-is when the sandboxed one is missing": that
    would turn the sandbox into a host passthrough.
    """
    if not path or not os.path.isabs(path):
        return path
    root_path = Path(root or actor_paths.active_filesystem_root()).absolute()

    # Today's interpretation wins whenever it resolves to something real, so a
    # world that genuinely contains an ``app/files/`` subtree is unaffected and
    # the common case costs one ``stat`` and no ``realpath``. Only a miss is
    # worth re-interpreting.
    naive = os.path.normpath(os.path.join(str(root_path), path.lstrip("/")))
    if os.path.exists(naive):
        return path

    # realpath() resolves the existing prefix of a not-yet-created path, so a
    # write target under an aliased directory de-aliases too.
    try:
        relative = Path(os.path.realpath(path)).relative_to(root_path.resolve())
    except ValueError:
        return path
    return "/" + str(relative) if str(relative) != "." else "/"


def resolve_under_root(
    path: str,
    *,
    root: str | None = None,
    check_exists: bool = False,
    must_be_file: bool = False,
    must_be_dir: bool = False,
) -> str:
    return actor_paths.resolve_virtual_path(
        _dealias_root_prefix(path, root),
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
    if physical_paths_enabled():
        root_path = Path(root or actor_paths.active_filesystem_root()).absolute()
        resolved_path = Path(path).resolve()
        try:
            _ = resolved_path.relative_to(root_path.resolve())
        except ValueError:
            return actor_paths.OUTSIDE_ACTOR_ROOT
        return str(resolved_path)
    return actor_paths.virtual_path_from_physical(path, root=root)
