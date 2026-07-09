"""Engine-binding facade — pick the right SQLite location for the live engine.

Three deployment shapes need to share the same server code:

1. **Runtime mode (production default).** ``DATABASE_PATH`` points at
   slow storage (EBS / NFS). Copy ``canonical → /tmp`` once at server
   cold start (via :func:`cold_seed_runtime`) and bind the engine to
   ``/tmp``. The agent task runs against tmpfs — random writes on gp2
   EBS are capped around 300 IOPS while tmpfs is pure RAM.

2. **Direct mode (dev / single-process).** Bind the engine directly to
   the canonical path; no ``/tmp`` copy. Useful when running locally
   against a checked-in fixture, or in tests where the canonical IS the
   runtime, or in container shapes whose canonical already lives on
   tmpfs. Selected by setting ``MCP_RUNTIME_DB_COPY=0`` (or
   ``false``/``no``) or passing ``force_copy=False``.

3. **Memory mode (transient tests).** ``:memory:`` SQLite — no file at
   all. Selected by passing ``canonical=None`` or ``canonical=":memory:"``.

The runtime/direct split exists because the snapshot machinery is built
around the runtime-mode invariant "canonical and runtime are different
files". A direct-mode caller must wire :func:`harvest_db_files`,
:func:`snapshot_db_only`, etc. with ``protect_paths`` / ``runtime``
overrides so they don't move/clobber the live DB. The
:class:`EngineBinding` returned here carries the resolved runtime path
so callers can hand it straight to those overrides — see the consumer
examples on :func:`bind_engine`.

Concretely, the four snapshot-side primitives accept the override values
this binding exposes:

* :func:`mcp_middleware.runtime_db.harvest_db_files` —
  ``protect_paths=[binding.runtime]`` (skip the live file at harvest).
* :func:`mcp_middleware.csv_engine.snapshot_db_via_runtime` —
  ``runtime=binding.runtime`` (in direct mode, equals canonical → routes
  through the in-place aliased branch).
* :func:`mcp_middleware.csv_engine.snapshot_with_populate` —
  ``runtime=binding.runtime`` (forwards through to the above and also
  guards step 0 + harvest).
* :func:`register_runtime_db_routes` — accepts the whole binding so
  the route handler can report the resolved path.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .paths import RuntimePaths
from .sync import cold_seed_runtime

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = [
    "BindingMode",
    "EngineBinding",
    "bind_engine",
    "log_binding",
]

logger = logging.getLogger(__name__)


# Environment variable consulted when ``force_copy`` is not passed
# explicitly. Falsy values ("0", "false", "no", case-insensitive) select
# direct mode; everything else (including unset) selects runtime mode.
# The default is **runtime mode** so production deploys are correct
# without any env wiring — direct mode is the opt-in dev convenience.
_ENV_COPY = "MCP_RUNTIME_DB_COPY"
_ENV_FALSY = frozenset({"0", "false", "no", "off"})


class BindingMode(StrEnum):
    """How the engine reaches its bytes.

    String-valued so it round-trips through JSON / env exporters /
    structured log fields without per-call conversion. The values match
    what :func:`log_binding` and the ``/_internal/db-path`` route emit.
    """

    RUNTIME = "runtime"
    """Canonical is copied to ``/tmp`` (tmpfs); engine reads the copy."""

    DIRECT = "direct"
    """Engine reads the canonical file in place (no copy)."""

    MEMORY = "memory"
    """In-memory SQLite (``:memory:``); no file involved."""


@dataclass(frozen=True)
class EngineBinding:
    """Resolved engine + provenance for ``bind_engine``.

    The dataclass is the single source of truth a calling server hands
    around: the ``engine`` for SQLAlchemy use, the ``url`` for logs /
    debugging, the ``mode`` so per-mode code paths can branch, and the
    file paths so snapshot/harvest callers can pass them as overrides.

    Attributes:
        engine: The :class:`sqlalchemy.Engine`. Caller owns disposal.
        url: The fully-resolved URL the engine was opened with (e.g.
            ``"sqlite:///tmp/workspace_runtime_abc123.db"`` or
            ``"sqlite://"`` for memory mode).
        mode: Which :class:`BindingMode` the binding ended up in.
        canonical: The original canonical path passed in by the caller,
            ``None`` for memory mode. Always populated for file modes —
            for direct mode it's the same physical path as ``runtime``.
        runtime: The path the engine actually reads from. For runtime
            mode this is the hashed ``/tmp`` copy; for direct mode it
            equals ``canonical``; for memory mode it's ``None``.
        paths: The full :class:`RuntimePaths` tuple (canonical + runtime
            + sidecar paths). Populated for both file modes — in direct
            mode ``paths.runtime`` is the canonical itself (the
            ``RuntimePaths`` is synthesised rather than computed via
            :func:`runtime_paths_for`). ``None`` for memory mode.
    """

    engine: Engine
    url: str
    mode: BindingMode
    canonical: Path | None
    runtime: Path | None
    paths: RuntimePaths | None

    @property
    def is_aliased(self) -> bool:
        """Convenience: True when ``runtime`` IS ``canonical`` (direct mode).

        Snapshot primitives that branch on the "runtime equals canonical"
        condition (e.g. :func:`snapshot_db_only`'s in-place mode) can use
        this rather than re-resolving both paths.
        """
        return self.mode is BindingMode.DIRECT


def bind_engine(
    canonical: str | os.PathLike[str] | None,
    *,
    force_copy: bool | None = None,
    create_engine_kwargs: dict[str, Any] | None = None,
) -> EngineBinding:
    """Build the engine + binding for ``canonical``.

    Args:
        canonical: Slow-storage DB path. Pass ``None`` or ``":memory:"``
            for memory mode (transient test DB).
        force_copy: Override the env var.

            * ``True`` — force runtime mode (cold-seed canonical → /tmp,
              bind engine to /tmp). ``ValueError`` if combined with
              memory mode (``canonical`` is ``None`` / ``":memory:"``).
            * ``False`` — force direct mode (bind engine to canonical
              in place, no copy).
            * ``None`` (default) — consult ``MCP_RUNTIME_DB_COPY``. Set
              to ``"0"`` / ``"false"`` / ``"no"`` / ``"off"`` (case-
              insensitive) selects direct mode; unset or any other
              value selects runtime mode.
        create_engine_kwargs: Forwarded verbatim to
            :func:`sqlalchemy.create_engine`. Use this for ``echo=True``,
            custom pool classes, etc. ``future=True`` is added unless
            the caller passed their own ``future`` key.

    Returns:
        :class:`EngineBinding` with the open engine + provenance. The
        caller is responsible for disposing ``binding.engine`` at
        shutdown (typically via an atexit hook or shutdown route).

    Raises:
        ValueError: When ``force_copy=True`` is combined with memory
            mode — there's no file to copy from.

    Memory mode notes:
        - In-memory SQLite engines lose their data when the last
          connection closes. ``bind_engine`` uses SQLAlchemy's default
          pool (``SingletonThreadPool`` for ``sqlite://`` URLs), which
          keeps a single connection per thread alive — good enough for
          single-threaded tests but NOT shared across worker threads.
          Callers that need cross-thread sharing should pass
          ``create_engine_kwargs={"poolclass": StaticPool,
          "connect_args": {"check_same_thread": False}}``.
        - There's no canonical path to record, so ``binding.canonical``,
          ``binding.runtime`` and ``binding.paths`` are all ``None``.

    Direct-mode example::

        # Single-process / dev: MCP_RUNTIME_DB_COPY=0 or force_copy=False
        binding = bind_engine("/data/workspace.db", force_copy=False)
        assert binding.mode is BindingMode.DIRECT
        assert binding.runtime == binding.canonical  # same physical file

        # Snapshot still works because the primitives accept overrides:
        snapshot_with_populate(
            state_dir="/data",
            canonical="/data/workspace.db",
            runtime=binding.runtime,  # protects + routes through aliased branch
        )

    Runtime-mode example::

        # Production default
        binding = bind_engine("/mnt/state/workspace.db")
        assert binding.mode is BindingMode.RUNTIME
        assert binding.runtime != binding.canonical
        assert "/tmp/" in str(binding.runtime)  # tmpfs
    """
    # Defer the heavy import so importing this module is cheap (the
    # FZ migration shape calls bind_engine from db.session import time
    # in the consumer).
    from sqlalchemy import create_engine

    # ── Memory mode ────────────────────────────────────────────────────
    # Treat None and ":memory:" identically — both produce a transient
    # in-memory engine with no file backing.
    if canonical is None or os.fspath(canonical) == ":memory:":
        if force_copy is True:
            raise ValueError(
                "bind_engine: force_copy=True is incompatible with memory "
                "mode (canonical is None or ':memory:'); there is no file "
                "to copy from. Pass an explicit canonical path or omit "
                "force_copy to use the env-driven default."
            )
        kwargs = _merged_engine_kwargs(create_engine_kwargs)
        url = "sqlite://"
        engine = create_engine(url, **kwargs)
        return EngineBinding(
            engine=engine,
            url=url,
            mode=BindingMode.MEMORY,
            canonical=None,
            runtime=None,
            paths=None,
        )

    # ── File mode: pick runtime vs direct via env / override ──────────
    use_copy = _resolve_use_copy(force_copy)
    canonical_path = Path(os.fspath(canonical)).expanduser().resolve()

    if use_copy:
        # Runtime mode: cold-seed canonical→/tmp (idempotent) and bind
        # the engine to the /tmp path. cold_seed handles missing
        # canonicals (blank world) and stale sidecars internally.
        paths = cold_seed_runtime(canonical_path)
        runtime_path = paths.runtime
        url = f"sqlite:///{runtime_path}"
        engine = create_engine(url, **_merged_engine_kwargs(create_engine_kwargs))
        return EngineBinding(
            engine=engine,
            url=url,
            mode=BindingMode.RUNTIME,
            canonical=canonical_path,
            runtime=runtime_path,
            paths=paths,
        )

    # Direct mode: bind to canonical in place. We still synthesise a
    # RuntimePaths so consumers that expect ``binding.paths`` (e.g. for
    # marker / WAL sidecar locations) don't need to special-case the
    # mode. The hashed runtime path from runtime_paths_for() is NOT
    # used — direct-mode bindings explicitly bypass /tmp.
    aliased_paths = RuntimePaths(
        canonical=canonical_path,
        runtime=canonical_path,
        marker=canonical_path.with_name(canonical_path.name + ".srcmeta"),
        wal=canonical_path.with_name(canonical_path.name + "-wal"),
        shm=canonical_path.with_name(canonical_path.name + "-shm"),
    )
    url = f"sqlite:///{canonical_path}"
    engine = create_engine(url, **_merged_engine_kwargs(create_engine_kwargs))
    return EngineBinding(
        engine=engine,
        url=url,
        mode=BindingMode.DIRECT,
        canonical=canonical_path,
        runtime=canonical_path,
        paths=aliased_paths,
    )


def log_binding(binding: EngineBinding, *, log: logging.Logger | None = None) -> None:
    """Emit one INFO line summarising ``binding``.

    Call this once at server startup so operators can grep a single line
    for "what bytes is this server reading?" without spelunking through
    the SQLAlchemy debug logs.

    Format (single line, key=value):

        runtime_db: mode=<mode> url=<url> canonical=<path|-> runtime=<path|->

    Args:
        binding: The :class:`EngineBinding` to describe.
        log: Optional logger to write to. Defaults to this module's
            logger so the line appears under
            ``mcp_middleware.runtime_db.binding`` — convenient for
            log-routing by module name.
    """
    target = log or logger
    canonical_str = str(binding.canonical) if binding.canonical else "-"
    runtime_str = str(binding.runtime) if binding.runtime else "-"
    target.info(
        "runtime_db: mode=%s url=%s canonical=%s runtime=%s",
        binding.mode.value,
        binding.url,
        canonical_str,
        runtime_str,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_use_copy(force_copy: bool | None) -> bool:
    """Decide whether to use runtime mode (True) or direct mode (False).

    Precedence:
    1. ``force_copy`` (explicit caller override) wins outright.
    2. ``MCP_RUNTIME_DB_COPY`` env var: falsy ("0"/"false"/"no"/"off",
       case-insensitive) → direct mode (return False).
    3. Default → runtime mode (return True).
    """
    if force_copy is not None:
        return force_copy
    raw = os.environ.get(_ENV_COPY)
    if raw is None:
        return True
    return raw.strip().lower() not in _ENV_FALSY


def _merged_engine_kwargs(extra: dict[str, Any] | None) -> dict[str, Any]:
    """Return ``create_engine`` kwargs with ``future=True`` defaulted.

    SQLAlchemy 2.0 default API is the "future" surface; the kwarg is
    still accepted for back-compat. We set it unless the caller already
    decided.
    """
    merged: dict[str, Any] = dict(extra or {})
    merged.setdefault("future", True)
    return merged
