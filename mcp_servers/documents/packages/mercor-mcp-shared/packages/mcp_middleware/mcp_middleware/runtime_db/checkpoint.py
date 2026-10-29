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
  correct semantics, not a bug). Before reopening, the route runs
  :func:`adopt_late_delivery`: if this server bound a BLANK world
  (``EngineBinding.delivered=False`` — it booted before the world
  download landed) and a canonical has since appeared, the freshly
  populated canonical is adopted into the working copy right here, so
  the late-delivery window closes generically at populate-end instead
  of waiting for each app's first tool call to reach a self-heal hook.

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
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict

from .binding import EngineBinding
from .ensure import DEFAULT_ENSURE_TIMEOUT, ensure_db_ready
from .paths import clear_awaiting_delivery, fingerprint_canonical, read_marker, write_marker
from .populate_route import DEFAULT_MISE_TASK, handle_populate_request
from .sync import _copy_db_with_wal_fold
from .working import WorkingMode

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy import Engine

    from .ready import EngineHolder

logger = logging.getLogger(__name__)

__all__ = [
    "CheckpointResult",
    "DbPathInfo",
    "PersistResult",
    "adopt_late_delivery",
    "persist_runtime_to_canonical",
    "register_runtime_db_routes",
    "run_wal_checkpoint",
]


def adopt_late_delivery(
    binding: EngineBinding | None,
    *,
    runtime_schema_hook: Callable[[Path], None] | None = None,
    holder: EngineHolder | None = None,
    ensure_timeout: float = DEFAULT_ENSURE_TIMEOUT,
) -> dict[str, Any]:
    """Adopt a canonical that arrived *after* this server bound a blank world.

    The late-delivery window: a server that boots before the world download /
    populate lands binds a BLANK runtime (``EngineBinding.delivered=False`` —
    the engine created the file lazily) and that bind is otherwise permanent
    for every request path that never reaches an app-level self-heal hook
    (e.g. the default-user gate's ``decide()``). The populate-end gate reopen
    (``POST /_internal/enable_db``) is the generic "the world just arrived"
    signal, so the enable route calls this first — BEFORE clearing the gate,
    while traffic still 503s — and the working copy is fresh by the time the
    first real request lands.

    Semantics, by binding shape:

    * ``delivered=True`` / memory mode / no binding → no-op. A binding that
      started from a real DB keeps its normal refresh path (pool recycle at
      handoff); adopting here could clobber legitimate post-boot writes.
      This function is strictly a blank-world escape hatch.
    * Canonical still absent → no-op (the world hasn't arrived; nothing to
      adopt).
    * **In-place modes** (``runtime`` IS ``canonical``): the populate wrote
      the very file the engine binds, possibly replacing its inode — dispose
      the pool so fresh connections reopen the path and see the new bytes, and
      clear the blank-world ``.awaiting`` sentinel so a later RESTART sees the
      canonical present-without-sentinel and reconstructs ``delivered=True``
      (rather than re-arming the adopt and refusing the in-place fold).
    * **Move modes** (tmpfs / workdir): dispose the pool, WAL-fold-copy
      ``canonical → runtime`` (fresh inode, sidecars dropped), run
      ``runtime_schema_hook`` on the fresh working copy (parity with the
      default-user gate's self-heal refresh — e.g. rebuild ephemeral
      runtime tables / indexes the snapshot stripped), stamp the
      ``.srcmeta`` marker. If the marker already matches the canonical's
      fingerprint the copy is skipped (idempotent re-POSTs of enable_db).

    On success the binding's ``delivered`` flag is corrected to ``True``
    (via ``object.__setattr__`` — the dataclass is frozen, but the flag is
    *provenance*, and the binding now demonstrably has a delivered DB); the
    next call no-ops. Best-effort throughout: any failure is logged and
    reported in the returned dict, never raised — a failed adoption must not
    block the gate reopen (the pre-existing self-heal paths still apply).

    Trigger 1 — the populate signal is the *initial* bind (no-proactive-bind)
    -----------------------------------------------------------------------

    Under the no-proactive-bind lifecycle the routes are registered with no
    static ``binding`` (``run_server`` stashes the recipe on the holder via
    :func:`register_db_lifecycle` and binds nothing at boot). The enable_db
    signal — "the world just arrived" — is therefore where the FIRST bind
    happens if no request triggered one during the populate window. When
    ``binding is None`` and a ``holder`` is supplied:

    * Holder already READY (a request bound a blank world mid-populate) → adopt
      that live binding through the normal path below.
    * Holder UNBOUND with a spec → bind now via :func:`ensure_db_ready`. The
      freshly populated canonical binds ``delivered=True`` directly, so there
      is no blank working copy to fold — this returns success immediately. If
      the bind itself fails it is reported (not raised) and the DB stays
      unbound so a later request-arm trigger retries (ruling D3).
    * Holder has no spec → no-op (a legacy engine-only registration).

    Args:
        binding: The static binding captured at route registration, or
            ``None`` under the no-proactive-bind lifecycle (resolved from
            ``holder`` instead).
        runtime_schema_hook: See the move-mode adoption path above.
        holder: The process lifecycle :class:`EngineHolder`. Supplied by
            ``run_server`` so this route can perform / adopt the initial bind
            (Trigger 1). ``None`` preserves the classic static-binding path.
        ensure_timeout: Bound (seconds) forwarded to :func:`ensure_db_ready`
            when this route performs the initial bind.

    Returns:
        ``{"late_delivery_adopted": bool, "late_delivery_detail": str}`` —
        merged into the enable_db response body so the lifecycle script's
        log (the visible stream) records what happened.
    """

    def _result(adopted: bool, detail: str) -> dict[str, Any]:
        log = logger.info if adopted else logger.debug
        log("enable_db late-delivery adoption: %s (%s)", adopted, detail)
        return {"late_delivery_adopted": adopted, "late_delivery_detail": detail}

    # Trigger 1 — resolve / perform the initial bind from the holder when the
    # routes carry no static binding (no-proactive-bind: the recipe lives on
    # the holder, nothing bound at boot). See the docstring for the matrix.
    if binding is None and holder is not None:
        if holder.is_ready():
            # A request already triggered a (blank) bind during the populate
            # window — adopt that live binding through the normal path below.
            binding = holder.binding
        else:
            spec = holder.spec
            if spec is None:
                return _result(False, "no binding and holder carries no spec")
            try:
                bound = ensure_db_ready(spec, holder=holder, timeout=ensure_timeout)
            except Exception as exc:  # noqa: BLE001 - a failed initial bind must not block the gate reopen
                return _result(
                    False,
                    f"initial bind from populate signal failed "
                    f"({type(exc).__name__}: {exc}); DB remains unbound, a later "
                    f"request-triggered bind retries (non-terminal)",
                )
            # The initial bind succeeded — the holder went UNBOUND → READY on
            # this call, which is the meaningful positive outcome of Trigger 1.
            # A freshly populated canonical classifies delivered=True (no blank
            # working copy to fold); memory / a still-blank world simply have
            # nothing to adopt. In every case the DB is now bound, so report it
            # as adopted with a detail that carries the nuance.
            if bound.delivered:
                detail = (
                    f"initial bind from populate signal: bound delivered "
                    f"canonical {bound.canonical}"
                )
            elif bound.canonical is None:
                detail = (
                    "initial bind from populate signal: bound (memory mode; no world to deliver)"
                )
            else:
                detail = (
                    f"initial bind from populate signal: bound blank world "
                    f"(canonical {bound.canonical} not present yet); a later "
                    f"enable_db folds the delivery when it lands"
                )
            return _result(True, detail)

    if binding is None:
        return _result(False, "no binding registered (deprecated engine-only routes)")
    if binding.delivered:
        return _result(False, "binding already has a delivered DB")
    if binding.mode is WorkingMode.MEMORY or binding.canonical is None or binding.runtime is None:
        return _result(False, f"mode {binding.mode} has no canonical to adopt")
    if not binding.canonical.exists():
        return _result(False, f"no canonical at {binding.canonical} yet (still a blank world)")

    # Drain pooled fds first in every shape: the pool holds the BLANK file's
    # inode (in-place: possibly replaced by the delivery; move modes: about
    # to be replaced by the copy below). The gate is still closed / about to
    # be opened by our caller, so no request races the dispose.
    try:
        binding.engine.dispose()
    except Exception as exc:  # noqa: BLE001 - dispose failure mustn't abort adoption
        logger.warning("adopt_late_delivery: engine.dispose() failed: %s", exc)

    if binding.is_aliased:
        # The world landed at the very path the engine binds. Drop the
        # blank-world sentinel so a subsequent RESTART reconstructs
        # ``delivered=True`` (canonical present, no sentinel) instead of
        # re-arming the adopt and refusing the fold. Best-effort — a stale
        # sentinel only costs one extra (harmless) adopt round trip.
        clear_awaiting_delivery(binding.canonical)
        object.__setattr__(binding, "delivered", True)
        return _result(
            True,
            f"in-place bind: pool disposed; fresh connections now read the "
            f"delivered canonical {binding.canonical}",
        )

    paths = binding.paths
    assert paths is not None  # file modes always carry RuntimePaths
    try:
        fingerprint = fingerprint_canonical(binding.canonical)
        if binding.runtime.exists() and read_marker(paths.marker) == fingerprint:
            object.__setattr__(binding, "delivered", True)
            return _result(
                True,
                f"working copy {binding.runtime} already carries canonical "
                f"fingerprint {fingerprint} (previously adopted)",
            )
        _copy_db_with_wal_fold(
            binding.canonical, binding.runtime, post_copy_hook=runtime_schema_hook
        )
        write_marker(paths.marker, binding.canonical)
    except Exception as exc:  # noqa: BLE001 - adoption must never block the gate reopen
        logger.warning(
            "adopt_late_delivery: could not adopt %s into %s: %s: %s — the "
            "blank working copy remains bound; app-level self-heal (if any) "
            "may still adopt it later",
            binding.canonical,
            binding.runtime,
            type(exc).__name__,
            exc,
        )
        return _result(
            False,
            f"adoption failed ({type(exc).__name__}: {exc}); blank working copy still bound",
        )
    object.__setattr__(binding, "delivered", True)
    return _result(
        True,
        f"copied delivered canonical {binding.canonical} over blank working "
        f"copy {binding.runtime} (WAL folded, marker stamped)",
    )


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
        #
        # Qualify the checkpoint to ``main``. An unqualified
        # ``PRAGMA wal_checkpoint`` checkpoints EVERY attached database, and
        # the fresh raw connection re-fires the engine's ``connect`` listener
        # — which, for an FTS-sidecar app, ``ATTACH``es ``<db>.fts.db`` AS
        # ``fts`` before this PRAGMA runs. SQLite then tries to checkpoint the
        # attached ``fts`` DB too and raises "database fts is already in use",
        # 500ing the ``/_internal/checkpoint`` route. We only ever want to
        # fold the runtime's OWN WAL here; the co-located FTS sidecar is a
        # derived, in-place-protected file with its own lifecycle. On a plain
        # (non-sidecar) engine ``main`` IS the sole/default DB, so qualifying
        # is a no-op there.
        raw_conn = engine.raw_connection()
        try:
            row = raw_conn.cursor().execute("PRAGMA main.wal_checkpoint(TRUNCATE)").fetchone()
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
        mode: The binding's :class:`~mcp_middleware.runtime_db.WorkingMode`
            value (``"in-place"`` / ``"memory"``).
        busy: 1 if the WAL couldn't be fully folded (a live writer
            still pins it); 0 on success. In-place folds report busy=0 (the
            unfolded frames stay durable in the ``-wal`` sidecar).
        bytes_copied: Always 0 — in-place folds mutate the canonical directly
            (no copy) and memory mode is a no-op.
        canonical: The canonical path folded, or ``null`` when there is none
            (memory mode).
        runtime: The runtime path — equals ``canonical`` for in-place bindings,
            ``null`` for memory mode.
        stale: Optional — 1 iff the fold was refused by the **staleness
            guard**: the canonical's fingerprint no longer matches the
            runtime's provenance marker, meaning the canonical was delivered
            or replaced AFTER this runtime was seeded and the runtime holds
            nothing newer. Reported as success (``busy=0``, HTTP 200,
            ``bytes_copied=0``) because the canonical — untouched — is the
            correct harvest source.
        error: Optional — present iff the fold was refused (busy) or raised.
    """

    mode: str
    busy: int
    bytes_copied: int
    canonical: str | None
    runtime: str | None
    stale: NotRequired[int]


def persist_runtime_to_canonical(binding: EngineBinding) -> PersistResult:
    """Fold the live server's runtime DB back onto its canonical, in-process.

    This copies runtime→canonical so an out-of-process, *different-uid* worker
    can then read
    the canonical.

    It exists because the runtime DB lives under a per-uid ``0o700`` dir (see
    :func:`~mcp_middleware.runtime_db.runtime_paths_for`) that only the server's
    own uid can read — so a snapshot / fold hook running as a different uid
    physically cannot harvest the server's tool-call mutations from the runtime.
    The persistence therefore MUST happen in the server process; this is the
    primitive the ``POST /_internal/persist`` route wraps.

    Behaviour by mode (all binding is in place now — runtime IS canonical):

    * **IN_PLACE.** runtime IS canonical; a checkpoint folds any WAL into it in
      place. No copy. ``bytes_copied=0``. A busy checkpoint is **not** an
      error here — the unfolded frames are still durable in the ``-wal`` sidecar
      (it's part of the DB), so it always reports ``busy=0`` (success) and just
      logs the partial fold. One guard applies: a **blank cold-seed** in-place
      bind (``delivered=False`` — booted before the world was delivered) must
      NOT fold its empty engine state over a world that may have landed at the
      canonical path since; it REFUSES (success-no-copy, ``stale=1``) until
      ``adopt_late_delivery`` flips ``delivered=True``.
    * **MEMORY.** Nothing on disk. No-op success.

    (The old RUNTIME/moved-runtime copy-back path is gone — there is no longer
    any binding whose runtime differs from its canonical.)

    Idempotent and safe to call with no live writers.
    """
    mode = binding.mode
    if mode is WorkingMode.MEMORY:
        return PersistResult(mode=mode.value, busy=0, bytes_copied=0, canonical=None, runtime=None)

    if binding.is_aliased:
        # ── Blank-fold guard (in-place analog of the RUNTIME staleness guard) ──
        # An in-place binding whose runtime IS the canonical normally folds its
        # WAL in place. But a BLANK cold-seed in-place bind — one that booted
        # before the world was delivered, so ``delivered=False`` and the file
        # holds at most ``create_all``'s empty schema — must NOT fold: a real
        # world may already have landed at the canonical path (blank-boot → late
        # in-place delivery), and checkpointing the blank engine's pages/WAL over
        # it clobbers the delivery with ~700 KiB of empty schema (observed in
        # production: ``persist mode=in-place bytes=0`` over a 1.38 GB world).
        # Refuse — report success-no-copy with ``stale=1`` exactly like the
        # moved-mode guard, so the snapshot harvests the untouched canonical.
        # ``adopt_late_delivery`` (enable_db) disposes the blank pool and flips
        # ``delivered=True`` once the world arrives, after which a normal in-place
        # fold resumes. See :attr:`EngineBinding.awaiting_delivery`.
        if binding.awaiting_delivery:
            logger.warning(
                "persist: REFUSING in-place WAL fold onto %s — this binding is a "
                "blank cold-seed (delivered=False): it booted before the world was "
                "delivered, so folding its (empty) engine state in place would "
                "clobber any world that has since landed at that path. Reporting "
                "success-no-copy (stale=1); adopt_late_delivery picks up the "
                "delivery at enable_db.",
                binding.canonical,
            )
            canonical_str_blank = str(binding.canonical) if binding.canonical else None
            blank_result: Any = {
                "mode": mode.value,
                "busy": 0,
                "bytes_copied": 0,
                "canonical": canonical_str_blank,
                "runtime": canonical_str_blank,
                "stale": 1,
            }
            return blank_result

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

    # Not memory, not in-place: impossible for a valid binding now. All file
    # binding is in place (runtime IS canonical); the only non-in-place mode is
    # MEMORY, handled above. A non-aliased file binding can only come from a
    # hand-constructed/legacy binding — fail loud rather than resurrect the
    # removed copy-back (moved-runtime → canonical) path.
    raise AssertionError(
        "persist_runtime_to_canonical: non-aliased file binding is unsupported — "
        "all binding is in place now (runtime must equal canonical). "
        f"mode={mode.value} canonical={binding.canonical} runtime={binding.runtime}"
    )


class DbPathInfo(TypedDict):
    """Shape of the ``GET /_internal/db-path`` response body.

    Keys:
        mode: One of ``"in-place"`` / ``"memory"`` — the
            :class:`~mcp_middleware.runtime_db.WorkingMode` of the live
            binding. Operators read this to understand which physical
            location the server is reading.
        path: The path the engine actually reads from. Equals
            ``canonical`` for the in-place mode; ``":memory:"`` for
            memory mode.
        canonical: Original canonical path passed to ``bind_engine``,
            or ``null`` for memory mode.
        runtime: Resolved runtime path the live engine reads. Equals
            ``canonical`` for in-place mode (runtime IS canonical);
            ``null`` for memory mode.
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
    runtime_schema_hook: Callable[[Path], None] | None = None,
    holder: EngineHolder | None = None,
    ensure_timeout: float = DEFAULT_ENSURE_TIMEOUT,
) -> None:
    """Mount the shared runtime-DB HTTP routes on ``mcp_or_app``.

    The following routes are mounted (``populate`` only when opted in):

    * ``POST {path}`` — drains the engine pool and runs ``PRAGMA
      wal_checkpoint(TRUNCATE)``. Returns a :class:`CheckpointResult`
      JSON body. Out-of-process workers POST this immediately before
      they read the runtime DB out-of-band. This route does NOT
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
      rationale. Before clearing the gate the route runs
      :func:`adopt_late_delivery` (blank-world bindings adopt a
      canonical that arrived after boot); the adoption outcome is
      merged into the response body.

    * ``POST {persist_path}`` — folds the live server's runtime DB back
      onto its canonical *in-process* (see
      :func:`persist_runtime_to_canonical`). This is the write-side
      because the binding is in place (runtime IS canonical), the server's
      tool-call mutations already land in the canonical DB's ``-wal``
      sidecar; this route just asks the server to fold that WAL into the
      canonical in place so an out-of-process snapshot / fold hook (which
      may run as a *different* uid and cannot open the live engine) harvests
      a fully-checkpointed file. Returns a :class:`PersistResult` JSON body;
      500 if the WAL never cleared, 501 if the routes were registered
      without a binding. A no-op success for memory bindings.

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
        runtime_schema_hook: Optional callable invoked with the freshly
            refreshed runtime DB path after the ``enable_db`` route adopts
            a late-delivered canonical (move modes only — tmpfs / workdir).
            Parity with the default-user gate's self-heal refresh: pass the
            same hook you give :func:`install_default_user_gate` so ephemeral
            runtime tables / indexes the snapshot stripped are rebuilt on the
            adopt path too. Defaults to ``None`` (no hook).
        holder: The process lifecycle :class:`EngineHolder`. **This is the
            no-proactive-bind entry point:** pass ``holder=`` (with
            ``binding``/``engine`` omitted) and the routes bind nothing at
            registration — they resolve the live engine from the holder at
            request time, and the ``enable_db`` route performs the *initial*
            bind (Trigger 1) via :func:`adopt_late_delivery` when the populate
            signal arrives. When a static ``binding`` IS provided, ``holder``
            is still forwarded to the adopt call but the static binding wins
            for every other route (fully backward compatible).
        ensure_timeout: Seconds forwarded to :func:`adopt_late_delivery` /
            :func:`ensure_db_ready` when the ``enable_db`` route performs the
            initial bind. Defaults to :data:`DEFAULT_ENSURE_TIMEOUT`.

    Raises:
        TypeError: If none of ``binding`` (positional or keyword), ``engine=``,
            or ``holder=`` is provided, or if ``mcp_or_app`` exposes neither
            ``custom_route`` nor ``add_route`` / ``router``.
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
    elif holder is not None:
        # No-proactive-bind mode: nothing is bound at registration. The routes
        # resolve the live engine/binding from the holder at request time, and
        # the enable_db route performs the initial bind (Trigger 1).
        pass
    else:
        raise TypeError(
            "register_runtime_db_routes: must pass binding= (preferred), the "
            "deprecated engine= keyword, or holder= (no-proactive-bind mode)"
        )

    # Compute the static info payload once when a binding/engine is known at
    # registration — its values never change for the lifetime of that binding,
    # so the info route returns it verbatim. In holder-only mode the binding
    # doesn't exist yet, so pass None and let the route resolve dynamically.
    info_payload = (
        _build_db_path_info(actual_binding, actual_engine)
        if (actual_binding is not None or actual_engine is not None)
        else None
    )

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
            runtime_schema_hook,
            holder,
            ensure_timeout,
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
            runtime_schema_hook,
            holder,
            ensure_timeout,
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
    if binding.mode is WorkingMode.MEMORY:
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
    # File mode — in-place binding: binding.paths is populated, runtime==canonical.
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


def _make_live_binding(
    binding: EngineBinding | None,
    engine: Engine | None,
    holder: EngineHolder | None,
) -> Callable[[], EngineBinding | None]:
    """Return a resolver for the *current* binding.

    A static ``binding`` (classic call shape) always wins and is returned
    verbatim. Otherwise, in holder mode, the binding is resolved from the
    holder at call time — it starts ``None`` (unbound) and becomes the live
    binding once a trigger binds. The deprecated engine-only shape has no
    binding, so it resolves to ``None`` (persist / adopt no-op there as before).
    """

    def _resolve() -> EngineBinding | None:
        if binding is not None:
            return binding
        if holder is not None:
            return holder.binding
        return None

    return _resolve


def _make_live_engine(
    engine: Engine | None,
    holder: EngineHolder | None,
) -> Callable[[], Engine | None]:
    """Return a resolver for the *current* engine.

    A static ``engine`` (classic / deprecated shapes) wins. Otherwise resolve
    from the holder's live binding at call time — ``None`` while unbound.
    """

    def _resolve() -> Engine | None:
        if engine is not None:
            return engine
        if holder is not None:
            b = holder.binding
            if b is not None:
                return b.engine
        return None

    return _resolve


def _run_checkpoint(engine: Engine | None) -> tuple[dict[str, Any], int]:
    """Resolve the ``/_internal/checkpoint`` body + status.

    In holder mode the DB may not be bound yet — there is no engine and thus no
    WAL to fold, so return a benign 200 no-op rather than 500'ing. Once bound,
    this behaves exactly as before.
    """
    if engine is None:
        return (
            {
                "busy": 0,
                "frames_in_wal": 0,
                "frames_checkpointed": 0,
                "detail": "no engine bound yet (no-proactive-bind); nothing to checkpoint",
            },
            200,
        )
    result: dict[str, Any] = dict(run_wal_checkpoint(engine))
    status = 500 if "error" in result else 200
    return result, status


def _live_info(binding: EngineBinding | None, engine: Engine | None) -> DbPathInfo:
    """Resolve the ``/_internal/db-path`` payload dynamically (holder mode).

    Before the first bind neither a binding nor an engine exists, so report an
    ``"unbound"`` placeholder; once a trigger binds, the real paths appear.
    """
    if binding is None and engine is None:
        return DbPathInfo(
            mode="unbound",
            path="",
            canonical=None,
            runtime=None,
            wal=None,
            shm=None,
            marker=None,
            url="",
        )
    return _build_db_path_info(binding, engine)


def _register_fastmcp(
    mcp: Any,
    engine: Engine | None,
    binding: EngineBinding | None,
    path: str,
    info_path: str,
    info_payload: DbPathInfo | None,
    disable_db_path: str,
    enable_db_path: str,
    persist_path: str,
    populate_path: str,
    populate_working_dir: Path | None,
    populate_mise_task: str,
    runtime_schema_hook: Callable[[Path], None] | None = None,
    holder: EngineHolder | None = None,
    ensure_timeout: float = DEFAULT_ENSURE_TIMEOUT,
) -> None:
    """FastMCP path: use the ``custom_route`` decorator."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from .db_gate import set_db_disabled, set_populate_in_progress

    live_binding = _make_live_binding(binding, engine, holder)
    live_engine = _make_live_engine(engine, holder)

    @mcp.custom_route(path, methods=["POST"])
    async def _checkpoint_route(_: Request) -> JSONResponse:
        result, status = _run_checkpoint(live_engine())
        return JSONResponse(result, status_code=status)

    @mcp.custom_route(info_path, methods=["GET"])
    async def _info_route(_: Request) -> JSONResponse:
        return JSONResponse(info_payload or _live_info(live_binding(), live_engine()), 200)

    @mcp.custom_route(persist_path, methods=["POST"])
    async def _persist_route(_: Request) -> JSONResponse:
        result, status = _run_persist(live_binding())
        return JSONResponse(result, status_code=status)

    @mcp.custom_route(disable_db_path, methods=["POST"])
    async def _disable_db_route(_: Request) -> JSONResponse:
        # Close the gate BEFORE disposing the pool so any in-flight
        # requests that arrive between these two lines see the gate as
        # closed and 503 instead of grabbing a connection we're about
        # to invalidate. Mark populate-in-progress so the gate treats this
        # closed window as "worker owns the DB" (no background-bind kick),
        # not "boot-closed, awaiting first bind".
        set_populate_in_progress(True)
        set_db_disabled(True)
        # Lock out request-triggered binds and wait for any in-flight bind to
        # settle before the worker swaps the DB inode (Finding 1). Closing the
        # gate stops NEW HTTP-arm binds, but the holder-level lock-out also
        # covers the stdio tool arm and races where a bind was already claimed;
        # it atomically refuses begin_binding under the holder lock rather than
        # relying on the (check-then-act) gate flag. Run the (bounded) settle
        # wait OFF the event loop so a slow in-flight bind can't stall the whole
        # async runtime — health checks and other routes stay responsive.
        if holder is not None:
            import anyio

            settled = await anyio.to_thread.run_sync(holder.lock_out_binding, ensure_timeout)
            if not settled:
                logger.warning(
                    "disable_db: a runtime-DB bind was still in progress at the "
                    "%.1fs lock-out deadline; proceeding with the gate closed.",
                    ensure_timeout,
                )
        # Drain pooled fds so the lifecycle script can swap the runtime
        # DB inode without an active connection pinning the old one.
        # Active checked-out connections close at the next check-in;
        # idle pooled ones close immediately. In holder mode the DB may not
        # be bound yet — nothing to drain, which is fine.
        eng = live_engine()
        if eng is not None:
            try:
                eng.dispose()
            except Exception as exc:  # noqa: BLE001 - dispose failure mustn't block the gate
                logger.warning("disable_db: engine.dispose() failed: %s", exc)
        return JSONResponse({"db_disabled": True}, status_code=200)

    @mcp.custom_route(enable_db_path, methods=["POST"])
    async def _enable_db_route(_: Request) -> JSONResponse:
        # Re-permit binds (Finding 1): populate has finished writing the
        # canonical, so the disable_db lock-out is lifted before the Trigger-1
        # adopt below (which itself binds via begin_binding). Request-triggered
        # binds stay blocked in this window by the still-closed gate +
        # populate-in-progress flag, so the adopt is the sole binder.
        if holder is not None:
            holder.allow_binding()
        # Adopt a late-delivered canonical (or perform the initial bind in
        # holder mode — Trigger 1) BEFORE clearing the gate: while the gate is
        # closed no request can race the pool dispose / copy, and the first
        # request after reopen sees the adopted world.
        payload: dict[str, Any] = adopt_late_delivery(
            live_binding(),
            runtime_schema_hook=runtime_schema_hook,
            holder=holder,
            ensure_timeout=ensure_timeout,
        )
        # The populate window is closing: clear the flag BEFORE reopening the
        # gate so a request that races the reopen sees a consistent
        # (open, not-in-populate) state rather than a transient
        # (open, in-populate) one.
        set_populate_in_progress(False)
        set_db_disabled(False)
        payload["db_disabled"] = False
        return JSONResponse(payload, status_code=200)

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
    engine: Engine | None,
    binding: EngineBinding | None,
    path: str,
    info_path: str,
    info_payload: DbPathInfo | None,
    disable_db_path: str,
    enable_db_path: str,
    persist_path: str,
    populate_path: str,
    populate_working_dir: Path | None,
    populate_mise_task: str,
    runtime_schema_hook: Callable[[Path], None] | None = None,
    holder: EngineHolder | None = None,
    ensure_timeout: float = DEFAULT_ENSURE_TIMEOUT,
) -> None:
    """Starlette / FastAPI path: use ``app.add_route``."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from .db_gate import set_db_disabled, set_populate_in_progress

    live_binding = _make_live_binding(binding, engine, holder)
    live_engine = _make_live_engine(engine, holder)

    async def _checkpoint_route(_: Request) -> JSONResponse:
        result, status = _run_checkpoint(live_engine())
        return JSONResponse(result, status_code=status)

    async def _info_route(_: Request) -> JSONResponse:
        return JSONResponse(info_payload or _live_info(live_binding(), live_engine()), 200)

    async def _persist_route(_: Request) -> JSONResponse:
        result, status = _run_persist(live_binding())
        return JSONResponse(result, status_code=status)

    async def _disable_db_route(_: Request) -> JSONResponse:
        # Order matters: close the gate first, then drain. See the
        # FastMCP twin above for the rationale. Mark populate-in-progress so
        # the gate treats this as a worker-owned window, not boot-closed.
        set_populate_in_progress(True)
        set_db_disabled(True)
        # Lock out request-triggered binds and wait for any in-flight bind to
        # settle before the inode swap (Finding 1), off the event loop; see the
        # FastMCP twin above.
        if holder is not None:
            import anyio

            settled = await anyio.to_thread.run_sync(holder.lock_out_binding, ensure_timeout)
            if not settled:
                logger.warning(
                    "disable_db: a runtime-DB bind was still in progress at the "
                    "%.1fs lock-out deadline; proceeding with the gate closed.",
                    ensure_timeout,
                )
        eng = live_engine()
        if eng is not None:
            try:
                eng.dispose()
            except Exception as exc:  # noqa: BLE001 - dispose failure mustn't block the gate
                logger.warning("disable_db: engine.dispose() failed: %s", exc)
        return JSONResponse({"db_disabled": True}, status_code=200)

    async def _enable_db_route(_: Request) -> JSONResponse:
        # Re-permit binds before the Trigger-1 adopt (Finding 1); see the
        # FastMCP twin above for the rationale.
        if holder is not None:
            holder.allow_binding()
        # Adopt-before-reopen (or initial bind in holder mode — Trigger 1);
        # see the FastMCP twin above for the rationale.
        payload: dict[str, Any] = adopt_late_delivery(
            live_binding(),
            runtime_schema_hook=runtime_schema_hook,
            holder=holder,
            ensure_timeout=ensure_timeout,
        )
        # Clear the populate flag before reopening the gate (see FastMCP twin).
        set_populate_in_progress(False)
        set_db_disabled(False)
        payload["db_disabled"] = False
        return JSONResponse(payload, status_code=200)

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
