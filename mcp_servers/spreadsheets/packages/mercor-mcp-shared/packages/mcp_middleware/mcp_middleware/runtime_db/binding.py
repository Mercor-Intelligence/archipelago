"""Engine-binding facade — bind the runtime DB in place and open the engine.

One call from ``db.session``::

    binding = bind_engine(resolve_canonical_db_path())

binds the delivered ``.db`` **exactly where it lies** (the canonical /
receive path) and opens the live engine on it. There is no working-copy
move: the runtime file IS the canonical file. Two things still happen:

1. A **blank-world provenance** decision. When the canonical is absent at
   bind time the engine will create it lazily (the Studio "server boots
   before the world download" sequence); a sentinel beside the canonical
   (see ``AWAITING_DELIVERY_SUFFIX``) records that so a RESTART — which
   finds the lazily-created blank present — still recognises it as
   awaiting delivery rather than a real DB. ``adopt_late_delivery`` clears
   the sentinel once the world lands.
2. A **writability preflight** on the bound file whenever it exists. A
   read-only cross-uid bind 500s on the first write and 500-loops the
   readiness probe into a multi-minute silent hang; failing at bind time
   is instant and diagnosable.

Memory mode (``canonical=None`` / ``":memory:"``) skips both and returns a
transient in-memory engine.

The snapshot export directory is still resolved (``deliver_dir`` /
``MCP_SNAPSHOT_DIR``) and carried on the binding for snapshot-side callers;
it is the one placement knob that survives. Snapshot-side primitives take
their overrides from the binding: ``runtime=binding.runtime`` for the
snapshot facade (``== canonical`` in every file binding now) and the whole
binding for :func:`register_runtime_db_routes`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .paths import (
    RuntimePaths,
    clear_awaiting_delivery,
    is_awaiting_delivery,
    mark_awaiting_delivery,
)
from .probe import has_rows_in, has_user_rows
from .sync import RuntimeDbReadonlyError, _assert_runtime_writable
from .working import WorkingMode, resolve_deliver_dir

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = [
    "EngineBinding",
    "bind_engine",
    "log_binding",
]

logger = logging.getLogger(__name__)


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
            ``"sqlite:///state/studio.db"`` or ``"sqlite://"`` for memory
            mode).
        mode: Which :class:`WorkingMode` the binding ended up in —
            ``IN_PLACE`` for every file binding, ``MEMORY`` for a
            transient in-memory engine.
        canonical: The receive path the caller passed in (where the DB
            was delivered and — unless ``MCP_SNAPSHOT_DIR`` says
            otherwise — where the snapshot must be exported). ``None``
            for memory mode. It is the same physical file as ``runtime``.
        runtime: The working path the engine actually reads/writes.
            Equals ``canonical`` for the in-place binding; ``None`` for
            memory mode.
        deliver_dir: Where the snapshot export must be written
            (``MCP_SNAPSHOT_DIR`` or the receive path's directory).
            ``None`` for memory mode.
        paths: The full :class:`RuntimePaths` tuple (canonical + runtime
            + sidecar paths) for the file binding; ``None`` for memory
            mode. ``paths.runtime`` is the canonical itself.
        delivered: ``True`` when a real DB file was present at the receive
            path at bind time. ``False`` in the blank world (engine
            creates the file lazily) and in memory mode. Lifecycle callers
            use this as the pre-built-DB signal.
    """

    engine: Engine
    url: str
    mode: WorkingMode
    canonical: Path | None
    runtime: Path | None
    paths: RuntimePaths | None
    deliver_dir: Path | None = None
    delivered: bool = False

    @property
    def is_aliased(self) -> bool:
        """True when ``runtime`` IS ``canonical`` — every file binding now.

        Snapshot primitives that branch on the "runtime equals canonical"
        condition (e.g. in-place WAL folding instead of a copy-back) use
        this rather than re-resolving both paths. ``False`` only in memory
        mode (no file).
        """
        return self.mode is WorkingMode.IN_PLACE

    @property
    def awaiting_delivery(self) -> bool:
        """True for an in-place bind that has no real DB yet (blank world).

        The server bound the canonical **in place** but no delivered DB was
        present at bind time (``delivered=False``): SQLAlchemy will create the
        file lazily as a blank cold-seed, and a real world is expected to land
        at the canonical path afterwards (the Studio "server boots before the
        world download" sequence). Two consumers read it:

        * **Apps** gate ``create_all`` on ``not awaiting_delivery`` so they
          don't write an empty schema that a later in-place WAL fold would
          then ship over the pending delivery.
        * **persist** (:func:`persist_runtime_to_canonical`) refuses to fold a
          binding in this state for the same reason — the fold would clobber a
          world that has since landed at the canonical path.

        ``adopt_late_delivery`` flips ``delivered`` to ``True`` (and clears the
        blank-world sentinel) once the world arrives, after which this is
        ``False`` and a normal in-place fold resumes. Always ``False`` for
        memory mode.
        """
        return self.is_aliased and not self.delivered

    #: Backwards/alternative name for :attr:`awaiting_delivery` — reads better
    #: at call sites that phrase the check as "is this a blank in-place bind?".
    @property
    def is_blank_inplace(self) -> bool:
        """Alias of :attr:`awaiting_delivery` (in-place bind with no real DB)."""
        return self.awaiting_delivery


def bind_engine(
    canonical: str | os.PathLike[str] | None,
    *,
    deliver_dir: str | os.PathLike[str] | None = None,
    create_engine_kwargs: dict[str, Any] | None = None,
    attach_fts_sidecar: bool = True,
    create_fts_sidecar: bool = False,
    world_tables: Sequence[str] | None = None,
) -> EngineBinding:
    """Bind the runtime DB in place and open the engine.

    Args:
        canonical: The receive path — where the DB was delivered. Pass
            ``None`` or ``":memory:"`` for memory mode (transient test
            DB).
        deliver_dir: Override the snapshot-export directory (defaults to
            ``MCP_SNAPSHOT_DIR``, then the receive path's directory).
        create_engine_kwargs: Forwarded verbatim to
            :func:`sqlalchemy.create_engine`. ``future=True`` is added
            unless the caller passed their own ``future`` key.
        attach_fts_sidecar: Register an ATTACH-on-connect listener for the
            FTS sidecar beside the DB (``<db>.fts.db``) so every engine
            connection sees the ``fts.``-qualified FTS tables (see
            :mod:`mcp_middleware.fts_sidecar`). **Default True** and
            detection-only: it attaches solely if that file already exists
            beside the DB, so it is a pure no-op for an FTS-free app or one
            whose index is still inline in ``main`` (no ``.fts.db`` present).
            The populate facade always splits any index into a co-located
            sidecar (``migrate_inline_fts_to_sidecar``), so once a DB has an
            index a sidecar exists to attach — which is why serve-time attach
            needs no per-app flag. Pass False only to force inline-only
            resolution (e.g. a test asserting the un-attached path). No-op in
            memory mode.
        create_fts_sidecar: With ``attach_fts_sidecar``, ATTACH even when
            the sidecar file is absent (creating an empty sidecar) — the
            populate/build path that will then write the index into it.
            Default False (the serve path only wants an existing sidecar).
        world_tables: Optional set of *world* table names used by the
            stale-sentinel self-heal to decide whether a canonical carrying
            the blank-world sentinel is a genuine external delivery. When
            given, the heal probes ``has_rows_in(canonical, world_tables)``
            instead of the schema-agnostic ``has_user_rows`` — so a canonical
            a boot only scaffolded with **catalogue** rows (no world data) is
            NOT mistaken for a delivery and the awaiting-delivery sentinel
            stays set, letting the CSV import seed the world / default-user
            tables. Callers with a default-user identity pass the referenced
            table (``DefaultUserRef.ref_table``). ``None`` (default) keeps the
            legacy "≥1 row in any table" behaviour.

    Returns:
        :class:`EngineBinding` with the open engine + provenance. The
        caller is responsible for disposing ``binding.engine`` at
        shutdown.

    Raises:
        RuntimeDbReadonlyError: When the bound file exists but is not
            writable by this process (see the preflight note in the
            module docstring).

    Memory-mode notes:
        - In-memory SQLite engines lose their data when the last
          connection closes. ``bind_engine`` uses SQLAlchemy's default
          pool (``SingletonThreadPool`` for ``sqlite://`` URLs) — good
          enough for single-threaded tests but NOT shared across worker
          threads. Callers that need cross-thread sharing should pass
          ``create_engine_kwargs={"poolclass": StaticPool,
          "connect_args": {"check_same_thread": False}}``.
        - ``binding.canonical`` / ``runtime`` / ``paths`` /
          ``deliver_dir`` are all ``None``.
    """
    from sqlalchemy import create_engine  # deferred: keep module import cheap

    # ── Memory mode ────────────────────────────────────────────────────
    if canonical is None or os.fspath(canonical) == ":memory:":
        url = "sqlite://"
        engine = create_engine(url, **_merged_engine_kwargs(create_engine_kwargs))
        return EngineBinding(
            engine=engine,
            url=url,
            mode=WorkingMode.MEMORY,
            canonical=None,
            runtime=None,
            paths=None,
            deliver_dir=None,
        )

    canonical_path = Path(os.fspath(canonical)).expanduser().resolve()
    deliver = (
        Path(os.fspath(deliver_dir)).expanduser().resolve()
        if deliver_dir is not None
        else resolve_deliver_dir(canonical_path)
    )

    # ── Blank-world provenance ─────────────────────────────────────────
    # The runtime IS the canonical, so there is no move to stamp a
    # ``.srcmeta`` marker and a real delivery is indistinguishable from a
    # blank the engine lazily created on a prior boot by marker presence.
    # Use the inverse signal — a blank-world sentinel beside the canonical
    # (see ``AWAITING_DELIVERY_SUFFIX``):
    #   * canonical ABSENT → blank world; write the sentinel so a RESTART
    #     (which finds the lazily-created blank present) still recognises
    #     it as awaiting delivery rather than a real DB. ``delivered``
    #     stays False.
    #   * canonical PRESENT *with* the sentinel → a blank leftover from an
    #     earlier await-delivery boot; keep delivered=False so the
    #     enable_db late-delivery adopt stays armed and the real world wins
    #     when it lands.
    #   * canonical PRESENT *without* the sentinel → a genuine delivery;
    #     delivered stays True. This is the normal Foundry deploy.
    delivered = canonical_path.exists()
    if not canonical_path.exists():
        mark_awaiting_delivery(canonical_path)
        delivered = False
    elif is_awaiting_delivery(canonical_path):
        # Canonical present WITH the blank-world sentinel. Normally a blank
        # leftover the engine lazily created (delivered=False, adopt armed).
        # BUT an EXTERNAL delivery — Modal cold-seed / boot race that lands a
        # real world without going through ``adopt_late_delivery`` (the only
        # path that clears the sentinel) — leaves the sentinel STALE beside a
        # genuinely-populated DB. Under universal in-place (runtime==canonical)
        # ``delivered`` is the SOLE pre-built-DB signal the facade's import
        # auto-skip rests on, so a stale sentinel forcing delivered=False makes
        # the importer re-run CSVs over pre-built rows and PK-collide
        # (e.g. ``attachments.id`` UNIQUE). Probe the file: a populated
        # canonical means the sentinel is stale — treat as delivered and
        # self-heal by clearing it (so a later RESTART reconstructs cleanly);
        # a genuinely-blank canonical keeps delivered=False so cold-world
        # populate still imports.
        #
        # "Populated" is measured against ``world_tables`` when the caller
        # supplies them (e.g. the default-user ref table): a server-first boot
        # can scaffold the canonical with CATALOGUE-only rows before populate
        # re-binds, and those catalogue rows are NOT evidence of an external
        # delivery. Probing the world table avoids that false positive — an
        # unscoped ``has_user_rows`` would clear the sentinel, skip the CSV
        # import, and leave the default-user identity row dangling.
        populated = (
            has_rows_in(canonical_path, world_tables)
            if world_tables
            else has_user_rows(canonical_path)
        )
        if populated:
            clear_awaiting_delivery(canonical_path)
            delivered = True
            logger.info(
                "bind_engine: in-place canonical %s present WITH blank-world "
                "sentinel but is populated — stale sentinel from an external "
                "delivery; treating as delivered=True and clearing the sentinel",
                canonical_path,
            )
        else:
            delivered = False
            logger.info(
                "bind_engine: in-place canonical %s present but blank-world sentinel "
                "still set and DB is empty — treating as a blank leftover "
                "(delivered=False); late-delivery adopt stays armed",
                canonical_path,
            )

    # ── Writability preflight ──────────────────────────────────────────
    # A cross-uid delivered file opened read-only would otherwise accept
    # traffic and 500-loop the readiness probe on the first write (a
    # multi-minute silent hang). Probe the file whenever it exists.
    if canonical_path.exists():
        _preflight_writable(canonical_path, canonical_path, WorkingMode.IN_PLACE)

    paths = RuntimePaths(
        canonical=canonical_path,
        runtime=canonical_path,
        marker=canonical_path.with_name(canonical_path.name + ".srcmeta"),
        wal=canonical_path.with_name(canonical_path.name + "-wal"),
        shm=canonical_path.with_name(canonical_path.name + "-shm"),
    )
    url = f"sqlite:///{canonical_path}"
    engine = create_engine(url, **_merged_engine_kwargs(create_engine_kwargs))
    if attach_fts_sidecar:
        # Register BEFORE the caller opens any connection, so every pooled
        # connection carries the ATTACH. bind_engine never connects the engine
        # itself (the preflight probes the file directly), so this is safe here.
        from ..fts_sidecar import attach_sidecar_on_connect, sidecar_path_for

        attach_sidecar_on_connect(
            engine, sidecar_path_for(canonical_path), create_if_missing=create_fts_sidecar
        )
    return EngineBinding(
        engine=engine,
        url=url,
        mode=WorkingMode.IN_PLACE,
        canonical=canonical_path,
        runtime=canonical_path,
        paths=paths,
        deliver_dir=deliver,
        delivered=delivered,
    )


def log_binding(binding: EngineBinding, *, log: logging.Logger | None = None) -> None:
    """Emit one INFO line summarising ``binding``.

    Call this once at server startup so operators can grep a single line
    for "what bytes is this server reading?" without spelunking through
    the SQLAlchemy debug logs.

    By default the line is emitted through **loguru** — the logging stack
    every Foundry-* app actually ships to Datadog. The original stdlib
    emission was silently dropped by loguru-only apps (no stdlib handler
    installed), which is exactly how a mis-placed binding went invisible
    in production; a startup breadcrumb that vanishes in the deployed
    configuration is worse than none. Pass ``log=`` to route through a
    specific stdlib logger instead (apps on plain ``logging``).

    Format (single line, key=value):

        runtime_db: mode=<mode> url=<url> canonical=<path|-> runtime=<path|-> deliver=<path|->
    """
    fields = (
        binding.mode.value,
        binding.url,
        str(binding.canonical) if binding.canonical else "-",
        str(binding.runtime) if binding.runtime else "-",
        str(binding.deliver_dir) if binding.deliver_dir else "-",
    )
    if log is not None:
        log.info("runtime_db: mode=%s url=%s canonical=%s runtime=%s deliver=%s", *fields)
        return
    from loguru import logger as loguru_logger  # deferred: keep module import cheap

    loguru_logger.info("runtime_db: mode={} url={} canonical={} runtime={} deliver={}", *fields)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _describe_owner(path: Path) -> str:
    """Best-effort ``uid:gid mode`` string for a path, for diagnostics.

    Returns ``"?"`` if the path can't be stat'd (races / already gone). Never
    raises — this only decorates an error message.
    """
    try:
        st = path.stat()
    except OSError:
        return "?"
    return f"uid={st.st_uid} gid={st.st_gid} mode={oct(st.st_mode & 0o777)}"


def _preflight_writable(bound: Path, canonical: Path, mode: WorkingMode) -> None:
    """Fail loud if the file about to be bound is not writable by this process.

    A cross-uid delivered file opened read-only would otherwise accept
    traffic and 500-loop the readiness probe on the first write (a
    multi-minute silent hang). Raise :class:`RuntimeDbReadonlyError` with
    enough context to diagnose at a glance instead.
    """
    if _assert_runtime_writable(bound):
        return
    uid = os.getuid()
    raise RuntimeDbReadonlyError(
        "runtime DB is not writable by the serving process — refusing to bind "
        "(a read-only bind 500s on the first write and 500-loops the "
        "readiness probe into a multi-minute silent hang).\n"
        f"  mode:       {mode.value}\n"
        f"  bound:      {bound} [{_describe_owner(bound)}]\n"
        f"  canonical:  {canonical} [{_describe_owner(canonical)}]\n"
        f"  serving uid: {uid}\n"
        "  remediation: the delivered DB must be readable+writable by the "
        "serving uid. Check who delivered/owns the file and the mode it was "
        "delivered with."
    )


def _merged_engine_kwargs(extra: dict[str, Any] | None) -> dict[str, Any]:
    """Return ``create_engine`` kwargs with ``future=True`` defaulted.

    SQLAlchemy 2.0 default API is the "future" surface; the kwarg is
    still accepted for back-compat. We set it unless the caller already
    decided.
    """
    merged: dict[str, Any] = dict(extra or {})
    merged.setdefault("future", True)
    return merged
