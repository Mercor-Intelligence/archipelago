"""Shared lifecycle-script entry points (``populate.sh`` / ``snapshot.sh``).

Every Foundry-* MCP server ships two near-identical shell-callable Python
scripts: ``scripts/populate_engine.py`` (driven by Modal's populate hook,
typically ~300 LOC) and ``scripts/snapshot_engine.py`` (driven by the
snapshot hook, ~150-200 LOC). Both perform the same ~95% boilerplate:
argparse, ``sys.path`` bootstrap, ``$STATE_LOCATION`` → fallback-anchor
bridge, :func:`resolve_canonical_db_path` (or its hand-rolled equivalent),
:func:`bind_engine` + dispose,
:func:`~mcp_middleware.csv_engine.snapshot_with_populate` call, plus a
``--validate-only`` branch and a ``:memory:`` short-circuit.

The recurring bug class this fixes: every shared-lib improvement to the
facade triggers a per-app wrapper drift cycle. PR #142's DbGateMiddleware
needed a populate.sh wrap on every consumer; PR #141's boot-race fix
needed step 0 awareness in every consumer's snapshot.sh. We've shipped
two cursorbot findings (cold-world skip and the ``:memory:`` corruption
fixed in PR #145) that the equivalent Zoho / Atlassian / MS-Teams /
Workspace wrappers also have, just nobody checked. Centralising the
wrapper here means every shared-lib fix lands once instead of N times.

After adopting this module, a per-app ``populate_engine.py`` collapses to::

    # mcp_servers/studio_server/scripts/populate_engine.py
    from pathlib import Path
    from mcp_middleware.csv_engine import SnapshotConfig
    from mcp_middleware.lifecycle import populate_main
    from mcp_middleware.runtime_db import EngineBinding

    def import_hook_factory(binding: EngineBinding, config: SnapshotConfig):
        # Closure over binding.engine + the already-loaded snapshot
        # config — must return a zero-arg callable that returns an int
        # exit code (matches snapshot_with_populate's ``import_hook=``
        # contract). The wrapper loads config once for the facade and
        # passes the same instance in so factories don't re-parse the
        # YAML.
        from db.session import init_db  # imported AFTER bind to honour ordering
        from app.populate import run_populate

        def _hook() -> int:
            init_db(binding.engine)
            return run_populate(binding.engine, config)

        return _hook

    if __name__ == "__main__":
        raise SystemExit(populate_main(
            config_path=Path(__file__).parents[1] / "snapshot_config.yaml",
            repo_root=Path(__file__).parents[3],
            import_hook_factory=import_hook_factory,
        ))

Same shape for ``snapshot_engine.py`` calling :func:`snapshot_main`.

API surface
-----------

* :func:`populate_main` — populate.sh entry point. Wraps the facade with
  an ``import_hook_factory`` so the app's CSV/JSON ingest runs against
  the freshly-bound engine.
* :func:`snapshot_main` — snapshot.sh entry point. Wraps the facade
  without an import hook (typical case — populate already ran), but
  accepts ``import_hook_factory=`` for the apps that want snapshot.sh
  to be self-contained (idempotent: if populate already ran, the facade
  auto-skips the import via the harvested-DB detection).

Both functions accept the same kwargs apart from intent — the
underlying ``_lifecycle_main`` does all the actual work. Two names
exist because the existing convention has two .sh entry points; apps
that want the brand-new "one .sh" convention can call either.

``STATE_LOCATION`` convention
-----------------------------

The wrapper consults ``$STATE_LOCATION`` as the canonical directory:

1. ``$STATE_LOCATION`` set → that path is the fallback anchor for
   :func:`resolve_canonical_db_path` AND the ``state_dir=`` argument to
   :func:`snapshot_with_populate`.
2. ``$STATE_LOCATION`` unset → ``default_state_dir`` (caller-provided,
   defaults to ``repo_root``).
3. The positional CLI arg ``state_dir`` (if passed) overrides both.

The wrapper creates the resolved state_dir if it doesn't exist
(``mkdir -p``). Cold-world boot — no state — is a no-op rather than an
error.

``MemoryMode`` handling
-----------------------

If :func:`resolve_canonical_db_path` returns
:class:`~mcp_middleware.runtime_db.MemoryMode`, the wrapper logs and
returns 0 without binding an engine. In-memory DBs have no lifecycle
work to do — the populate hook would have nothing to read; the snapshot
hook has nothing to ship.

This is in addition to the facade's own ``:memory:`` short-circuit
(:func:`snapshot_with_populate` detects the sentinel at its top). Two
layers of defence are intentional: the wrapper short-circuits before
binding an engine (cheaper), and the facade short-circuits before
filesystem ops (catches direct callers that skip the wrapper).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from loguru import logger

from .csv_engine import SnapshotConfig, SnapshotHookResult, load_config, snapshot_with_populate
from .runtime_db import (
    CanonicalPath,
    MemoryMode,
    bind_engine,
    log_binding,
    resolve_canonical_db_path,
)

if TYPE_CHECKING:
    from .runtime_db import EngineBinding

__all__ = [
    "populate_main",
    "snapshot_main",
]


# Type alias for the import-hook factory contract. Caller-provided
# closure that takes the resolved :class:`EngineBinding` and the
# already-loaded :class:`SnapshotConfig`, and returns a zero-arg
# callable matching :func:`snapshot_with_populate`'s ``import_hook=``
# parameter (returns ``int`` exit code). Passing the config in (rather
# than making each factory re-parse the YAML) avoids duplicating the
# ``load_config`` call the wrapper already made for the facade.
ImportHookFactory = Callable[["EngineBinding", SnapshotConfig], Callable[[], int]]

# Build-index hook signature — takes the engine, no return.
BuildIndexHook = Callable[["EngineBinding"], None]


# Phase string for logging only — distinguishes populate.sh from snapshot.sh
# in operator-facing log lines. Doesn't affect behaviour; both phases run
# the same lifecycle facade. Two phase names exist because the existing
# Foundry-* convention has two .sh entry points.
_PHASE_POPULATE = "populate"
_PHASE_SNAPSHOT = "snapshot"


def populate_main(
    *,
    config_path: Path,
    repo_root: Path,
    server_module_dir: Path | None = None,
    import_hook_factory: ImportHookFactory | None = None,
    build_index_hook: BuildIndexHook | None = None,
    drop_tables: list[str] | None = None,
    default_state_dir: Path | None = None,
    default_db_filename: str = "studio.db",
    db_env_var: str = "DATABASE_PATH",
    argv: list[str] | None = None,
) -> int:
    """Shared ``populate.sh`` entry point.

    Equivalent of per-app ``scripts/populate_engine.py``: runs the
    :func:`~mcp_middleware.csv_engine.snapshot_with_populate` facade
    with the app's ``import_hook`` wired in, so a Modal populate
    lifecycle invocation imports CSV/JSON sources into the runtime DB,
    builds whatever indexes the app needs, and (because the facade is
    end-to-end) writes a clean canonical back to ``state_dir`` ready
    for the snapshot hook to capture.

    Args:
        config_path: Absolute path to the app's ``snapshot_config.yaml``.
            Loaded once via :func:`~mcp_middleware.csv_engine.load_config`.
        repo_root: Absolute path to the repo's root. Used to bootstrap
            ``sys.path`` (so the app's modules import cleanly) and as
            the fallback for ``default_state_dir`` when neither
            ``$STATE_LOCATION`` nor a positional CLI arg is supplied.
        server_module_dir: Absolute path to the per-server directory
            (typically ``repo_root/mcp_servers/<server>/``). ``None``
            triggers auto-detection: if ``repo_root/mcp_servers/`` has
            exactly one subdirectory, that's used; otherwise
            :class:`ValueError` to force the caller to be explicit. Use
            the kwarg directly for multi-server repos or non-conforming
            layouts.
        import_hook_factory: Closure that builds the actual import hook
            once the engine is bound. Called as
            ``import_hook_factory(binding, config) -> Callable[[], int]``
            with the same :class:`SnapshotConfig` the wrapper already
            loaded from ``config_path`` (so factories don't re-parse the
            YAML). The returned callable matches
            :func:`snapshot_with_populate`'s ``import_hook=`` contract.
            ``None`` skips the import step (the facade will then either
            auto-skip via harvested-DB detection or just no-op step 2).
        build_index_hook: Optional app-specific FTS5/vec0/derived-index
            builder. Called as ``build_index_hook(binding) -> None`` —
            wrapped into a zero-arg closure for the facade.
        drop_tables: Forwarded to ``snapshot_with_populate``. Tables
            dropped from the clean canonical (e.g. ``docvec_*``).
        default_state_dir: Fallback for the state_dir when
            ``$STATE_LOCATION`` is unset AND no CLI override is passed.
            Defaults to ``repo_root``.
        default_db_filename: Filename used when ``$db_env_var`` is unset.
            Defaults to ``"studio.db"``. Per-app callers override (e.g.
            ``"atlassian.db"``, ``"zoho.db"``).
        db_env_var: Env var consulted by
            :func:`resolve_canonical_db_path`. Defaults to
            ``"DATABASE_PATH"``.
        argv: Argument list. ``None`` → ``sys.argv[1:]``. Recognised
            args: positional ``state_dir`` (overrides ``$STATE_LOCATION``
            and ``default_state_dir``), ``--validate-only`` (run setup
            checks and exit 0).

    Returns:
        Shell exit code: ``0`` on success, non-zero on failure.

    Behaviour summary:

    * ``$STATE_LOCATION`` and CLI state_dir resolution happen before any
      engine binding.
    * ``MemoryMode`` short-circuit returns 0 without binding an engine.
    * ``--validate-only`` returns 0 after argv parsing + state_dir
      resolution + canonical resolution, without binding an engine or
      touching the facade.
    * On non-memory file-mode: bind engine, build hooks via the factory,
      call the facade, dispose engine in a ``finally``, return rc.
    """
    return _lifecycle_main(
        phase=_PHASE_POPULATE,
        config_path=config_path,
        repo_root=repo_root,
        server_module_dir=server_module_dir,
        import_hook_factory=import_hook_factory,
        build_index_hook=build_index_hook,
        drop_tables=drop_tables,
        default_state_dir=default_state_dir,
        default_db_filename=default_db_filename,
        db_env_var=db_env_var,
        argv=argv,
    )


def snapshot_main(
    *,
    config_path: Path,
    repo_root: Path,
    server_module_dir: Path | None = None,
    import_hook_factory: ImportHookFactory | None = None,
    build_index_hook: BuildIndexHook | None = None,
    drop_tables: list[str] | None = None,
    default_state_dir: Path | None = None,
    default_db_filename: str = "studio.db",
    db_env_var: str = "DATABASE_PATH",
    argv: list[str] | None = None,
) -> int:
    """Shared ``snapshot.sh`` entry point.

    Equivalent of per-app ``scripts/snapshot_engine.py``: runs the
    :func:`~mcp_middleware.csv_engine.snapshot_with_populate` facade so
    a Modal snapshot lifecycle invocation captures whatever runtime
    state was left by the populate hook (or by an SME-uploaded
    pre-built DB).

    By default no ``import_hook_factory`` is passed — the typical
    snapshot.sh just packages the runtime state populate.sh already
    built. Apps that want snapshot.sh to be self-contained (i.e. run
    populate inline if it didn't run) can pass
    ``import_hook_factory=`` and the facade will idempotently re-import
    (the facade's harvested-DB auto-skip protects against double-import
    when an SME-shipped DB landed).

    All kwargs are identical to :func:`populate_main`. The phase string
    in log output is the only behavioural difference.
    """
    return _lifecycle_main(
        phase=_PHASE_SNAPSHOT,
        config_path=config_path,
        repo_root=repo_root,
        server_module_dir=server_module_dir,
        import_hook_factory=import_hook_factory,
        build_index_hook=build_index_hook,
        drop_tables=drop_tables,
        default_state_dir=default_state_dir,
        default_db_filename=default_db_filename,
        db_env_var=db_env_var,
        argv=argv,
    )


# ---------------------------------------------------------------------------
# Shared implementation
# ---------------------------------------------------------------------------


def _lifecycle_main(
    *,
    phase: str,
    config_path: Path,
    repo_root: Path,
    server_module_dir: Path | None,
    import_hook_factory: ImportHookFactory | None,
    build_index_hook: BuildIndexHook | None,
    drop_tables: list[str] | None,
    default_state_dir: Path | None,
    default_db_filename: str,
    db_env_var: str,
    argv: list[str] | None,
) -> int:
    """Actual implementation shared by ``populate_main`` / ``snapshot_main``."""
    args = _parse_argv(argv, phase=phase)
    # Resolve the state_dir path but DO NOT mkdir it yet — the MemoryMode
    # short-circuit below returns before any lifecycle work, and creating
    # the directory in that case would be a documented no-op with a real
    # filesystem side effect.
    state_dir = _resolve_state_dir(
        cli_override=args.state_dir,
        default_state_dir=default_state_dir,
        repo_root=repo_root,
    )

    # Resolve canonical AFTER state_dir is known — STATE_LOCATION is the
    # convention for "where the canonical lives by default."
    canonical = resolve_canonical_db_path(
        env_var=db_env_var,
        default_filename=default_db_filename,
        fallback_anchor=state_dir,
    )

    if args.validate_only:
        logger.info(
            "{}_main: --validate-only ok (state_dir={} canonical={!r}) — exiting 0",
            phase,
            state_dir,
            canonical,
        )
        return 0

    # MemoryMode short-circuit — return 0 without binding an engine or
    # creating the state_dir. The facade also short-circuits MemoryMode
    # at its top, but stopping here is cheaper (no sys.path mutation,
    # no config load, no mkdir side effect) and the log line is
    # phase-tagged for clearer operator-facing output.
    if isinstance(canonical, MemoryMode):
        logger.info(
            "{}_main: canonical is MemoryMode — nothing to {}, exiting 0",
            phase,
            phase,
        )
        return 0

    assert isinstance(canonical, CanonicalPath)  # narrows for type checkers

    # Non-memory path from here on: materialise the state_dir so downstream
    # steps (harvest, snapshot) can write into it. Cold-world boot with a
    # missing directory is a no-op rather than an error — same semantics
    # the pre-refactor code had, just deferred past the short-circuit.
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("{}_main: could not create state_dir {}: {}", phase, state_dir, exc)
        return 1

    # Bootstrap sys.path before loading config or binding the engine — the
    # config may reference app-defined readers / transforms / key normalisers
    # via dotted-path strings that need the app's modules on sys.path to
    # import cleanly.
    _bootstrap_sys_path(repo_root=repo_root, server_module_dir=server_module_dir)

    try:
        config = load_config(config_path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        # OSError: file missing / unreadable.
        # yaml.YAMLError: malformed YAML that yaml.safe_load rejects.
        # ValueError: raised by load_config itself when the parsed shape
        # is invalid (bad `sources` entry, non-mapping `import_options`,
        # etc.). All three are caller-visible failures — surface as rc=1
        # rather than an uncaught traceback that bypasses the wrapper's
        # documented exit-code contract.
        logger.error("{}_main: could not load config {}: {}", phase, config_path, exc)
        return 1

    logger.info(
        "{}_main: starting (state_dir={} canonical={} config={})",
        phase,
        state_dir,
        canonical.path,
        config_path,
    )

    binding = bind_engine(canonical.path)
    log_binding(binding)

    try:
        result = _run_facade(
            phase=phase,
            binding=binding,
            state_dir=state_dir,
            canonical=canonical,
            config=config,
            import_hook_factory=import_hook_factory,
            build_index_hook=build_index_hook,
            drop_tables=drop_tables,
        )
    except Exception:
        # snapshot_with_populate raises on import_hook rc != 0 and on
        # facade-internal errors. Log + return 1 rather than letting the
        # exception kill the shell — the script's caller (Modal) only
        # cares about the exit code, and the traceback is already in the
        # log via the exception handler.
        logger.exception("{}_main: lifecycle facade raised", phase)
        return 1
    finally:
        binding.engine.dispose()

    logger.info(
        "{}_main: done (harvested={} import_rc={!r} import_skipped={!r} pruned={} "
        "index_built={} post_harvest_ran={})",
        phase,
        len(result.harvested),
        result.import_rc,
        result.import_skipped_reason,
        len(result.pruned),
        result.index_built,
        result.post_harvest_ran,
    )
    return 0


def _run_facade(
    *,
    phase: str,
    binding: EngineBinding,
    state_dir: Path,
    canonical: CanonicalPath,
    config: SnapshotConfig,
    import_hook_factory: ImportHookFactory | None,
    build_index_hook: BuildIndexHook | None,
    drop_tables: list[str] | None,
) -> SnapshotHookResult:
    """Build the per-call hooks and invoke ``snapshot_with_populate``.

    Factored out so the engine-dispose ``finally`` in ``_lifecycle_main``
    wraps the hook construction too — a factory that raises during hook
    construction (e.g. an import error in the app's populate module) must
    still dispose the engine on the way out.
    """
    del phase  # parameter retained for future phase-aware behaviour

    import_hook: Callable[[], int] | None = None
    if import_hook_factory is not None:
        import_hook = import_hook_factory(binding, config)

    build_index_hook_zero_arg: Callable[[], None] | None = None
    if build_index_hook is not None:
        # Capture binding in a closure so the facade sees the zero-arg
        # signature it expects. Named `_wrapped` (not the outer var)
        # so basedpyright doesn't flag it as a redeclaration; we assign
        # into the outer name on the next line.
        def _wrapped() -> None:
            assert build_index_hook is not None  # narrows for type checkers
            build_index_hook(binding)

        build_index_hook_zero_arg = _wrapped

    return snapshot_with_populate(
        state_dir=state_dir,
        canonical=canonical.path,
        config=config,
        import_hook=import_hook,
        build_index_hook=build_index_hook_zero_arg,
        drop_tables=drop_tables,
        runtime=binding.runtime,
    )


# ---------------------------------------------------------------------------
# Internals: argv, state_dir, sys.path
# ---------------------------------------------------------------------------


def _state_dir_arg(raw: str) -> Path | None:
    """argparse ``type=`` for the positional state_dir.

    Returns ``None`` for empty / whitespace-only input so an unquoted
    ``populate_engine.py "$1"`` invocation with no positional forwards
    an empty string, which parses as "state_dir not provided" — same
    treatment as an empty ``$STATE_LOCATION``. Without this argparse's
    default ``type=Path`` would turn ``""`` into ``Path(".")`` (str
    representation of an empty path), silently overriding both env-var
    and default fallbacks with the process cwd.
    """
    if not raw.strip():
        return None
    return Path(raw)


def _parse_argv(argv: list[str] | None, *, phase: str) -> argparse.Namespace:
    """Parse the two recognised lifecycle args.

    Recognised args (positional ``state_dir``, ``--validate-only``) are
    deliberately minimal — apps that need richer CLI surface should
    parse their own and pass the residue via ``argv=`` after stripping
    the lifecycle args.
    """
    parser = argparse.ArgumentParser(
        prog=f"{phase}_main",
        description=f"Mercor MCP {phase} lifecycle entry point.",
    )
    parser.add_argument(
        "state_dir",
        nargs="?",
        default=None,
        type=_state_dir_arg,
        help=(
            "Override $STATE_LOCATION (defaults to env var; if unset, "
            "falls back to the wrapper's default_state_dir kwarg or repo_root)."
        ),
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help=(
            "Resolve state_dir + canonical and exit 0 without binding an "
            "engine or running the lifecycle facade. Useful for CI checks "
            "that confirm the wrapper is wired correctly."
        ),
    )
    return parser.parse_args(argv)


def _resolve_state_dir(
    *,
    cli_override: Path | None,
    default_state_dir: Path | None,
    repo_root: Path,
) -> Path:
    """Resolve the state_dir from CLI override > $STATE_LOCATION > default > repo_root.

    Returns the resolved absolute path. Does NOT create the directory —
    that's :func:`_lifecycle_main`'s job, deferred past the MemoryMode
    short-circuit so path resolution stays a pure computation without
    filesystem side effects. Note the ``.resolve()`` at the end still
    calls the filesystem to canonicalise symlinks, but that's a read,
    not a mutation.
    """
    if cli_override is not None:
        resolved = cli_override.expanduser()
    else:
        env_value = (os.getenv("STATE_LOCATION") or "").strip()
        if env_value:
            resolved = Path(env_value).expanduser()
        elif default_state_dir is not None:
            resolved = default_state_dir.expanduser()
        else:
            resolved = repo_root.expanduser()

    return resolved.resolve()


def _bootstrap_sys_path(
    *,
    repo_root: Path,
    server_module_dir: Path | None,
) -> None:
    """Insert ``repo_root`` and ``server_module_dir`` at the front of ``sys.path``.

    Auto-detects ``server_module_dir`` from ``repo_root/mcp_servers/`` when
    it's ``None``:

    * Exactly one subdirectory → use that.
    * Zero subdirectories → skip the server-module insertion (only
      ``repo_root`` lands on the path).
    * Two or more subdirectories → :class:`ValueError`. Multi-server
      repos must pass ``server_module_dir=`` explicitly so the wrapper
      doesn't silently pick the wrong one.

    Insertions are no-ops when the absolute path is already on
    ``sys.path``, so calling this multiple times is safe.
    """
    inserts: list[Path] = [repo_root]

    if server_module_dir is None:
        candidate = repo_root / "mcp_servers"
        if candidate.is_dir():
            entries = sorted(p for p in candidate.iterdir() if p.is_dir())
            if len(entries) == 1:
                server_module_dir = entries[0]
            elif len(entries) >= 2:
                raise ValueError(
                    f"_bootstrap_sys_path: server_module_dir auto-detection "
                    f"failed for {candidate} — found {len(entries)} "
                    f"subdirectories ({', '.join(p.name for p in entries)}); "
                    f"pass server_module_dir= explicitly to disambiguate "
                    f"(this is a multi-server repo)."
                )
            # len(entries) == 0 → fall through, no server-module insert

    if server_module_dir is not None:
        inserts.append(server_module_dir)

    for raw in inserts:
        abs_path = str(raw.expanduser().resolve())
        if abs_path not in sys.path:
            sys.path.insert(0, abs_path)
