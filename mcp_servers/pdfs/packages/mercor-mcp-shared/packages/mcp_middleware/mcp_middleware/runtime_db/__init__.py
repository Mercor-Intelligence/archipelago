"""Runtime DB sync for SQLite-backed MCP servers.

Apps whose ``DATABASE_PATH`` points at slow storage (EBS / NFS) get a big
hot-loop win from running SQLite off tmpfs: random writes on gp2 EBS are
capped around 300 IOPS while tmpfs is pure RAM. This package provides
the primitives needed to safely manage the two-location pattern
(canonical-on-EBS, runtime-on-tmpfs):

* :func:`bind_engine` — **the recommended entry point.** One call from
  ``db.session`` returns an :class:`EngineBinding` carrying the live
  engine plus the resolved canonical / runtime / deliver paths. Binding
  is always **in place**: the runtime file IS the canonical file (the
  working-location ladder that used to move RAM-fitting DBs into per-uid
  tmpfs is gone; see :mod:`.working`). The only surviving placement knob
  is the snapshot ``MCP_SNAPSHOT_DIR`` deliver dir. Memory mode via
  ``canonical=None`` / ``":memory:"``.
* :func:`cold_seed_runtime` — lower-level primitive: copy canonical →
  runtime exactly once when the runtime is absent. Idempotent. In-place
  bindings never call it (runtime IS canonical); use this directly only
  if you're managing a separate runtime path yourself.
* :func:`fully_indexed` — read-only probe: does this DB already have
  every FTS shadow populated? Used by the populate-skip guard.
* :func:`harvest_db_files` — populate-side pre-step: move every ``.db``
  in ``state_dir`` (plus WAL / SHM / srcmeta sidecars) to ``/tmp`` so
  pre-built DBs become runtime DBs AND ``state_dir`` is left clean for
  the next snapshot to write into. Accepts ``protect_paths=`` so a
  direct-mode binding's canonical isn't moved out from under the engine.
* :func:`register_runtime_db_routes` — mount the shared
  ``/_internal/checkpoint``, ``/_internal/db-path``,
  ``/_internal/disable_db`` and ``/_internal/enable_db`` routes on a
  FastMCP instance (or Starlette / FastAPI app). Called automatically
  by :func:`mcp_middleware.run_server` when an engine is provided.
* :func:`run_wal_checkpoint` — the in-process primitive the route is
  built on; safe to call directly for tests or for apps that want
  custom routing.
* :class:`DbGateMiddleware` — Starlette middleware that 503's app
  traffic while the process-global DB gate is closed (whitelisted
  paths like ``/health`` and the ``/_internal/*`` lifecycle routes
  pass through). Register it FIRST (outermost) so it runs ahead of
  any DB-touching middleware.
* :func:`is_db_disabled` / :func:`set_db_disabled` — the primitives
  the middleware reads / writes. The disable/enable HTTP routes
  toggle these, but tests and in-process callers can flip them
  directly. See :data:`DEFAULT_WHITELIST` for the paths that stay
  reachable while the gate is closed.
* :func:`log_binding` — emit a one-line INFO summary of an
  :class:`EngineBinding` at startup so operators can see at a glance
  which mode the server picked.
"""

from __future__ import annotations

from .binding import (
    EngineBinding,
    bind_engine,
    log_binding,
)
from .canonical import (
    Canonical,
    CanonicalPath,
    MemoryMode,
    resolve_canonical_db_path,
)
from .checkpoint import (
    CheckpointResult,
    DbPathInfo,
    PersistResult,
    adopt_late_delivery,
    persist_runtime_to_canonical,
    register_runtime_db_routes,
    run_wal_checkpoint,
)
from .cli import fully_indexed_cli
from .db_gate import (
    DEFAULT_WHITELIST,
    DbGateMiddleware,
    is_db_disabled,
    is_populate_in_progress,
    set_db_disabled,
    set_populate_in_progress,
)
from .ensure import DEFAULT_ENSURE_TIMEOUT, ensure_db_ready
from .harvest import harvest_db_files
from .health import DEFAULT_HEALTH_PATH, register_health_route
from .identity_seed import DEFAULT_IDENTITY_TABLE, seed_fallback_default_user
from .lifecycle import (
    MidBootServerError,
    PersistOutcome,
    checkpoint_url_for_port,
    close_db_gate_or_refuse,
    detect_live_server_process,
    disable_db_url_for_port,
    drain_server_pool,
    enable_db_url_for_port,
    estimated_post_import_bytes,
    handoff_runtime_to_server,
    internal_base_url_for_port,
    persist_server_runtime,
    persist_url_for_port,
    server_base_from_locator,
    set_server_db_gate,
    unlink_stale_runtime,
)
from .paths import (
    RuntimePaths,
    awaiting_delivery_marker,
    clear_awaiting_delivery,
    ensure_private_dir,
    fingerprint_canonical,
    is_awaiting_delivery,
    mark_awaiting_delivery,
    per_uid_dir,
    read_marker,
    runtime_paths_for,
    runtime_paths_in,
    write_marker,
)
from .populate_route import (
    PopulateCompletedResponse,
    PopulateStartedResponse,
    handle_populate_request,
)
from .probe import fully_indexed, has_rows_in, has_user_rows
from .ready import (
    DbLifecycleSpec,
    DbNotReadyError,
    DbReadyState,
    EngineHolder,
    engine_holder,
    lifecycle_spec,
)
from .register import background_ensure, register_db_lifecycle
from .sync import (
    RuntimeDbReadonlyError,
    cold_seed_runtime,
)
from .working import (
    WorkingMode,
    resolve_deliver_dir,
)

__all__ = [
    # engine-binding facade (one-stop shop for "give me the live engine")
    "EngineBinding",
    "bind_engine",
    "log_binding",
    # checkpoint primitive + HTTP routes
    "CheckpointResult",
    "DbPathInfo",
    "PersistResult",
    "adopt_late_delivery",
    "persist_runtime_to_canonical",
    "register_runtime_db_routes",
    "run_wal_checkpoint",
    # CLI helper
    "fully_indexed_cli",
    # Typed canonical-path resolver (sum type makes ":memory:" corruption unrepresentable)
    "Canonical",
    "CanonicalPath",
    "MemoryMode",
    "resolve_canonical_db_path",
    # HTTP-layer DB gate (process-global flag + Starlette middleware)
    "DEFAULT_WHITELIST",
    "DbGateMiddleware",
    "is_db_disabled",
    "is_populate_in_progress",
    "set_db_disabled",
    "set_populate_in_progress",
    # state_dir → /tmp harvester (populate pre-step)
    "harvest_db_files",
    # canonical shared /health liveness route (auto-mounted by run_server)
    "DEFAULT_HEALTH_PATH",
    "register_health_route",
    # populate-side lifecycle primitives (drain / persist / gate / unlink / handoff / estimate)
    "MidBootServerError",
    "PersistOutcome",
    "checkpoint_url_for_port",
    "close_db_gate_or_refuse",
    "detect_live_server_process",
    "disable_db_url_for_port",
    "drain_server_pool",
    "enable_db_url_for_port",
    "estimated_post_import_bytes",
    "handoff_runtime_to_server",
    "internal_base_url_for_port",
    "persist_server_runtime",
    "persist_url_for_port",
    "server_base_from_locator",
    "set_server_db_gate",
    "unlink_stale_runtime",
    # path primitives (advanced; most callers don't need these)
    "RuntimePaths",
    "awaiting_delivery_marker",
    "clear_awaiting_delivery",
    "ensure_private_dir",
    "fingerprint_canonical",
    "is_awaiting_delivery",
    "mark_awaiting_delivery",
    "per_uid_dir",
    "read_marker",
    "runtime_paths_for",
    "runtime_paths_in",
    "write_marker",
    # working-mode enum + snapshot deliver-dir resolver
    "WorkingMode",
    "resolve_deliver_dir",
    # index probe
    "fully_indexed",
    "has_rows_in",
    "has_user_rows",
    # lazy readiness state machine (no-proactive-bind lifecycle)
    "DbLifecycleSpec",
    "DbNotReadyError",
    "DbReadyState",
    "EngineHolder",
    "engine_holder",
    "lifecycle_spec",
    # bounded synchronous ensure (bind-on-demand driver)
    "DEFAULT_ENSURE_TIMEOUT",
    "ensure_db_ready",
    # blank-world fallback default-user seed (D4)
    "DEFAULT_IDENTITY_TABLE",
    "seed_fallback_default_user",
    # lifecycle wiring (both-arm triggers)
    "background_ensure",
    "register_db_lifecycle",
    # populate route (opt-in via register_runtime_db_routes(populate_working_dir=...))
    "PopulateCompletedResponse",
    "PopulateStartedResponse",
    "handle_populate_request",
    # sync surface
    "RuntimeDbReadonlyError",
    "cold_seed_runtime",
]
