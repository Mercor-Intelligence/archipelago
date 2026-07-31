"""WAL checkpoint primitive + shared ``/_internal/*`` routes.

The shape of the checkpoint route, in plain English: drain the
SQLAlchemy pool, then fold the SQLite WAL into the main DB file via
``PRAGMA wal_checkpoint(TRUNCATE)``.

* **Drain first** (``engine.dispose()``). In WAL mode each pooled fd
  holds a memory map into the ``-shm`` sidecar; closing them releases
  the maps and lets SQLite coordinate the checkpoint cleanly.
* **Checkpoint on a fresh raw connection** (``engine.raw_connection()``).
  ``engine.connect()`` enters autobegin mode — an implicit transaction
  blocks SQLite from checkpointing on the same connection — so we
  bypass the SQLAlchemy session and run the PRAGMA on a vanilla DBAPI
  cursor.

The endpoint exists so out-of-process workers (populate, backup,
snapshot) can ask the live server to checkpoint *its own engine* before
they touch the runtime DB. A worker that checkpoints from its own
connection misses the frames pinned by the server's pool — the copy
that follows would ship stale data.

For test wiring or in-process call sites the underlying primitive is
:func:`run_wal_checkpoint`; the HTTP route is just a thin wrapper around
it that returns the result as JSON.

Companion routes mounted by :func:`register_runtime_db_routes`:

* ``GET /_internal/db-path`` — emits the resolved engine binding
  (mode, canonical, runtime, sidecar paths) as JSON. Out-of-process
  workers and operators use it to discover where the live server's
  bytes actually live before they touch anything on disk.
* ``POST /_internal/disable_db`` (``disable_db_path``) — sets the
  process-global DB gate (see :mod:`.db_gate`) and disposes the
  engine's connection pool. Lifecycle scripts (populate, snapshot,
  restore) call this **first** so subsequent app requests 503 with
  ``Retry-After`` instead of touching the runtime DB while it's being
  rewritten under them.
* ``POST /_internal/enable_db`` (``enable_db_path``) — clears the DB
  gate. The lifecycle script's ``finally`` / ``trap`` block calls this
  on clean exit. A crashed script leaves the gate closed on purpose;
  see :mod:`.db_gate` for the rationale (sticky-closed-on-failure is
  correct semantics, not a bug).

The split between ``/_internal/checkpoint`` and the new
disable/enable pair is deliberate. ``/_internal/checkpoint`` remains
the explicit operator-drain endpoint: it flushes the WAL and drains
the pool *without* toggling the gate, so it's safe to use for ad-hoc
maintenance or one-shot pre-snapshot drains where you don't want to
take traffic offline. The gate-toggle endpoints are the
lifecycle-script-driven path that actually blocks request flow.
"""

from __future__ import annotations

import logging
import os
import tempfile
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from .binding import BindingMode, EngineBinding
from .populate_route import DEFAULT_MISE_TASK, handle_populate_request

if TYPE_CHECKING:
    from sqlalchemy import Engine

logger = logging.getLogger(__name__)

__all__ = [
    "CheckpointResult",
    "DbPathInfo",
    "PersistResult",
    "persist_runtime_to_canonical",
    "register_runtime_db_routes",
    "run_wal_checkpoint",
]


class CheckpointResult(TypedDict):
    """Shape of the ``/_internal/checkpoint`` response body.

    Keys:
        busy: 1 if a reader still pins the WAL (checkpoint was partial /
            blocked), 0 if the WAL was fully folded.
        frames_in_wal: How many frames were sitting in the WAL when the
            PRAGMA ran. ``frames_in_wal == frames_checkpointed`` together
            with ``busy == 0`` means the WAL is completely empty.
        frames_checkpointed: How many of those frames the PRAGMA managed
            to fold back into the main DB file before returning.
        error: Optional — present iff the PRAGMA itself raised
            (defensive; almost always absent).
    """

    busy: int
    frames_in_wal: int
    frames_checkpointed: int


def run_wal_checkpoint(engine: Engine) -> CheckpointResult:
    """Drain the pool + checkpoint ``engine``'s SQLite WAL into the main DB.

    Returns a :class:`CheckpointResult` regardless of success — a PRAGMA
    failure surfaces as ``busy=1`` + an ``error`` key so the caller can
    log and bail without an exception.

    The function is engine-agnostic on its surface but only meaningful
    on a SQLite engine: ``PRAGMA wal_checkpoint`` is a SQLite-ism. On
    non-SQLite backends the PRAGMA either no-ops or errors depending on
    the driver; either way the result is reported faithfully.
    """
    busy = 1
    frames_in_wal = 0
    frames_checkpointed = 0
    try:
        # Step 1: close every idle connection in the SQLAlchemy pool so
        # their -shm memory maps are released before we ask SQLite to
        # checkpoint. The pool repopulates lazily on the next request.
        # engine.dispose() is sync and fast; awaiting is unnecessary.
        engine.dispose()

        # Step 2: run the checkpoint on a fresh raw connection so the
        # PRAGMA runs outside any SQLAlchemy-managed transaction.
        raw_conn = engine.raw_connection()
        try:
            row = raw_conn.cursor().execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            raw_conn.close()

        if row is not None:
            busy, frames_in_wal, frames_checkpointed = (
                int(row[0]),
                int(row[1]),
                int(row[2]),
            )
            logger.info(
                "checkpoint: busy=%s frames_in_wal=%s frames_checkpointed=%s",
                busy,
                frames_in_wal,
                frames_checkpointed,
            )
    except Exception as exc:  # noqa: BLE001 - defensive (PRAGMA failure must not raise)
        logger.warning("checkpoint: PRAGMA wal_checkpoint failed: %s", exc)
        # mypy/pyright: TypedDict total=True means we can't conditionally
        # add an "error" key without widening the type. Use Any cast.
        result: Any = {
            "busy": 1,
            "frames_in_wal": 0,
            "frames_checkpointed": 0,
            "error": str(exc),
        }
        return result
    finally:
        # Step 3: ALWAYS return with an EMPTY pool — success or failure.
        # ``raw_conn.close()`` above only returns the checkpoint connection
        # to the pool; its SQLite fd stays open, pinned to the *current*
        # inode. The whole point of this endpoint is to let the caller
        # (populate) safely MOVE / REPLACE the runtime DB file immediately
        # afterwards. If we left that pooled fd alive it would keep pointing
        # at the old inode after the replace, and the next request served by
        # that pooled connection would read the STALE database (e.g. an
        # empty cold-seed DB → every row lookup returns null) while freshly
        # opened connections see the new data. Disposing here guarantees the
        # first post-harvest connection opens the path fresh. It runs in a
        # ``finally`` (not the ``try`` body) so the drain contract holds even
        # when the PRAGMA raised. ``dispose()`` itself must never mask the
        # original error, so we swallow its (vanishingly rare) failures.
        try:
            engine.dispose()
        except Exception as exc:  # noqa: BLE001 - dispose failure must not raise
            logger.warning("checkpoint: post-checkpoint engine.dispose() failed: %s", exc)

    return {
        "busy": busy,
        "frames_in_wal": frames_in_wal,
        "frames_checkpointed": frames_checkpointed,
    }


class PersistResult(TypedDict):
    """Shape of the ``POST /_internal/persist`` response body.

    Keys:
        mode: The binding's :class:`~mcp_middleware.runtime_db.BindingMode`
            value (``"runtime"`` / ``"direct"`` / ``"memory"``).
        busy: 1 if the runtime WAL couldn't be fully folded (a live writer
            still pins it) so NO copy was made; 0 on success.
        bytes_copied: Size of the canonical after the fold, in bytes. 0 for
            the no-op modes (direct / memory) and for a busy refusal.
        canonical: The canonical path written, or ``null`` when there is none
            (memory mode).
        runtime: The runtime path folded from, or ``null`` (memory mode; equals
            canonical in direct mode).
        error: Optional — present iff the fold was refused (busy) or raised.
    """

    mode: str
    busy: int
    bytes_copied: int
    canonical: str | None
    runtime: str | None


def persist_runtime_to_canonical(binding: EngineBinding) -> PersistResult:
    """Fold the live server's runtime DB back onto its canonical, in-process.

    The reverse of
    :func:`~mcp_middleware.runtime_db.refresh_runtime_from_canonical` (which
    copies canonical→runtime for an out-of-process worker): this copies
    runtime→canonical so an out-of-process, *different-uid* worker can then read
    the canonical.

    It exists because the runtime DB lives under a per-uid ``0o700`` dir (see
    :func:`~mcp_middleware.runtime_db.runtime_paths_for`) that only the server's
    own uid can read — so a snapshot / fold hook running as a different uid
    physically cannot harvest the server's tool-call mutations from the runtime.
    The persistence therefore MUST happen in the server process; this is the
    primitive the ``POST /_internal/persist`` route wraps.

    Behaviour by mode:

    * **RUNTIME.** Drain the pool + ``PRAGMA wal_checkpoint(TRUNCATE)`` on the
      runtime engine (via :func:`run_wal_checkpoint`). If the WAL never clears
      (``busy != 0``) REFUSE to copy — a fold under a live writer could ship a
      torn snapshot — and report ``busy=1`` + ``error``. Otherwise WAL-aware
      copy runtime → a temp file in the canonical's directory, relax it to
      ``0o644``, then ``os.replace`` it over the canonical **atomically** so a
      mid-copy failure (ENOSPC, I/O error) leaves the original canonical intact
      instead of a half-overwritten corrupt DB. The atomic rename also needs
      only directory-write (not file-write), easing the cross-uid ownership
      case. A faithful byte copy only — no table stripping / index rebuild (the
      app's snapshot step still owns pruning + index policy).
    * **DIRECT.** runtime IS canonical; a checkpoint folds any WAL into it in
      place. No copy. ``bytes_copied=0``. A busy checkpoint is **not** an
      error here — the unfolded frames are still durable in the ``-wal`` sidecar
      (it's part of the DB), so DIRECT always reports ``busy=0`` (success) and
      just logs the partial fold.
    * **MEMORY.** Nothing on disk. No-op success.

    Idempotent and safe to call with no live writers.
    """
    mode = binding.mode
    if mode is BindingMode.MEMORY:
        return PersistResult(mode=mode.value, busy=0, bytes_copied=0, canonical=None, runtime=None)

    if mode is BindingMode.DIRECT or binding.is_aliased:
        # runtime == canonical: fold the WAL into it in place, no copy needed.
        # A busy checkpoint is NOT a failure here — nothing is copied, and the
        # unfolded frames remain durable in the ``-wal`` sidecar (WAL is part of
        # the DB). Reporting busy=1 would make the snapshot facade refuse a
        # snapshot that has zero data at risk, so we always report success and
        # just log the partial fold.
        ckpt = run_wal_checkpoint(binding.engine)
        ckpt_busy = int(ckpt.get("busy", 0))
        if ckpt_busy:
            logger.info(
                "persist: direct-mode WAL not fully folded (busy=%s) — the "
                "unfolded frames are still durable in the -wal sidecar; "
                "reporting success (no copy at risk)",
                ckpt_busy,
            )
        canonical_str = str(binding.canonical) if binding.canonical else None
        return PersistResult(
            mode=mode.value,
            busy=0,
            bytes_copied=0,
            canonical=canonical_str,
            runtime=canonical_str,
        )

    # RUNTIME mode: fold the WAL, then copy runtime → canonical.
    assert binding.runtime is not None
    assert binding.canonical is not None
    runtime = Path(binding.runtime)
    canonical = Path(binding.canonical)

    ckpt = run_wal_checkpoint(binding.engine)
    if int(ckpt.get("busy", 1)) != 0:
        logger.warning(
            "persist: runtime WAL never cleared (busy=%s) — refusing to copy "
            "%s → %s; a fold under a live writer could ship a torn snapshot",
            ckpt.get("busy"),
            runtime,
            canonical,
        )
        busy_result: Any = {
            "mode": mode.value,
            "busy": 1,
            "bytes_copied": 0,
            "canonical": str(canonical),
            "runtime": str(runtime),
            "error": f"runtime WAL busy={ckpt.get('busy')}; refused to persist",
        }
        return busy_result

    try:
        from .sync import copy_db_wal_aware

        # Atomic overwrite: copy into a temp file in the canonical's directory,
        # relax perms on the temp, then os.replace() it over the canonical. A
        # mid-copy failure (ENOSPC, I/O error) then leaves the ORIGINAL canonical
        # untouched instead of a half-written corrupt DB. The final rename is
        # atomic (same directory ⇒ same filesystem) and needs only directory
        # write, not write on the canonical inode — which also softens the
        # cross-uid ownership case. We relax the temp to 0o644 BEFORE the swap so
        # there's never a readable-but-torn window; the canonical must stay
        # readable by the (different-uid) fold process (a 0o600 owner-lock would
        # defeat the whole cross-uid purpose).
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{canonical.name}.", suffix=".persist-tmp", dir=str(canonical.parent)
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            copy_db_wal_aware(runtime, tmp_path)
            try:
                os.chmod(tmp_path, 0o644)
            except OSError as exc:
                logger.warning("persist: chmod 0o644 %s failed: %s", tmp_path, exc)
            os.replace(tmp_path, canonical)
        except BaseException:
            # Any failure (including the copy raising): drop the temp so we don't
            # leak .persist-tmp files, and leave the canonical as it was.
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            raise
        bytes_copied = canonical.stat().st_size
    except Exception as exc:  # noqa: BLE001 - fold failure surfaces as JSON, not a 500 trace
        logger.warning("persist: copy %s → %s failed: %s", runtime, canonical, exc)
        err_result: Any = {
            "mode": mode.value,
            "busy": 1,
            "bytes_copied": 0,
            "canonical": str(canonical),
            "runtime": str(runtime),
            "error": str(exc),
        }
        return err_result

    logger.info(
        "persist: folded runtime %s → canonical %s (%.1f MB)",
        runtime,
        canonical,
        bytes_copied / 1_000_000,
    )
    return PersistResult(
        mode=mode.value,
        busy=0,
        bytes_copied=bytes_copied,
        canonical=str(canonical),
        runtime=str(runtime),
    )


class DbPathInfo(TypedDict):
    """Shape of the ``GET /_internal/db-path`` response body.

    Keys:
        mode: One of ``"runtime"`` / ``"direct"`` / ``"memory"`` — the
            :class:`~mcp_middleware.runtime_db.BindingMode` of the live
            binding. Operators read this to understand which physical
            location the server is reading.
        path: The path the engine actually reads from. Equals
            ``runtime`` for file-backed modes; ``":memory:"`` for
            memory mode.
        canonical: Original canonical path passed to ``bind_engine``,
            or ``null`` for memory mode.
        runtime: Resolved runtime path the live engine reads. For
            direct mode this equals ``canonical``; for runtime mode it
            equals the hashed ``/tmp`` copy; ``null`` for memory mode.
        wal: ``<runtime>-wal`` sidecar path. ``null`` for memory mode.
        shm: ``<runtime>-shm`` sidecar path. ``null`` for memory mode.
        marker: ``<runtime>.srcmeta`` provenance marker path. ``null``
            for memory mode.
        url: The SQLAlchemy URL string the engine was opened with
            (typically ``"sqlite:///..."`` or ``"sqlite://"``).

    Workers (populate, backup, snapshot) call this BEFORE touching the
    runtime DB so they know what file they're synchronising with — the
    alternative is each app exporting its own ``DATABASE_PATH`` env var,
    which drifts every time the runtime-DB hashing scheme changes.
    """

    mode: str
    path: str
    canonical: str | None
    runtime: str | None
    wal: str | None
    shm: str | None
    marker: str | None
    url: str


def register_runtime_db_routes(
    mcp_or_app: Any,
    binding: EngineBinding | Engine | None = None,
    *,
    engine: Engine | None = None,
    path: str = "/_internal/checkpoint",
    info_path: str = "/_internal/db-path",
    disable_db_path: str = "/_internal/disable_db",
    enable_db_path: str = "/_internal/enable_db",
    persist_path: str = "/_internal/persist",
    populate_path: str = "/_internal/populate",
    populate_working_dir: Path | None = None,
    populate_mise_task: str = DEFAULT_MISE_TASK,
) -> None:
    """Mount the shared runtime-DB HTTP routes on ``mcp_or_app``.

    The following routes are mounted (``populate`` only when opted in):

    * ``POST {path}`` — drains the engine pool and runs ``PRAGMA
      wal_checkpoint(TRUNCATE)``. Returns a :class:`CheckpointResult`
      JSON body. Out-of-process workers POST this immediately before
      they refresh the runtime DB from canonical (see
      :func:`refresh_runtime_from_canonical`). This route does NOT
      toggle the DB gate — it's the explicit "operator drain" path.

    * ``GET {info_path}`` — returns the binding's resolved paths as a
      :class:`DbPathInfo` JSON body. Only mounted when ``binding`` is
      provided; the deprecated engine-only call shape mounts a degraded
      info route that reports ``mode="unknown"`` plus the URL-derived
      path (we don't know the canonical/mode without a binding).

    * ``POST {disable_db_path}`` — closes the DB gate (see
      :mod:`.db_gate`) and disposes the engine pool. After this returns
      200, all non-whitelisted app requests get a 503 with
      ``Retry-After``, freeing the lifecycle script to rewrite the
      runtime DB file inode without racing live SQLAlchemy connections.

    * ``POST {enable_db_path}`` — opens the gate. The lifecycle
      script's ``finally`` / ``trap`` calls this on clean exit. A
      crashed script that never reaches this endpoint leaves the gate
      closed on purpose — see :mod:`.db_gate` for the sticky-closed
      rationale.

    * ``POST {persist_path}`` — folds the live server's runtime DB back
      onto its canonical *in-process* (see
      :func:`persist_runtime_to_canonical`). This is the write-side
      counterpart to ``/_internal/checkpoint``: because the runtime DB
      lives under a per-uid ``0o700`` dir, an out-of-process snapshot /
      fold hook running as a *different* uid cannot read the server's
      runtime to harvest its tool-call mutations — so the server persists
      them to the (shared-storage, cross-uid-readable) canonical itself
      when asked. Returns a :class:`PersistResult` JSON body; 500 if the
      WAL never cleared, 501 if the routes were registered without a
      binding. **Distinct from the checkpoint route on purpose** —
      checkpoint is POSTed BEFORE a canonical→runtime refresh, so folding
      runtime→canonical inside it would clobber a freshly-uploaded
      canonical. Only meaningful in RUNTIME mode; a no-op success for
      direct / memory bindings.

    * ``POST {populate_path}`` — **opt-in.** Only mounted when
      ``populate_working_dir=`` is provided. Accepts a JSON body with
      ``input_path`` (a CSV directory or a single ``.db`` file), stages
      those files into ``$STATE_LOCATION``, and spawns ``mise run
      <populate_mise_task> <state_dir>`` as a detached subprocess.
      Returns HTTP 202 with the PID + log path (fire-and-forget), or
      HTTP 200 / 500 with the log tail when the caller passes
      ``wait: true``. See :mod:`.populate_route` for the full request /
      response shape.

    Args:
        mcp_or_app: A FastMCP instance (uses ``@mcp.custom_route``) or a
            Starlette / FastAPI app (uses ``app.add_route``). The
            detection is duck-typed on ``custom_route``.
        binding: An :class:`EngineBinding` from
            :func:`~mcp_middleware.runtime_db.bind_engine`. Carries the
            engine + every path the info route needs. **This is the
            preferred form.** For one release we also accept a raw
            :class:`sqlalchemy.Engine` here (positional or via ``engine=``);
            both emit :class:`DeprecationWarning` and route through a
            degraded info-route fallback.
        engine: **Deprecated.** Pass the engine on its own (no binding).
            Equivalent to passing the engine positionally as
            ``binding``; both call shapes raise the same
            :class:`DeprecationWarning`. Removed in the release after
            next; migrate to ``binding=``.
        path: HTTP path for the checkpoint route. Defaults to
            ``/_internal/checkpoint``; override only if the default
            collides with an existing route.
        info_path: HTTP path for the db-path info route. Defaults to
            ``/_internal/db-path``; override on collision.
        disable_db_path: HTTP path for the gate-close route. Defaults
            to ``/_internal/disable_db``. If you override this, also
            update the corresponding entry in your
            :class:`~mcp_middleware.runtime_db.db_gate.DbGateMiddleware`
            ``whitelist=`` so the route stays reachable while the gate
            is closed.
        enable_db_path: HTTP path for the gate-open route. Defaults to
            ``/_internal/enable_db``. Same whitelist caveat as
            ``disable_db_path``.
        persist_path: HTTP path for the runtime→canonical persist route.
            Defaults to ``/_internal/persist``. The default
            :data:`~mcp_middleware.runtime_db.db_gate.DEFAULT_WHITELIST`
            already covers it; if you override this kwarg, also update your
            :class:`~mcp_middleware.runtime_db.db_gate.DbGateMiddleware`
            ``whitelist=`` so it stays reachable while the gate is closed.
        populate_path: HTTP path for the populate-trigger route.
            Defaults to ``/_internal/populate``. **Only mounted when
            ``populate_working_dir`` is not None.** Same whitelist
            caveat as the other lifecycle routes — the default
            :data:`~mcp_middleware.runtime_db.db_gate.DEFAULT_WHITELIST`
            already covers ``/_internal/populate``; if you override
            this kwarg you must also update your whitelist so the
            populate endpoint stays reachable while the gate is closed.
        populate_working_dir: Absolute path to the directory containing
            the ``mise.toml`` that defines the populate task. Typically
            the repo root of the consuming app. When ``None`` (default),
            the populate route is NOT mounted — apps that don't want an
            HTTP-triggerable populate just omit this kwarg. Passing an
            existing directory is enough to opt in.

            **Adopter requirement:** your ``populate.sh`` MUST consume
            ``$1`` as its state-location channel (with the env var as
            fallback), because ``mise.toml``'s ``[env]`` block is
            applied *after* the endpoint's subprocess env and will
            silently override ``STATE_LOCATION``. Required shape::

                STATE_LOCATION="${1:-${STATE_LOCATION:-/.apps_data/appname}}"

            Consumers that hardcode ``STATE_LOCATION`` will silently
            misfire — the subprocess returns 0 (finds real CSVs at the
            production path), and the endpoint reports
            ``status="completed"``, but the staged inputs are ignored.
            See :mod:`.populate_route` module docstring, "ADOPTER
            CHECKLIST".
        populate_mise_task: Name of the mise task to invoke. Defaults
            to ``"populate"`` — matches the ``[tasks.populate]`` entry
            in every Foundry-* ``mise.toml``. Override only if the app
            renamed the task (e.g. multi-server repos with distinct
            populate flows).

    Raises:
        TypeError: If neither ``binding`` (positional or keyword) nor
            ``engine=`` is provided, or if ``mcp_or_app`` exposes
            neither ``custom_route`` nor ``add_route`` / ``router``.
    """
    if binding is not None and engine is not None:
        raise TypeError("register_runtime_db_routes: pass binding= OR engine= (not both)")

    # Disambiguate: the positional ``binding`` param may carry either
    # an EngineBinding (new API), a raw Engine (deprecated call shape
    # via positional), or None (deprecated keyword-only ``engine=``
    # path). isinstance is the cleanest signal — sqlalchemy.Engine and
    # EngineBinding share no inheritance.
    actual_binding: EngineBinding | None = None
    actual_engine: Engine | None = None

    if isinstance(binding, EngineBinding):
        actual_binding = binding
        actual_engine = binding.engine
    elif binding is not None:
        # Positional raw Engine — deprecated call shape, but keep working.
        warnings.warn(
            "register_runtime_db_routes: passing a raw Engine is deprecated; "
            "pass an EngineBinding from mcp_middleware.runtime_db.bind_engine "
            "via binding= instead. The engine-only call will be removed in "
            "the release after next.",
            DeprecationWarning,
            stacklevel=2,
        )
        actual_engine = binding  # type: ignore[assignment]  # checked-by-runtime
    elif engine is not None:
        warnings.warn(
            "register_runtime_db_routes: the engine= keyword is deprecated; "
            "pass an EngineBinding from mcp_middleware.runtime_db.bind_engine "
            "via binding= instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        actual_engine = engine
    else:
        raise TypeError(
            "register_runtime_db_routes: must pass binding= (preferred) or "
            "the deprecated engine= keyword"
        )

    # Compute the static info payload once — its values are determined
    # by the binding/engine at registration time and never change for
    # the lifetime of the binding. The info route returns it verbatim,
    # so we avoid per-request recomputation.
    info_payload = _build_db_path_info(actual_binding, actual_engine)

    # Defer the starlette import so callers that pass a FastMCP instance
    # (which has its own response helper) don't pay the import cost when
    # they don't need it.
    custom_route = getattr(mcp_or_app, "custom_route", None)
    if callable(custom_route):
        _register_fastmcp(
            mcp_or_app,
            actual_engine,
            actual_binding,
            path,
            info_path,
            info_payload,
            disable_db_path,
            enable_db_path,
            persist_path,
            populate_path,
            populate_working_dir,
            populate_mise_task,
        )
        return

    add_route = getattr(mcp_or_app, "add_route", None) or getattr(mcp_or_app, "router", None)
    if add_route is not None:
        _register_starlette(
            mcp_or_app,
            actual_engine,
            actual_binding,
            path,
            info_path,
            info_payload,
            disable_db_path,
            enable_db_path,
            persist_path,
            populate_path,
            populate_working_dir,
            populate_mise_task,
        )
        return

    raise TypeError(
        f"register_runtime_db_routes: {type(mcp_or_app).__name__} has no "
        "custom_route() (FastMCP) or add_route()/router (Starlette/FastAPI)"
    )


def _build_db_path_info(
    binding: EngineBinding | None,
    engine: Engine | None,
) -> DbPathInfo:
    """Resolve the static info payload for the ``/_internal/db-path`` route.

    Two shapes:

    * ``binding`` provided (new API): full payload with mode, canonical,
      runtime, sidecars, and url derived from the binding's accessors.
    * ``binding`` is None but ``engine`` is (deprecated call shape):
      degraded payload with ``mode="unknown"`` — we know the URL the
      engine was opened with, but not whether it's a /tmp copy or in
      place, so the operator-facing answer is "look at the path
      yourself". Better than 404'ing the info route entirely.
    """
    if binding is not None:
        return _build_info_from_binding(binding)
    assert engine is not None
    return _build_info_from_engine_url(engine)


def _build_info_from_binding(binding: EngineBinding) -> DbPathInfo:
    """Full info payload — every field comes from the binding."""
    if binding.mode is BindingMode.MEMORY:
        return DbPathInfo(
            mode=binding.mode.value,
            path=":memory:",
            canonical=None,
            runtime=None,
            wal=None,
            shm=None,
            marker=None,
            url=binding.url,
        )
    # File mode — binding.paths is populated for both runtime and direct.
    assert binding.paths is not None
    assert binding.runtime is not None
    return DbPathInfo(
        mode=binding.mode.value,
        path=str(binding.runtime),
        canonical=str(binding.canonical) if binding.canonical else None,
        runtime=str(binding.runtime),
        wal=str(binding.paths.wal),
        shm=str(binding.paths.shm),
        marker=str(binding.paths.marker),
        url=binding.url,
    )


def _build_info_from_engine_url(engine: Engine) -> DbPathInfo:
    """Degraded info payload — we only have the engine URL.

    Used on the deprecated ``register_runtime_db_routes(mcp, engine)`` /
    ``engine=`` call paths. The route still exists so operators can hit
    a uniform endpoint, but mode is reported as ``"unknown"`` and
    sidecar paths are derived from the URL (best-effort).
    """
    url = str(engine.url)
    database = engine.url.database
    if not database or database == ":memory:":
        return DbPathInfo(
            mode="unknown",
            path=":memory:",
            canonical=None,
            runtime=None,
            wal=None,
            shm=None,
            marker=None,
            url=url,
        )
    # Synthesise sidecars from the engine URL's database path — this
    # matches what SQLite will actually create when the engine writes.
    return DbPathInfo(
        mode="unknown",
        path=database,
        canonical=None,  # unknown without a binding
        runtime=database,
        wal=f"{database}-wal",
        shm=f"{database}-shm",
        marker=f"{database}.srcmeta",
        url=url,
    )


def _run_persist(binding: EngineBinding | None) -> tuple[dict[str, Any], int]:
    """Resolve the ``/_internal/persist`` response body + HTTP status.

    Shared by the FastMCP and Starlette arms. When the routes were registered
    via the deprecated engine-only shape (no binding), persist is impossible —
    we don't know the canonical to fold onto — so we return a 501 with a
    remediation message rather than silently no-op'ing (which would look like a
    successful persist and drop the mutation). A ``busy``/``error`` result maps
    to 500 so the calling snapshot hook refuses to harvest stale data.
    """
    if binding is None:
        return (
            {
                "error": (
                    "persist requires an EngineBinding; this server registered "
                    "runtime-db routes with the deprecated engine-only shape. "
                    "Pass binding= from mcp_middleware.runtime_db.bind_engine (or "
                    "run_server with engine= + runtime_canonical=)."
                ),
                "mode": "unknown",
                "busy": 1,
                "bytes_copied": 0,
                "canonical": None,
                "runtime": None,
            },
            501,
        )
    result = persist_runtime_to_canonical(binding)
    status = 500 if ("error" in result or result["busy"]) else 200
    return dict(result), status


def _register_fastmcp(
    mcp: Any,
    engine: Engine,
    binding: EngineBinding | None,
    path: str,
    info_path: str,
    info_payload: DbPathInfo,
    disable_db_path: str,
    enable_db_path: str,
    persist_path: str,
    populate_path: str,
    populate_working_dir: Path | None,
    populate_mise_task: str,
) -> None:
    """FastMCP path: use the ``custom_route`` decorator."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from .db_gate import set_db_disabled

    @mcp.custom_route(path, methods=["POST"])
    async def _checkpoint_route(_: Request) -> JSONResponse:
        result = run_wal_checkpoint(engine)
        status = 500 if "error" in result else 200
        return JSONResponse(result, status_code=status)

    @mcp.custom_route(info_path, methods=["GET"])
    async def _info_route(_: Request) -> JSONResponse:
        return JSONResponse(info_payload, status_code=200)

    @mcp.custom_route(persist_path, methods=["POST"])
    async def _persist_route(_: Request) -> JSONResponse:
        result, status = _run_persist(binding)
        return JSONResponse(result, status_code=status)

    @mcp.custom_route(disable_db_path, methods=["POST"])
    async def _disable_db_route(_: Request) -> JSONResponse:
        # Close the gate BEFORE disposing the pool so any in-flight
        # requests that arrive between these two lines see the gate as
        # closed and 503 instead of grabbing a connection we're about
        # to invalidate.
        set_db_disabled(True)
        # Drain pooled fds so the lifecycle script can swap the runtime
        # DB inode without an active connection pinning the old one.
        # Active checked-out connections close at the next check-in;
        # idle pooled ones close immediately.
        try:
            engine.dispose()
        except Exception as exc:  # noqa: BLE001 - dispose failure mustn't block the gate
            logger.warning("disable_db: engine.dispose() failed: %s", exc)
        return JSONResponse({"db_disabled": True}, status_code=200)

    @mcp.custom_route(enable_db_path, methods=["POST"])
    async def _enable_db_route(_: Request) -> JSONResponse:
        set_db_disabled(False)
        return JSONResponse({"db_disabled": False}, status_code=200)

    if populate_working_dir is not None:

        @mcp.custom_route(populate_path, methods=["POST"])
        async def _populate_route(request: Request) -> JSONResponse:
            try:
                body = await request.json()
            except Exception as exc:  # noqa: BLE001 - malformed JSON must surface as 400
                return JSONResponse(
                    {"status": "error", "error": f"malformed JSON body: {exc}"},
                    status_code=400,
                )
            response_body, http_status = handle_populate_request(
                body,
                working_dir=populate_working_dir,
                mise_task=populate_mise_task,
            )
            return JSONResponse(response_body, status_code=http_status)


def _register_starlette(
    app: Any,
    engine: Engine,
    binding: EngineBinding | None,
    path: str,
    info_path: str,
    info_payload: DbPathInfo,
    disable_db_path: str,
    enable_db_path: str,
    persist_path: str,
    populate_path: str,
    populate_working_dir: Path | None,
    populate_mise_task: str,
) -> None:
    """Starlette / FastAPI path: use ``app.add_route``."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from .db_gate import set_db_disabled

    async def _checkpoint_route(_: Request) -> JSONResponse:
        result = run_wal_checkpoint(engine)
        status = 500 if "error" in result else 200
        return JSONResponse(result, status_code=status)

    async def _info_route(_: Request) -> JSONResponse:
        return JSONResponse(info_payload, status_code=200)

    async def _persist_route(_: Request) -> JSONResponse:
        result, status = _run_persist(binding)
        return JSONResponse(result, status_code=status)

    async def _disable_db_route(_: Request) -> JSONResponse:
        # Order matters: close the gate first, then drain. See the
        # FastMCP twin above for the rationale.
        set_db_disabled(True)
        try:
            engine.dispose()
        except Exception as exc:  # noqa: BLE001 - dispose failure mustn't block the gate
            logger.warning("disable_db: engine.dispose() failed: %s", exc)
        return JSONResponse({"db_disabled": True}, status_code=200)

    async def _enable_db_route(_: Request) -> JSONResponse:
        set_db_disabled(False)
        return JSONResponse({"db_disabled": False}, status_code=200)

    async def _populate_route(request: Request) -> JSONResponse:
        # Only reachable when populate_working_dir was provided;
        # register_runtime_db_routes gates the actual mount below.
        assert populate_working_dir is not None
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001 - malformed JSON must surface as 400
            return JSONResponse(
                {"status": "error", "error": f"malformed JSON body: {exc}"},
                status_code=400,
            )
        response_body, http_status = handle_populate_request(
            body,
            working_dir=populate_working_dir,
            mise_task=populate_mise_task,
        )
        return JSONResponse(response_body, status_code=http_status)

    if hasattr(app, "add_route"):
        app.add_route(path, _checkpoint_route, methods=["POST"])
        app.add_route(info_path, _info_route, methods=["GET"])
        app.add_route(disable_db_path, _disable_db_route, methods=["POST"])
        app.add_route(enable_db_path, _enable_db_route, methods=["POST"])
        app.add_route(persist_path, _persist_route, methods=["POST"])
        if populate_working_dir is not None:
            app.add_route(populate_path, _populate_route, methods=["POST"])
    else:  # FastAPI exposes app.router
        app.router.add_route(path, _checkpoint_route, methods=["POST"])
        app.router.add_route(info_path, _info_route, methods=["GET"])
        app.router.add_route(disable_db_path, _disable_db_route, methods=["POST"])
        app.router.add_route(enable_db_path, _enable_db_route, methods=["POST"])
        app.router.add_route(persist_path, _persist_route, methods=["POST"])
        if populate_working_dir is not None:
            app.router.add_route(populate_path, _populate_route, methods=["POST"])
