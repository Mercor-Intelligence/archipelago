"""Runtime DB sync for SQLite-backed MCP servers.

Apps whose ``DATABASE_PATH`` points at slow storage (EBS / NFS) get a big
hot-loop win from running SQLite off tmpfs: random writes on gp2 EBS are
capped around 300 IOPS while tmpfs is pure RAM. This package provides
the primitives needed to safely manage the two-location pattern
(canonical-on-EBS, runtime-on-tmpfs):

* :func:`bind_engine` — **the recommended entry point.** One call from
  ``db.session`` returns an :class:`EngineBinding` carrying the live
  engine plus the resolved canonical / runtime paths. Picks between
  runtime mode (canonical → /tmp copy), direct mode (bind to canonical
  in place), and memory mode (``:memory:``) via the
  ``MCP_RUNTIME_DB_COPY`` env var or an explicit ``force_copy=`` kwarg.
* :func:`cold_seed_runtime` — lower-level primitive used by
  ``bind_engine`` in runtime mode: copy canonical → runtime exactly
  once when the runtime is absent. Idempotent. Use this directly only
  if you're managing the engine yourself.
* :func:`refresh_runtime_from_canonical` — out-of-process worker (populate
  / sync / backup) refresh: checkpoint the live server, then copy
  canonical → runtime if the canonical's fingerprint changed. Refuses
  to copy when the checkpoint never clears (would corrupt the live DB).
* :func:`resolve_runtime_path` — read-only path lookup. Raises
  :class:`RuntimeDbMissingError` if the runtime hasn't been seeded yet.
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

The three sync APIs are split by *process role*, not by capability —
calling the wrong one in the wrong place can clobber an active server's
runtime DB. See the docstrings on each function for the contract.
"""

from __future__ import annotations

from .binding import (
    BindingMode,
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
    register_runtime_db_routes,
    run_wal_checkpoint,
)
from .cli import fully_indexed_cli
from .db_gate import (
    DEFAULT_WHITELIST,
    DbGateMiddleware,
    is_db_disabled,
    set_db_disabled,
)
from .harvest import harvest_db_files
from .paths import (
    RuntimePaths,
    fingerprint_canonical,
    read_marker,
    runtime_paths_for,
    write_marker,
)
from .populate_route import (
    PopulateCompletedResponse,
    PopulateStartedResponse,
    handle_populate_request,
)
from .probe import fully_indexed
from .sync import (
    RefreshOutcome,
    RefreshResult,
    RuntimeDbMissingError,
    cold_seed_runtime,
    refresh_runtime_from_canonical,
    resolve_runtime_path,
)

__all__ = [
    # engine-binding facade (one-stop shop for "give me the live engine")
    "BindingMode",
    "EngineBinding",
    "bind_engine",
    "log_binding",
    # checkpoint primitive + HTTP routes
    "CheckpointResult",
    "DbPathInfo",
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
    "set_db_disabled",
    # state_dir → /tmp harvester (populate pre-step)
    "harvest_db_files",
    # path primitives (advanced; most callers don't need these)
    "RuntimePaths",
    "fingerprint_canonical",
    "read_marker",
    "runtime_paths_for",
    "write_marker",
    # index probe
    "fully_indexed",
    # populate route (opt-in via register_runtime_db_routes(populate_working_dir=...))
    "PopulateCompletedResponse",
    "PopulateStartedResponse",
    "handle_populate_request",
    # sync surface
    "RefreshOutcome",
    "RefreshResult",
    "RuntimeDbMissingError",
    "cold_seed_runtime",
    "refresh_runtime_from_canonical",
    "resolve_runtime_path",
]
