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

``config_registrar`` seam (warm-world ``import_always``)
--------------------------------------------------------

An app whose derivation logic (row / group / pre-transform / post-import
hooks) lives *inside* its ``import_hook`` has a warm-world gap: when a
pre-built DB is shipped the facade auto-skips the ``import_hook`` and runs
:func:`~mcp_middleware.csv_engine.importer.apply_import_always` to
clear-and-reapply the ``import_always`` entities from the shipped CSVs — but
those hooks were never registered, so the re-import inserts bare rows that
trip NOT NULL / skip derived columns. ``config_registrar`` closes that gap:
pass ``config_registrar=app.register_hooks`` (signature
``(config, state_dir)``) and the facade calls it once on ITS config, BEFORE
the import_hook AND before ``apply_import_always``, so the same derivation
hooks feed both the cold and warm paths. This is the supported replacement
for registering hooks manually inside an ``import_hook_factory`` closure
(which only feeds the cold path).

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

Runtime mode & the DB gate
--------------------------

These entry points run in the **lifecycle process** (Modal's populate /
snapshot hook), which is a *separate process — and often a separate uid —*
from the live MCP server. That split drives two responsibilities that are
deliberately **NOT** owned here:

* **Persisting the live server's runtime.** In runtime mode the server
  serves from a per-uid ``0o700`` ``/tmp`` runtime this process cannot read.
  Before snapshot can harvest a faithful canonical, the server must fold its
  runtime onto the canonical itself. The snapshot phase drives this by
  passing ``server_port`` to the facade, which POSTs ``/_internal/persist``
  (:func:`~mcp_middleware.runtime_db.persist_server_runtime`) — a WAL
  checkpoint + copy the *server* executes. If that route is absent /
  unreachable the facade proceeds with a WARN rather than blocking a deploy
  (see :class:`~mcp_middleware.runtime_db.PersistOutcome`); a reachable
  server whose pool refuses to drain raises. The lifecycle process never
  touches the server's runtime directly — it only asks the server to persist.

* **Opening / closing the HTTP DB gate — auto-managed (opt-out).** The
  503-while-populating gate (:class:`~mcp_middleware.runtime_db.DbGateMiddleware`)
  lives on the *server* process (its ``/_internal/disable_db`` /
  ``/_internal/enable_db`` routes own the flag), but the facade now **drives** it
  by default so adopting the runtime-DB system stays unintrusive — an app
  shouldn't have to hand-roll the close/reopen dance around every wrapper call.

  Correctness never depended on the gate: the persist WAL-fold is atomic (temp +
  rename on the server side) and the step-0.5 drain disposes the server's pool,
  which together already guarantee a corruption-free point-in-time snapshot.
  What the gate *adds* is pausing live app traffic for the window, so a request
  can't read a half-swapped runtime DB and 503s cleanly instead. That is a
  usability win the facade can deliver for free, so it does.

  When gate management engages (``manage_db_gate=True``, the default) the wrapper
  POSTs ``disable_db`` to the live server **before** the facade's persist, then
  POSTs ``enable_db`` in a ``finally`` after the whole facade completes — via
  :func:`~mcp_middleware.runtime_db.set_server_db_gate`. It engages on **exactly
  the persist-in-place path**: the snapshot phase with a live server to persist
  (``server_port`` resolved, no ``import_hook``). The populate phase (cold build,
  no live server) and the self-contained snapshot (``import_hook`` present, no
  live server assumed) never touch a gate. Everything is best-effort: a server
  without the gate routes (404/501), an unreachable server, or a failed close all
  return ``False`` and the facade proceeds *without* a gate — the persist/drain
  still make the snapshot safe. The reopen is only attempted when the close
  actually succeeded (nothing to reopen otherwise).

  **Sticky-closed-on-failure (default).** The reopen fires only on a *clean*
  run. If the run fails (the facade raised, or came back with a non-zero
  ``import_rc``) the gate is left **CLOSED** so the server keeps 503ing as the
  operator's signal to investigate, rather than silently resuming traffic against
  a possibly half-swapped canonical — the philosophy documented on
  :mod:`~mcp_middleware.runtime_db.db_gate`. Pass ``reopen_on_failure=True`` to
  restore always-reopen (availability over the stuck-signal). Pass
  ``manage_db_gate=False`` to opt out of gate management entirely and own it
  yourself.

  **Mid-boot live-server guard (OBI-44).** The close isn't a bare best-effort
  POST — it goes through
  :func:`~mcp_middleware.runtime_db.close_db_gate_or_refuse`, which first scans
  ``/proc`` for a live server for *this* app (cwd under ``live_server_cwd_root``,
  which defaults to ``repo_root``, and cmdline carrying
  ``live_server_cmdline_match``, default ``"main.py"``). A server that is mid-boot
  has its DB pool bound while its HTTP surface isn't listening yet, so a single
  best-effort close would silently no-op and we'd harvest the runtime out from
  under it. When such a process is present the guard **polls** the close until the
  server accepts it and **raises** (aborting the harvest, exit 1) if it never does
  within the poll window. When no matching process is found — the normal
  cold-populate case, or any host without ``/proc`` — it degrades to the same
  single best-effort close as before. Linux-only; inert elsewhere.
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
    MidBootServerError,
    bind_engine,
    close_db_gate_or_refuse,
    disable_db_url_for_port,
    enable_db_url_for_port,
    log_binding,
    resolve_canonical_db_path,
    set_server_db_gate,
)

if TYPE_CHECKING:
    from .runner import DefaultUserRef
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

# Type alias for the config-registrar seam. Caller-provided closure that
# mutates the facade's already-loaded :class:`SnapshotConfig` in place —
# typically to register row / group / pre-transform / post-import hooks
# (the app's derivation logic) onto it. Called as
# ``config_registrar(config, state_dir)`` ONCE by the facade, on ITS config,
# BEFORE the import_hook runs AND before ``apply_import_always``. This lets
# the shared library own applying ``import_always`` entities end-to-end
# (warm world) with the app's derivation hooks in place — the CSV re-import
# runs the same transforms the cold import_hook would, instead of inserting
# bare rows that trip NOT NULL / skip derivation. The ``state_dir`` arg is
# the CSV directory, matching an app's existing
# ``register_hooks(config, csv_dir)`` signature.
ConfigRegistrar = Callable[[SnapshotConfig, Path], None]

# Build-index hook signature — takes the engine, no return.
BuildIndexHook = Callable[["EngineBinding"], None]


# Phase string for logging only — distinguishes populate.sh from snapshot.sh
# in operator-facing log lines. Doesn't affect behaviour; both phases run
# the same lifecycle facade. Two phase names exist because the existing
# Foundry-* convention has two .sh entry points.
_PHASE_POPULATE = "populate"
_PHASE_SNAPSHOT = "snapshot"


def _live_server_port(
    *,
    phase: str,
    has_import_hook: bool,
    persist_port: int | None,
) -> int | None:
    """Resolve the live-server port for the *persist-in-place* path, or ``None``.

    Persist-in-place engages on exactly one path: the snapshot phase with a live
    server to persist and no ``import_hook`` (see :func:`_run_facade` for why the
    two snapshot modes are mutually exclusive). On that path the server listens on
    ``persist_port`` (or ``$MCP_PORT`` / ``5000`` — matching
    :func:`~mcp_middleware.run_server`'s default). Everywhere else — the populate
    phase (cold build, no live server) and the self-contained snapshot
    (``import_hook`` present) — returns ``None``.

    This single resolver is shared by :func:`_run_facade` (which turns a non-None
    port into ``server_port=`` + ``runtime=canonical`` persist-in-place wiring)
    and :func:`_lifecycle_main` (which turns the same non-None port into the
    DB-gate close/reopen locator), so the two can never disagree about whether a
    live server is in play.
    """
    if phase == _PHASE_SNAPSHOT and not has_import_hook:
        return persist_port if persist_port is not None else int(os.environ.get("MCP_PORT", "5000"))
    return None


def populate_main(
    *,
    config_path: Path,
    repo_root: Path,
    server_module_dir: Path | None = None,
    import_hook_factory: ImportHookFactory | None = None,
    config_registrar: ConfigRegistrar | None = None,
    build_index_hook: BuildIndexHook | None = None,
    drop_tables: list[str] | None = None,
    default_state_dir: Path | None = None,
    default_db_filename: str = "studio.db",
    db_env_var: str = "DATABASE_PATH",
    persist_port: int | None = None,
    default_user_table: str | None = None,
    default_user_csv: str | None = None,
    default_user_trust_baked_rows: bool = False,
    default_user_ref: DefaultUserRef | None = None,
    enforce_default_user: bool | None = None,
    manage_db_gate: bool = True,
    reopen_on_failure: bool = False,
    live_server_cwd_root: Path | None = None,
    live_server_cmdline_match: str = "main.py",
    relax_canonical_perms: bool = True,
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
        config_registrar: Optional closure that mutates the facade's loaded
            :class:`SnapshotConfig` in place *before* any lifecycle work —
            called as ``config_registrar(config, state_dir)`` exactly once,
            on the facade's own config, BEFORE the ``import_hook`` runs AND
            before the warm-world ``apply_import_always``. Its purpose is to
            register the app's derivation hooks (row / group / pre-transform /
            post-import) onto the config the shared library owns, so
            ``import_always`` entities re-imported in the warm world (a
            pre-built DB was shipped, so ``import_hook`` is auto-skipped) run
            the *same* transforms the cold import would — instead of inserting
            bare CSV rows that trip NOT NULL / skip derived columns. The
            ``state_dir`` arg is the CSV directory, so an app's existing
            ``register_hooks(config, csv_dir)`` is a drop-in registrar. ``None``
            (default) registers nothing (correct for apps with no
            ``import_always`` entities or no derivation hooks). This is the
            supported replacement for registering hooks manually inside an
            ``import_hook_factory`` closure — the factory only feeds the cold
            path, whereas the registrar feeds both.
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
        persist_port: Port of the *live server* (a separate process/uid)
            serving this DB. Used **only in the snapshot phase**: the facade
            builds ``http://127.0.0.1:{persist_port}/_internal/persist`` and
            drives step-0.4 persist-and-refuse + step-0.5 drain so the server
            folds its cross-uid ``/tmp`` runtime onto the canonical before
            harvest (see :func:`snapshot_with_populate`). ``None`` (default)
            falls back to ``$MCP_PORT`` (or ``5000``), matching
            :func:`~mcp_middleware.run_server`. Ignored in the populate phase
            (a cold build with no live server to drain).
        default_user_table: Forwarded to ``snapshot_with_populate`` — enables
            the default-user identity step (assert + CSV-authoritative apply).
            ``None`` (default) skips it. See that function for the full
            contract.
        default_user_csv: Forwarded to ``snapshot_with_populate`` — filename
            (relative to ``state_dir``) of the authoritative default-user CSV.
        default_user_trust_baked_rows: Forwarded to
            ``snapshot_with_populate`` — governs the no-CSV-this-run case.
        default_user_ref: Forwarded to ``snapshot_with_populate`` — optional
            :class:`~mcp_middleware.DefaultUserRef` FK-resolution check.
        enforce_default_user: Forwarded to ``snapshot_with_populate`` —
            per-call override for the identity assert.
        manage_db_gate: When ``True`` (default), the wrapper auto-manages the
            live server's HTTP DB gate on the persist-in-place path — POST
            ``disable_db`` before the facade's persist and ``enable_db`` in a
            ``finally`` after it (see the module docstring). Best-effort: a server
            without the gate routes / unreachable / a failed close all proceed
            without a gate. No effect in the populate phase (no live server to
            gate) or the self-contained snapshot. Pass ``False`` to own the gate
            yourself.
        reopen_on_failure: Governs the gate's fate when the run FAILS (the facade
            raised or returned a non-zero ``import_rc``). ``False`` (default) is
            *sticky-closed-on-failure*: a botched run leaves the gate CLOSED so the
            server keeps 503ing as the operator's signal to investigate, rather
            than silently resuming traffic against a possibly half-swapped
            canonical (matches the philosophy documented on
            :mod:`~mcp_middleware.runtime_db.db_gate`). ``True`` restores
            always-reopen (the gate is reopened in the ``finally`` regardless of
            outcome) for consumers that prefer availability over the stuck-signal.
            Only relevant when the gate was actually closed (persist-in-place path
            + a successful close); a clean success always reopens either way.
        live_server_cwd_root: Root the mid-boot live-server guard (OBI-44) anchors
            its ``/proc`` scan to. Before closing the gate on the persist-in-place
            path, the facade looks for a process whose ``cwd`` is under this root
            AND whose command line contains ``live_server_cmdline_match``; if one
            is present it POLLS the gate-close until the server accepts it and
            RAISES (aborts the harvest) if it never does — a server whose pool is
            bound but whose HTTP surface isn't listening yet must not be harvested
            out from under. ``None`` (default) uses ``repo_root``. Linux-only; on
            hosts without ``/proc`` the guard is inert and a single best-effort
            close is used. No effect when ``manage_db_gate=False`` or off the
            persist-in-place path.
        live_server_cmdline_match: Command-line token identifying this app's live
            server for the OBI-44 guard. Defaults to ``"main.py"``.
        relax_canonical_perms: When ``True`` (**default**), ``chmod 0o644`` the
            written canonical (``DATABASE_PATH``) after a successful run so a live
            server running under a *different* uid can read it to cold-seed its own
            per-uid runtime. In the runtime-DB two-location pattern the runtime DB
            is deliberately private (per-uid ``0o700`` dir + ``0o600`` file), so the
            canonical is the cross-uid surface that must be readable; a ``0o600``
            populate-written canonical otherwise hangs a differently-uid'd server
            on MCP readiness. On by default so adopters get correct cross-uid
            behaviour without hand-rolling a post-populate ``chmod`` (this is the
            populate-side twin of the persist path's own ``0o644`` relax in
            ``checkpoint.py`` — the two are now consistent). Best-effort: a chmod
            failure logs and proceeds (a canonical owned by another uid must not
            fail an otherwise-successful run). Pass ``False`` only to keep the
            canonical at whatever the process umask produced — e.g. a single-process
            deploy that deliberately wants a tight ``0o600`` canonical and has no
            cross-uid server cold-seeding from it.
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
        config_registrar=config_registrar,
        build_index_hook=build_index_hook,
        drop_tables=drop_tables,
        default_state_dir=default_state_dir,
        default_db_filename=default_db_filename,
        db_env_var=db_env_var,
        persist_port=persist_port,
        default_user_table=default_user_table,
        default_user_csv=default_user_csv,
        default_user_trust_baked_rows=default_user_trust_baked_rows,
        default_user_ref=default_user_ref,
        enforce_default_user=enforce_default_user,
        manage_db_gate=manage_db_gate,
        reopen_on_failure=reopen_on_failure,
        live_server_cwd_root=live_server_cwd_root,
        live_server_cmdline_match=live_server_cmdline_match,
        relax_canonical_perms=relax_canonical_perms,
        argv=argv,
    )


def snapshot_main(
    *,
    config_path: Path,
    repo_root: Path,
    server_module_dir: Path | None = None,
    import_hook_factory: ImportHookFactory | None = None,
    config_registrar: ConfigRegistrar | None = None,
    build_index_hook: BuildIndexHook | None = None,
    drop_tables: list[str] | None = None,
    default_state_dir: Path | None = None,
    default_db_filename: str = "studio.db",
    db_env_var: str = "DATABASE_PATH",
    persist_port: int | None = None,
    default_user_table: str | None = None,
    default_user_csv: str | None = None,
    default_user_trust_baked_rows: bool = False,
    default_user_ref: DefaultUserRef | None = None,
    enforce_default_user: bool | None = None,
    manage_db_gate: bool = True,
    reopen_on_failure: bool = False,
    live_server_cwd_root: Path | None = None,
    live_server_cmdline_match: str = "main.py",
    relax_canonical_perms: bool = True,
    argv: list[str] | None = None,
) -> int:
    """Shared ``snapshot.sh`` entry point.

    Equivalent of per-app ``scripts/snapshot_engine.py``: runs the
    :func:`~mcp_middleware.csv_engine.snapshot_with_populate` facade so
    a Modal snapshot lifecycle invocation captures whatever runtime
    state was left by the populate hook (or by an SME-uploaded
    pre-built DB).

    ``import_hook_factory`` selects between two mutually-exclusive snapshot
    modes:

    * **Omitted (default) — pure snapshot.** The typical snapshot.sh just
      packages the runtime state populate.sh already built. In runtime mode
      the facade drives *persist-in-place*: it asks a live server (on
      ``persist_port`` / ``$MCP_PORT``) to fold its cross-uid runtime onto the
      canonical, then snapshots the canonical in place. This is the path that
      correctly captures a live server's latest writes.

    * **Passed — self-contained snapshot** (run populate inline if it didn't
      run). The facade idempotently re-imports, and its harvested-DB auto-skip
      protects against double-import when an SME-shipped DB (or a prior
      populate) landed. To keep that auto-skip working, this mode does **not**
      persist-in-place (persist-in-place aliases ``runtime=canonical``, which
      protects the canonical from harvest and structurally defeats the
      pre-built-DB detection → the import would re-run and PK-collide). A
      self-contained snapshot therefore assumes no live server has already
      populated the canonical; don't combine it with a live-server deploy that
      needs its runtime persisted.

    All kwargs are identical to :func:`populate_main` (including
    ``manage_db_gate`` / ``reopen_on_failure`` and the ``live_server_*`` mid-boot
    guard, which auto-manage the live server's HTTP DB gate on the pure-snapshot
    persist-in-place path). The phase string in log output is the other
    behavioural difference.

    ``relax_canonical_perms`` (default ``True``) applies here too, for the
    **self-contained** (``import_hook`` present) snapshot mode — which writes the
    canonical via the same ``snapshot_db_only`` path as populate and so has the
    same cross-uid readability gap. The pure/persist-in-place snapshot mode already
    relaxes the canonical to ``0o644`` on the persist side, so the chmod is
    redundant (but harmless) there.

    ``config_registrar`` (see :func:`populate_main`) applies here too and is
    called on the same unconditional pre-facade path. It matters for the
    **self-contained** snapshot mode: when a pre-built DB is present the facade
    auto-skips the ``import_hook`` and runs ``apply_import_always`` in the warm
    world, which needs the app's derivation hooks on the config. The
    pure/persist-in-place snapshot mode does no import, so a registrar there
    simply registers hooks nothing consumes (harmless).
    """
    return _lifecycle_main(
        phase=_PHASE_SNAPSHOT,
        config_path=config_path,
        repo_root=repo_root,
        server_module_dir=server_module_dir,
        import_hook_factory=import_hook_factory,
        config_registrar=config_registrar,
        build_index_hook=build_index_hook,
        drop_tables=drop_tables,
        default_state_dir=default_state_dir,
        default_db_filename=default_db_filename,
        db_env_var=db_env_var,
        persist_port=persist_port,
        default_user_table=default_user_table,
        default_user_csv=default_user_csv,
        default_user_trust_baked_rows=default_user_trust_baked_rows,
        default_user_ref=default_user_ref,
        enforce_default_user=enforce_default_user,
        manage_db_gate=manage_db_gate,
        reopen_on_failure=reopen_on_failure,
        live_server_cwd_root=live_server_cwd_root,
        live_server_cmdline_match=live_server_cmdline_match,
        relax_canonical_perms=relax_canonical_perms,
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
    config_registrar: ConfigRegistrar | None,
    build_index_hook: BuildIndexHook | None,
    drop_tables: list[str] | None,
    default_state_dir: Path | None,
    default_db_filename: str,
    db_env_var: str,
    persist_port: int | None,
    default_user_table: str | None,
    default_user_csv: str | None,
    default_user_trust_baked_rows: bool,
    default_user_ref: DefaultUserRef | None,
    enforce_default_user: bool | None,
    manage_db_gate: bool,
    reopen_on_failure: bool,
    live_server_cwd_root: Path | None,
    live_server_cmdline_match: str,
    relax_canonical_perms: bool,
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

    # Register the app's derivation hooks onto the facade's config BEFORE any
    # lifecycle work — the import_hook (cold) reads them via the same config
    # object, and so does the warm-world apply_import_always inside
    # snapshot_with_populate. Doing it here (once, unconditionally) is what lets
    # the shared library own import_always end-to-end: the CSV re-import runs the
    # app's transforms instead of inserting bare rows. A registrar that raises is
    # a caller bug in the app's registration logic — surface it as rc=1 rather
    # than let a half-registered config reach the facade.
    if config_registrar is not None:
        try:
            config_registrar(config, state_dir)
        except Exception:
            logger.exception(
                "{}_main: config_registrar raised while registering hooks on the "
                "config (state_dir={}); aborting",
                phase,
                state_dir,
            )
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

    # Auto-manage the live server's HTTP DB gate on the persist-in-place path
    # (snapshot phase + live server + no import_hook — the same predicate
    # _run_facade uses to decide persist-in-place, via the shared resolver). We
    # close the gate BEFORE _run_facade so the facade's persist runs while live
    # app traffic 503s cleanly instead of racing the runtime-DB swap; the reopen
    # is in the finally. Best-effort: a server without the gate routes / one
    # that's unreachable / a failed close all leave gate_closed False and proceed
    # WITHOUT a gate — the persist WAL-fold + drain still make the snapshot
    # corruption-free. manage_db_gate=False opts out entirely.
    #
    # The close goes through close_db_gate_or_refuse, which arms the OBI-44
    # mid-boot guard: if a live server for THIS app is coming up on the host (its
    # cwd under live_server_cwd_root — repo_root by default — and its cmdline
    # carrying live_server_cmdline_match), its pool is already bound but its HTTP
    # surface may not be listening yet, so a silent no-op close would let us
    # harvest the runtime out from under it. In that case the guard POLLS the
    # close until the server accepts it and RAISES if it never does — we abort the
    # harvest rather than corrupt the copy it's about to touch.
    gate_port = _live_server_port(
        phase=phase,
        has_import_hook=import_hook_factory is not None,
        persist_port=persist_port,
    )
    gate_closed = False
    if manage_db_gate and gate_port is not None:
        try:
            gate_closed = close_db_gate_or_refuse(
                disable_db_url_for_port(gate_port),
                live_server_cwd_root=(
                    live_server_cwd_root if live_server_cwd_root is not None else repo_root
                ),
                live_server_cmdline_match=live_server_cmdline_match,
            )
        except MidBootServerError:
            logger.exception(
                "{}_main: refusing to harvest — a mid-boot live server is present but never "
                "accepted the DB-gate close on port {}; aborting to avoid harvesting its bound "
                "runtime (OBI-44)",
                phase,
                gate_port,
            )
            binding.engine.dispose()
            return 1
        if gate_closed:
            logger.info(
                "{}_main: closed server DB gate on port {} for the persist window",
                phase,
                gate_port,
            )

    facade_succeeded = False
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
            persist_port=persist_port,
            default_user_table=default_user_table,
            default_user_csv=default_user_csv,
            default_user_trust_baked_rows=default_user_trust_baked_rows,
            default_user_ref=default_user_ref,
            enforce_default_user=enforce_default_user,
        )
        # A clean return isn't automatically success: snapshot_with_populate
        # normally RAISES on a non-zero import_hook rc (caught below), but guard
        # the belt-and-suspenders case where a non-zero import_rc comes back
        # without raising — that's still a failed run for gate purposes.
        facade_succeeded = result.import_rc in (None, 0)
    except Exception:
        # snapshot_with_populate raises on import_hook rc != 0 and on
        # facade-internal errors. Log + return 1 rather than letting the
        # exception kill the shell — the script's caller (Modal) only
        # cares about the exit code, and the traceback is already in the
        # log via the exception handler. facade_succeeded stays False so the
        # gate stays closed (unless reopen_on_failure).
        logger.exception("{}_main: lifecycle facade raised", phase)
        return 1
    finally:
        # Gate fate (only relevant when we actually closed it):
        #   * clean success → reopen (restore traffic).
        #   * failure + reopen_on_failure=False (default) → leave CLOSED. This is
        #     sticky-closed-on-failure: a botched snapshot must keep the server
        #     503ing as the operator's signal, not silently resume serving a
        #     possibly half-swapped canonical (see the db_gate module docstring —
        #     the facade previously violated this by always reopening).
        #   * failure + reopen_on_failure=True → reopen anyway (availability over
        #     the stuck-signal, for consumers that want the old behaviour).
        if gate_closed:
            assert gate_port is not None  # gate_closed implies a resolved port
            if facade_succeeded or reopen_on_failure:
                set_server_db_gate(enable_db_url_for_port(gate_port))
            else:
                logger.warning(
                    "{}_main: run failed — leaving the server DB gate CLOSED on "
                    "port {} (sticky-closed-on-failure). The server will keep "
                    "returning 503 until an operator investigates; pass "
                    "reopen_on_failure=True to always reopen instead.",
                    phase,
                    gate_port,
                )
        binding.engine.dispose()

    # Relax the just-written canonical so a differently-uid'd live server can
    # read it to cold-seed its own per-uid runtime. The runtime DB is deliberately
    # private (per-uid 0o700 dir + 0o600 file — see runtime_db.paths), so the
    # cross-uid surface that actually matters is the CANONICAL, not the runtime.
    # The persist path already relaxes its temp to 0o644 (checkpoint.py); the
    # populate write (snapshot_db_only) relies on umask, so this makes the guarantee
    # explicit and umask-independent. On by default (consolidate cross-uid perms in
    # the shared facade rather than leaving each app to hand-roll a chmod) and
    # best-effort — a chmod failure (e.g. the canonical is owned by another uid)
    # must not fail an otherwise-successful run.
    if relax_canonical_perms:
        _relax_canonical_perms(result.canonical, phase=phase)

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


def _relax_canonical_perms(canonical_path: str | os.PathLike[str], *, phase: str) -> None:
    """Best-effort ``chmod 0o644`` the written canonical for cross-uid cold-seed.

    In the runtime-DB two-location pattern a live MCP server runs under a
    *different* uid than the populate/snapshot process and cold-seeds its own
    per-uid runtime from the canonical (``DATABASE_PATH``). If populate wrote the
    canonical ``0o600`` (tight umask, root-owned), that server can't read it and
    hangs on MCP readiness. Relaxing to ``0o644`` (other-readable, not writable)
    fixes that. Never raises — a differently-owned canonical shouldn't fail a
    successful run; log and proceed.
    """
    try:
        os.chmod(canonical_path, 0o644)  # noqa: S103 - intentional cross-uid readability
        logger.info(
            "{}_main: relaxed canonical {} to 0o644 for cross-uid cold-seed",
            phase,
            canonical_path,
        )
    except OSError as exc:
        logger.warning(
            "{}_main: chmod 0o644 {} failed ({}: {}) — proceeding; a differently-uid'd "
            "server may be unable to cold-seed from it",
            phase,
            canonical_path,
            type(exc).__name__,
            exc,
        )


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
    persist_port: int | None,
    default_user_table: str | None,
    default_user_csv: str | None,
    default_user_trust_baked_rows: bool,
    default_user_ref: DefaultUserRef | None,
    enforce_default_user: bool | None,
) -> SnapshotHookResult:
    """Build the per-call hooks and invoke ``snapshot_with_populate``.

    Factored out so the engine-dispose ``finally`` in ``_lifecycle_main``
    wraps the hook construction too — a factory that raises during hook
    construction (e.g. an import error in the app's populate module) must
    still dispose the engine on the way out.

    Phase-aware runtime / persist wiring
    ------------------------------------
    The two phases feed the facade different ``runtime=`` / ``server_port=``
    arguments because their runtime-mode contracts differ:

    * **populate** — a cold *build* step. No live server is serving this DB,
      so there is nothing to persist. Snapshot back through the freshly-built
      runtime (``binding.runtime``) exactly as the historical build pipeline
      does; passing ``server_port`` here would try to drain a server that
      isn't running.

    * **snapshot (no import_hook — pure snapshot)** — a live server (a
      *separate* process/uid) may be serving the same DB from a cross-uid
      ``/tmp`` runtime. It must fold its runtime onto the canonical *before*
      harvest or its latest writes are lost, so we pass ``server_port`` to
      trigger the facade's step-0.4 persist-and-refuse + step-0.5 drain. We
      also snapshot **in place** (``runtime=canonical``) rather than through
      ``binding.runtime``: this process cold-seeded ``binding.runtime`` from
      the canonical at ``bind_engine`` time — i.e. *before* the server's
      persist — so it is stale. Aliasing ``runtime=canonical`` makes the
      freshly-persisted canonical the source of truth AND disables the step-0
      marker-divergence guard that would otherwise delete the just-persisted
      canonical before harvest.

    * **snapshot WITH import_hook (self-contained snapshot)** — persist-in-place
      is **disabled** and the pre-#163 path is restored (``runtime=binding.runtime``,
      no ``server_port``). Aliasing ``runtime=canonical`` sets
      ``harvest_protected=[canonical]``, so ``pre_built_db_shipped`` can never
      be True and the facade's auto-skip can't fire — the import would re-run
      against an already-populated canonical and PK-collide. Keeping the
      canonical harvestable preserves the documented self-contained contract
      (auto-skip when a pre-built DB / prior populate is present; import
      otherwise). The two snapshot modes are mutually exclusive: a
      self-contained snapshot assumes no live server has already populated the
      canonical, so it does not persist one.
    """
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

    # Persist-in-place engages ONLY in the snapshot phase AND only when there
    # is no import_hook — the two are mutually exclusive:
    #
    #   * No import_hook (pure snapshot, e.g. Atlassian): a live server may be
    #     serving a cross-uid runtime, so drive persist+drain (server_port) and
    #     snapshot the freshly-persisted canonical IN PLACE (runtime=canonical).
    #     Aliasing protects the canonical from harvest and disables the step-0
    #     marker guard so the just-persisted canonical survives — safe here
    #     because there is no import to skip (the step-2 branch is gated on
    #     import_hook is not None, which is False).
    #
    #   * import_hook present (self-contained snapshot — runs populate inline if
    #     it didn't run): the canonical MUST stay harvestable so the pre-built-DB
    #     auto-skip can fire. Aliasing would set harvest_protected=[canonical] →
    #     pre_built_db_shipped is structurally always False → the import re-runs
    #     against an already-populated canonical and PK-collides (the mirror of
    #     the direct-mode harvest-protect bug). Restore the pre-persist-in-place
    #     path: snapshot through binding.runtime and don't drive persist. A
    #     self-contained snapshot assumes no live server has already populated
    #     the canonical, so persist-in-place doesn't apply.
    server_port = _live_server_port(
        phase=phase,
        has_import_hook=import_hook is not None,
        persist_port=persist_port,
    )
    if server_port is not None:
        runtime: str | Path = canonical.path
    else:
        runtime = binding.runtime

    # Phase-aware stripping — populate delivers a COMPLETE DB, snapshot strips.
    #
    #   * **populate** is a *build/delivery* step: the ``.db`` it writes IS the
    #     artifact that gets uploaded and later harvested as a pre-built DB. It
    #     MUST be complete — retaining BOTH its FTS5 / vec0 virtual tables
    #     (``drop_virtual_tables=False``) AND any app-listed derived/content
    #     tables (``drop_tables`` → ``None``, e.g. vec0 shadow tables like
    #     ``docvec_*``). Stripping either would ship an incomplete DB that the
    #     step-3 probe can't heal: a surviving virtual table makes step 3 skip
    #     the rebuild, so the dropped derived tables would never come back —
    #     delivering a broken index. Populate therefore drops NOTHING.
    #
    #   * **snapshot** is an end-of-task *persist* step: the live runtime in
    #     ``/tmp`` keeps the full schema for the running server; the persisted
    #     canonical only needs the base tables (smaller, faster to move) and is
    #     rebuilt at boot — or by a later populate that consumes it (the psp
    #     pipeline), where step 3 builds the stripped index back. So snapshot
    #     strips both virtual tables (``drop_virtual_tables=True``) and the app's
    #     ``drop_tables``.
    if phase == _PHASE_SNAPSHOT:
        effective_drop_tables = drop_tables
        drop_virtual_tables = True
    else:
        effective_drop_tables = None
        drop_virtual_tables = False

    return snapshot_with_populate(
        state_dir=state_dir,
        canonical=canonical.path,
        config=config,
        import_hook=import_hook,
        build_index_hook=build_index_hook_zero_arg,
        drop_tables=effective_drop_tables,
        drop_virtual_tables=drop_virtual_tables,
        runtime=runtime,
        server_port=server_port,
        default_user_table=default_user_table,
        default_user_csv=default_user_csv,
        default_user_trust_baked_rows=default_user_trust_baked_rows,
        default_user_ref=default_user_ref,
        enforce_default_user=enforce_default_user,
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
