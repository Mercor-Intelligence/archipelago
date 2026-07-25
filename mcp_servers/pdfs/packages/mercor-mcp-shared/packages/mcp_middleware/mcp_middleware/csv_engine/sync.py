"""Synchronous facade over the async csv_engine import/export functions.

Several consuming apps (e.g. Foundry-Zoho) are synchronous — they use a
SQLAlchemy ``Session`` / ``Engine`` and never run an event loop. These wrappers
let them call the engine without writing ``async``/``await``:

    from mcp_middleware.csv_engine import import_from_zip_sync
    import_from_zip_sync(zip_bytes, "sqlite:///data.db", config)

Each wrapper accepts a **database URL string**, a sync ``Engine``, or an
``AsyncEngine``. For a URL or sync Engine it builds an ``AsyncEngine`` (mapping
the driver to its async variant — ``sqlite`` -> ``aiosqlite``, ``postgresql``
-> ``asyncpg``, ``mysql`` -> ``aiomysql``), runs the operation, and disposes it,
all on a single private event loop so connections stay loop-bound. An
``AsyncEngine`` passed in is used as-is and left open (the caller owns it).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import Engine
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .exporter import (
    export_snapshot,
    export_snapshot_zip,
    export_with_directives,
    snapshot_directory,
)
from .importer import import_csv_entity, import_directory, import_from_zip, import_multi_csv

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from pathlib import Path
    from typing import Any

    from sqlalchemy.engine import URL

    from mcp_middleware.runner import DefaultUserRef

    from .config import ImportDirectives, SnapshotConfig

    DbSource = str | URL | Engine | AsyncEngine

__all__ = [
    "SnapshotHookResult",
    "export_snapshot_sync",
    "export_snapshot_zip_sync",
    "export_with_directives_sync",
    "import_csv_entity_sync",
    "import_directory_sync",
    "import_from_zip_sync",
    "import_multi_csv_sync",
    "prune_imported_files",
    "snapshot_db_only",
    "snapshot_db_via_runtime",
    "snapshot_directory_sync",
    "snapshot_with_populate",
]

logger = logging.getLogger(__name__)

# Map a sync backend to its async driver.
_ASYNC_DRIVERS = {
    "sqlite": "sqlite+aiosqlite",
    "postgresql": "postgresql+asyncpg",
    "mysql": "mysql+aiomysql",
}


def _to_async_url(url: str | URL) -> URL:
    """Map a database URL to its async-driver equivalent."""
    parsed = make_url(str(url))
    driver = _ASYNC_DRIVERS.get(parsed.get_backend_name())
    if driver:
        return parsed.set(drivername=driver)
    return parsed


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run a coroutine to completion, even if called from within a loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Already inside an event loop: run on a dedicated thread with its own loop.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


def _resolve_engine(db: DbSource) -> tuple[AsyncEngine, bool]:
    """Return (async_engine, created) for a URL / sync Engine / AsyncEngine.

    ``created`` is True when this call built the engine (and must dispose it).
    """
    if isinstance(db, AsyncEngine):
        return db, False
    url = db.url if isinstance(db, Engine) else db
    return create_async_engine(_to_async_url(url)), True


def import_from_zip_sync(
    file_data: bytes,
    db: DbSource,
    config: SnapshotConfig | None = None,
) -> dict[str, int]:
    """Synchronous :func:`import_from_zip`."""

    async def _op() -> dict[str, int]:
        engine, created = _resolve_engine(db)
        try:
            return await import_from_zip(file_data, engine, config)
        finally:
            if created:
                await engine.dispose()

    return _run(_op())


def import_directory_sync(
    csv_dir: str | Path,
    db: DbSource,
    config: SnapshotConfig,
    *,
    confirm_clear: bool = False,
) -> dict[str, int]:
    """Synchronous :func:`import_directory` (seed a DB from a CSV directory)."""
    from pathlib import Path as _Path

    async def _op() -> dict[str, int]:
        engine, created = _resolve_engine(db)
        try:
            return await import_directory(
                _Path(csv_dir), engine, config, confirm_clear=confirm_clear
            )
        finally:
            if created:
                await engine.dispose()

    return _run(_op())


def import_csv_entity_sync(
    csv_text: str,
    table_name: str,
    db: DbSource,
    config: SnapshotConfig | None = None,
) -> dict[str, int]:
    """Synchronous :func:`import_csv_entity`."""

    async def _op() -> dict[str, int]:
        engine, created = _resolve_engine(db)
        try:
            return await import_csv_entity(csv_text, table_name, engine, config)
        finally:
            if created:
                await engine.dispose()

    return _run(_op())


def import_multi_csv_sync(
    csv_text: str,
    db: DbSource,
    config: SnapshotConfig | None = None,
) -> dict[str, int]:
    """Synchronous :func:`import_multi_csv`."""

    async def _op() -> dict[str, int]:
        engine, created = _resolve_engine(db)
        try:
            return await import_multi_csv(csv_text, engine, config)
        finally:
            if created:
                await engine.dispose()

    return _run(_op())


def export_with_directives_sync(
    db: DbSource,
    table: str,
    directives: ImportDirectives,
    *,
    wide_columns: list[str] | None = None,
    include_headers: bool = True,
    where_by_constants: bool = True,
) -> str:
    """Synchronous :func:`export_with_directives`."""

    async def _op() -> str:
        engine, created = _resolve_engine(db)
        try:
            return await export_with_directives(
                engine,
                table,
                directives,
                wide_columns=wide_columns,
                include_headers=include_headers,
                where_by_constants=where_by_constants,
            )
        finally:
            if created:
                await engine.dispose()

    return _run(_op())


def snapshot_directory_sync(
    output_dir: str | Path,
    db: DbSource,
    config: SnapshotConfig,
    *,
    entities: list[str] | None = None,
) -> dict[str, int]:
    """Synchronous :func:`snapshot_directory`.

    Inverse of :func:`import_directory_sync`: writes each directive-driven
    entity back to ``output_dir / <files[0]>`` using the same config.

    Note:
        This is the directive-driven snapshot path. Entities without
        ``import_config.directives`` are **silently skipped** — their wide
        CSV cannot be reconstructed from the EAV / fan-out shapes
        ``transform_with_directives`` writes. Apps whose entities are flat
        (no ``directives:`` block — typed-per-domain table shapes such as
        Drive / Gmail / Docs / etc.) should use :func:`export_snapshot_sync`
        instead, which auto-resolves columns from the SQLAlchemy schema
        and emits one CSV per entity using the entity's
        :class:`~mcp_middleware.csv_engine.config.ExportConfig`.
    """
    from pathlib import Path as _Path

    async def _op() -> dict[str, int]:
        engine, created = _resolve_engine(db)
        try:
            return await snapshot_directory(
                engine,
                config,
                _Path(output_dir),
                entities=entities,
            )
        finally:
            if created:
                await engine.dispose()

    return _run(_op())


def export_snapshot_sync(
    output_dir: str | Path,
    db: DbSource,
    config: SnapshotConfig,
    *,
    entities: list[str] | None = None,
) -> dict[str, int]:
    """Synchronous :func:`export_snapshot`.

    Schema-driven snapshot: for each entity in ``config`` with an
    ``export:`` block (or the implicit default), auto-resolve columns
    from the SQLAlchemy schema and write one CSV per entity to
    ``output_dir / <name>.csv``. Use this for apps whose tables are
    flat / strongly-typed per domain (Drive / Gmail / Docs / …) — every
    column on the table becomes a CSV column.

    The complement of :func:`import_directory_sync` for flat-mode
    entities (no ``directives:`` block). For directive-driven entities
    (Zoho-style EAV fan-out), :func:`snapshot_directory_sync` is the
    correct inverse.

    Args:
        output_dir: Destination directory; created if missing.
        db: Database URL, sync ``Engine``, or ``AsyncEngine``.
        config: Snapshot configuration (same one used at import time).
        entities: Optional subset of entity names. ``None`` = every
            entity with an ``export:`` block.

    Returns:
        Mapping of entity name -> number of rows written.
    """
    from pathlib import Path as _Path

    async def _op() -> dict[str, int]:
        engine, created = _resolve_engine(db)
        try:
            return await export_snapshot(
                engine,
                config,
                _Path(output_dir),
                entities=entities,
            )
        finally:
            if created:
                await engine.dispose()

    return _run(_op())


def snapshot_db_only(
    dst: str | Path,
    db: DbSource,
    *,
    drop_tables: list[str] | None = None,
    drop_virtual_tables: bool = True,
) -> Path:
    """Produce a clean SQLite snapshot of ``db`` at ``dst``.

    This is the **DB-only** complement of :func:`export_snapshot_sync` —
    no CSVs, no entity iteration. Use when the consumer wants a raw
    ``.db`` file (e.g. the Studio grader extracts the snapshot archive
    and opens ``workspace.db`` directly), separate from any CSV export
    flow that may also run on the same engine.

    How it works:

    1. **Checkpoint the engine's WAL** so the source DB on disk reflects
       every committed frame, then close idle connections. This is the
       same primitive :func:`mcp_middleware.runtime_db.run_wal_checkpoint`
       runs; we do it inline here because the consumer that asked for
       the snapshot is usually the same process that owns the engine.
    2. **Copy via the SQLite online-backup API** (``conn.backup()``).
       ``shutil.copy2`` of the main ``.db`` file alone silently omits
       frames still in ``-wal``; the online backup API reads a
       consistent snapshot that includes every committed WAL frame
       regardless of checkpoint state.
    3. **Switch the destination out of WAL mode**
       (``PRAGMA journal_mode=DELETE``). Backup propagates the source's
       journal mode, so a WAL-mode source produces a WAL-mode
       destination — which would leave ``-wal`` / ``-shm`` sidecars next
       to the snapshot, defeating the "snapshot is just ``workspace.db``"
       contract.
    4. **Drop computed tables** on the *destination* copy. By default
       every virtual table in ``sqlite_master`` is dropped (FTS5 / vec0)
       so the snapshot ships only the agent-relevant ORM tables. Apps
       with additional regular computed tables (``docvec_documents``,
       ``docvec_chunks``, etc.) pass them via ``drop_tables=[...]``.
       Drop failures raise — silently skipping (the old behaviour) lets
       indexes survive into the snapshot without anyone noticing.
    5. **VACUUM** the destination so the pages freed by step 4 are
       actually reclaimed from the file. SQLite's ``DROP TABLE`` (and
       virtual-table ``xDestroy``) marks pages free in the page-map but
       leaves the file the same size on disk — without VACUUM, a 12 GB
       source with a 6 GB FTS5 index still ships as a 12 GB snapshot
       even though the index tables are gone from ``sqlite_master``.
       Skipped when no drops were requested (no freelist to reclaim).
    6. **Defensive sidecar cleanup**: after every connection closes, any
       lingering ``-wal`` / ``-shm`` / ``-journal`` files next to ``dst``
       are unlinked. By step 3 + step 5 there shouldn't be any, but
       defence in depth keeps the snapshot contract airtight.

    Args:
        dst: Destination path. Parent directory is created if missing.
        db: Database source — URL, sync ``Engine``, or ``AsyncEngine``.
            Only sync ``Engine`` benefits from the pool drain + checkpoint
            optimisation; the URL / AsyncEngine paths skip that step
            (no live pool to drain).
        drop_tables: Optional list of regular table names to drop on
            the destination copy. Each runs via ``DROP TABLE IF EXISTS``
            so missing tables are silently skipped. Drop failures on
            existing tables raise.
        drop_virtual_tables: **Default ``True``.** Drops every virtual
            table in the destination's ``sqlite_master`` via
            ``DROP TABLE`` (invokes ``xDestroy`` to clean up shadow
            B-trees). Triggers whose SQL body references a dropped
            virtual table are dropped too — they live as independent
            ``sqlite_master`` objects and are NOT cleaned up by
            ``xDestroy``. Pass ``False`` only when you genuinely want
            to ship FTS5 / vec0 indexes in the snapshot (rare;
            re-indexing on import is normally cheap and avoids the
            sqlite-vec / sqlite-fts5 dependency on the consumer).

    Returns:
        The absolute :class:`Path` to the produced ``.db`` file.

    Raises:
        ValueError: If ``db`` doesn't resolve to a SQLite file path.
        RuntimeError: If any virtual-table drop fails on the
            destination — typically when a ``vec0`` table is present
            but ``sqlite-vec`` can't be loaded (extension loading
            disabled in this SQLite build, or the package isn't
            installed). Silently shipping the index is worse than
            failing the snapshot.
    """
    import sqlite3
    from pathlib import Path as _Path

    dst_path = _Path(dst).expanduser().resolve()
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    # Step 1 + 2: checkpoint the source's WAL on its own engine, then
    # use sqlite3 online backup for a consistent copy.
    src_db_path = _source_db_path(db)
    if src_db_path is None:
        raise ValueError(
            f"snapshot_db_only: source {db!r} does not resolve to a SQLite "
            "file path (only file-backed sqlite is supported)"
        )

    # Aliased mode: dst path resolves to the same file as src. The
    # SQLite online backup API would deadlock on self-backup, so route
    # through an in-place checkpoint + drops + VACUUM instead. The
    # destination contract degrades naturally to "the canonical IS the
    # snapshot, just compacted in place." Caller MUST guarantee no
    # competing writer on this file for the duration of the call —
    # VACUUM holds an EXCLUSIVE lock and ``_drop_computed_on_dest``
    # runs DDL.
    try:
        aliased = _Path(src_db_path).resolve() == dst_path
    except OSError:
        aliased = False
    if aliased:
        return _snapshot_db_in_place(
            dst_path,
            db,
            drop_tables=drop_tables,
            drop_virtual_tables=drop_virtual_tables,
        )

    if isinstance(db, Engine):
        # Checkpoint via the SQLAlchemy engine first so the canonical .db
        # file on disk reflects the latest commits before backup. The
        # backup API copies a consistent view of in-memory pages, so it
        # would still see the WAL — but folding first means fewer pages
        # to copy and a cleaner -wal at the source.
        try:
            from mcp_middleware.runtime_db.checkpoint import run_wal_checkpoint

            run_wal_checkpoint(db)
        except Exception:  # noqa: BLE001 - best-effort optimisation
            pass

    # Use timeout=60 so a busy writer in the source doesn't make the
    # backup raise SQLITE_BUSY immediately.
    src_conn = sqlite3.connect(src_db_path, timeout=60)
    dst_conn = sqlite3.connect(str(dst_path))
    try:
        src_conn.backup(dst_conn, pages=-1)
        # Switch the destination out of WAL mode so it's self-contained.
        # Backup propagates journal_mode from source → dst, and a WAL-
        # mode dst would leave -wal / -shm sidecars next to the snapshot
        # main file. journal_mode=DELETE checkpoints any existing WAL
        # frames and switches the DB to rollback-journal mode (no -wal
        # sidecar; -journal is created per-transaction and deleted on
        # commit).
        dst_conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        # Order: close source first so its file lock releases before
        # we attempt the (optional) DDL on the destination.
        src_conn.close()
        dst_conn.close()

    # Step 4: drop computed tables on the destination. Always run when
    # either flag is requested — by default drop_virtual_tables=True so
    # this fires unless the caller explicitly opts out.
    #
    # If any drop fails (Bugbot #1+#2 on PR #131): successful drops
    # have ALREADY been committed inline (and virtual-table xDestroy
    # is not reliably transactional anyway), so dst_path is now in an
    # indeterminate half-stripped state. Shipping that partial file
    # would defeat the whole point of the fail-loud contract — the
    # caller sees a RuntimeError but a downstream consumer that
    # ignores the error would still pick up the half-modified .db.
    # Unlink dst (and its sidecars) before re-raising so the snapshot
    # location can't ship a partial file; retry semantics are then
    # "next call backs up cleanly from src again, no leftover state".
    if drop_virtual_tables or drop_tables:
        try:
            _drop_computed_on_dest(
                dst_path,
                drop_tables=drop_tables or [],
                drop_virtual_tables=drop_virtual_tables,
            )
            # Step 5: VACUUM. DROP TABLE (and xDestroy on virtual tables)
            # frees pages into the page-map but does NOT truncate the
            # file. Without this, the snapshot ships at the pre-drop
            # size — FGW observed 12.7 GB canonicals where dropping the
            # FTS5 + docvec tables should have left ~hundreds of MB.
            # Inside the same try/except as the drops so a VACUUM
            # failure also triggers the partial-file cleanup.
            _compact_dst(dst_path)
        except Exception:
            _remove_snapshot_artifacts(dst_path)
            raise

    # Step 6: defensive sidecar cleanup. After journal_mode=DELETE +
    # connection close + VACUUM, there shouldn't be -wal / -shm /
    # -journal files next to dst — but if SQLite is in a weird state
    # (e.g. drop-time crash) we don't want to ship them. Unlink any
    # that exist.
    _remove_snapshot_sidecars(dst_path)

    return dst_path


def _snapshot_db_in_place(
    dst_path: Path,
    db: DbSource,
    *,
    drop_tables: list[str] | None,
    drop_virtual_tables: bool,
) -> Path:
    """Aliased-mode snapshot: dst IS src, so do everything in place.

    The standard ``snapshot_db_only`` path uses SQLite's online backup
    API to copy src→dst. That API can't back up a database to itself —
    it would either deadlock or corrupt the file. When the caller is
    operating with ``runtime == canonical`` (typically because they're
    in dev mode via ``MCP_RUNTIME_DB_COPY=0`` and the live engine reads
    canonical directly), the snapshot still has a useful job: fold any
    WAL frames into the main file, drop computed/virtual tables the
    caller doesn't want shipped, and VACUUM to reclaim pages.

    Behaviour differences vs the standard path:

    * **No journal_mode switch.** The standard path forces dst into
      DELETE mode so the snapshot ships without ``-wal`` / ``-shm``
      sidecars. Here, the live engine wants to stay in WAL — flipping
      it would force the live server into rollback-journal mode mid-run.
      WAL stays.
    * **No dst unlink on drop failure.** The standard path's safety net
      (``_remove_snapshot_artifacts``) deletes dst if any drop fails —
      that's right when dst is a separate snapshot file, catastrophic
      when dst is the live canonical. Drop failures here re-raise
      unchanged; cleanup is the caller's problem.
    * **No sidecar removal at end.** Same reasoning — the live engine
      owns -wal/-shm.
    """
    if isinstance(db, Engine):
        # Fold the WAL so the canonical file on disk reflects every
        # committed frame. Best-effort: a checkpoint failure here just
        # means VACUUM will do more work later, not a correctness issue.
        try:
            from mcp_middleware.runtime_db.checkpoint import run_wal_checkpoint

            run_wal_checkpoint(db)
        except Exception:  # noqa: BLE001 - best-effort optimisation
            pass

    logger.info(
        "snapshot_db_only: aliased mode (dst == src %s) — in-place "
        "checkpoint + drops + VACUUM (no backup, no journal-mode switch)",
        dst_path,
    )

    if drop_virtual_tables or drop_tables:
        _drop_computed_on_dest(
            dst_path,
            drop_tables=drop_tables or [],
            drop_virtual_tables=drop_virtual_tables,
        )
    # VACUUM is the load-bearing step in aliased mode: even without
    # drops, the WAL-fold above can leave the file with substantial
    # freelist if it was holding stale pages. Run unconditionally so
    # the in-place "snapshot" is at least as compact as the backup path
    # would have produced.
    _compact_dst(dst_path)
    return dst_path


def _compact_dst(dst_path: Path) -> None:
    """Reclaim freelist pages on ``dst_path`` via ``VACUUM``.

    SQLite ``DROP TABLE`` (and virtual-table ``xDestroy``) marks pages
    as free in the page-map but does NOT shrink the file on disk. For a
    snapshot that just dropped a multi-gigabyte FTS5 / vec0 / docvec
    index, the freelist is huge and the file is still its pre-drop
    size — which defeats the entire point of dropping the indexes in
    the first place.

    ``VACUUM`` rewrites the database into a temporary copy that omits
    freelist pages, then atomically replaces the original. This is
    what actually shrinks the file on disk.

    Notes:

    * Disk usage during VACUUM peaks at roughly ``2 × live_size`` (the
      original plus the rebuild). For a 12 GB source whose live data is
      ~500 MB after drops, peak is ~12.5 GB; the file ends at ~500 MB.
    * Runs in its own fresh connection: it can't be inside an open
      transaction, and reusing the drop-time connection from
      :func:`_drop_computed_on_dest` would risk leftover state.
    * Failures here are raised; the caller (``snapshot_db_only``)
      catches inside the same ``try`` as the drops so the partial
      file gets cleaned up via :func:`_remove_snapshot_artifacts`.
    """
    import sqlite3

    conn = sqlite3.connect(str(dst_path), timeout=60.0)
    try:
        conn.isolation_level = None  # VACUUM cannot run inside a transaction
        conn.execute("VACUUM")
    finally:
        conn.close()


def _remove_snapshot_sidecars(dst_path: Path) -> None:
    """Unlink any ``-wal`` / ``-shm`` / ``-journal`` sidecars next to ``dst_path``."""
    from pathlib import Path as _Path

    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = _Path(str(dst_path) + suffix)
        try:
            sidecar.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("snapshot_db_only: could not remove %s: %s", sidecar, exc)


def _remove_snapshot_artifacts(dst_path: Path) -> None:
    """Unlink ``dst_path`` AND its sidecars.

    Used when a drop failure leaves the destination in an indeterminate
    state — we'd rather leave the snapshot slot empty than ship a
    half-stripped .db file that grader-style consumers might pick up
    without noticing the RuntimeError on the producer side.
    """
    try:
        dst_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning(
            "snapshot_db_only: could not remove partial snapshot %s after drop "
            "failure (caller will see the underlying RuntimeError, but the "
            "partial file remains on disk): %s",
            dst_path,
            exc,
        )
    _remove_snapshot_sidecars(dst_path)


def snapshot_db_via_runtime(
    canonical_path: str | Path,
    *,
    runtime: str | Path | None = None,
    drop_tables: list[str] | None = None,
    drop_virtual_tables: bool = True,
) -> Path:
    """One-shot snapshot hook for the snapshot-only deploy lifecycle.

    For hosts where the populate hook can't run (Modal / Studio after
    populate was removed), the snapshot phase is the only entry point.
    This facade composes the two existing primitives in the order the
    lifecycle requires:

    1. :func:`mcp_middleware.runtime_db.cold_seed_runtime` — copy
       ``canonical_path`` to its hashed runtime path under ``/tmp``
       (typically tmpfs) **if and only if** the runtime is absent. The
       runtime copy keeps every FTS5 / vec0 index — the live engine
       reads from it across the agent task and across subsequent
       snapshot invocations. Idempotent: subsequent calls find the
       runtime already present and skip the copy.

    2. :func:`snapshot_db_only` — write a *clean* copy from the runtime
       back to ``canonical_path``, with FTS5 / vec0 (and any
       ``drop_tables``) stripped. The runtime is **untouched** — it
       keeps its indexes for the next task.

    Net effect at every snapshot phase:

    * ``canonical_path`` = clean DB ready for the host's filesystem
      capture (``format=files``) to grab without ~16 GB of FTS5 / vec0
      baggage.
    * Runtime DB at ``runtime_paths_for(canonical_path).runtime`` keeps
      the indexes for the live engine to use during the next task.

    **First-call semantics**: the caller may pass a freshly-deployed
    ``canonical_path`` that contains the indexed bytes from the previous
    deploy. ``cold_seed_runtime`` copies those bytes into the runtime
    location (preserving indexes), and ``snapshot_db_only`` then writes
    a clean copy back to the canonical (overwriting the indexed bytes).
    After this call: canonical = clean, runtime = indexed. The agent
    task runs against the runtime; the *next* snapshot call finds the
    runtime already populated and just refreshes the canonical from it.

    Args:
        canonical_path: The path the host's filesystem capture will
            grab — typically ``$STATE_LOCATION/workspace.db``. The
            runtime path is derived from this via
            :func:`runtime_paths_for(canonical_path).runtime`, so the
            same canonical always maps to the same runtime location.
        runtime: Optional override for the runtime path. When ``None``
            (default), cold_seed_runtime computes the hashed ``/tmp``
            path. Pass an explicit path when the live engine is bound
            somewhere other than the hashed default — typically
            ``binding.runtime`` from
            :func:`~mcp_middleware.runtime_db.bind_engine`. In particular,
            **direct-mode** bindings pass ``runtime=canonical_path``,
            which routes through :func:`snapshot_db_only`'s aliased
            branch: WAL checkpoint + drops + in-place VACUUM, no copy.
        drop_tables: Forwarded to :func:`snapshot_db_only` —
            additional named regular tables to drop on the snapshot
            copy (e.g. ``["docvec_documents", "docvec_chunks"]``).
        drop_virtual_tables: Forwarded to :func:`snapshot_db_only` —
            default ``True`` drops every CREATE VIRTUAL TABLE in
            ``sqlite_master``.

    Returns:
        The absolute :class:`Path` to the canonical snapshot file
        (same as the resolved ``canonical_path``).

    Raises:
        FileNotFoundError: If the runtime path doesn't exist after
            :func:`cold_seed_runtime` — either the canonical was absent
            (nothing to snapshot) or the cold-seed copy failed. Raised
            *before* the backup step so the canonical is never wiped to
            an empty database.
        ValueError: From :func:`snapshot_db_only` for non-file SQLite
            sources (``:memory:``, URI sources without a path).
        RuntimeError: From :func:`snapshot_db_only` if any computed-table
            drop fails on the snapshot copy.

    Example:
        Snapshot hook in a snapshot-only deploy (Modal / Studio)::

            from mcp_middleware.csv_engine import snapshot_db_via_runtime

            def on_snapshot():
                snapshot_db_via_runtime("/state/workspace.db")

        On first call the indexed canonical is preserved at the
        runtime path (``/tmp/workspace_runtime_<hash>.db``) and a
        clean copy is written to ``/state/workspace.db``. Every
        subsequent call refreshes the canonical from the runtime.
    """
    from pathlib import Path as _Path

    from sqlalchemy import create_engine

    from mcp_middleware.runtime_db.sync import cold_seed_runtime

    canonical = _Path(canonical_path).expanduser().resolve()

    # Step 1: resolve the runtime path. Caller-supplied wins (direct-mode
    # bindings pass runtime=canonical, which makes snapshot_db_only take
    # its aliased branch). Otherwise cold_seed_runtime computes the
    # hashed /tmp path and seeds it if absent. cold_seed is idempotent
    # so re-entrant snapshot calls don't double-copy.
    if runtime is not None:
        runtime_path = _Path(runtime).expanduser().resolve()
    else:
        paths = cold_seed_runtime(canonical)
        runtime_path = paths.runtime

    # Step 1b: verify the runtime actually exists. cold_seed_runtime
    # returns the resolved paths even on the no-op / failure paths:
    #
    #   * canonical doesn't exist → "blank world" branch returns paths
    #     without creating runtime
    #   * copy failed mid-flight → exception caught, runtime cleaned up,
    #     paths returned anyway (so the caller can fall back to the
    #     canonical)
    #
    # If we proceeded to snapshot_db_only with a nonexistent runtime,
    # sqlite3.connect would silently create an empty .db at the runtime
    # path, backup would copy zero pages onto the canonical, and the
    # caller's populated canonical would be wiped to empty. Raise
    # instead so the caller sees the underlying problem (missing
    # canonical or copy failure) before any destructive operation.
    if not runtime_path.exists():
        raise FileNotFoundError(
            f"snapshot_db_via_runtime: runtime path {runtime_path} does not "
            f"exist after cold_seed_runtime from {canonical}. Either the "
            f"canonical is missing (no DB to snapshot — check that "
            f"{canonical} exists) or the cold-seed copy failed (search "
            f"logs for 'cold_seed: copy ... failed'). Proceeding to "
            f"snapshot_db_only would overwrite the canonical with an "
            f"empty database."
        )

    # Step 2: open a one-shot engine pointing at the runtime, snapshot
    # back to the canonical with indexes stripped, dispose. We don't
    # reuse the server's live engine because the live engine might be
    # configured against any URL — the runtime path is what we just
    # cold-seeded and is the authoritative source for the snapshot.
    # When canonical == runtime (direct mode), snapshot_db_only takes
    # its aliased branch (in-place checkpoint + drops + VACUUM).
    engine = create_engine(f"sqlite:///{runtime_path}", future=True)
    try:
        return snapshot_db_only(
            canonical,
            engine,
            drop_tables=drop_tables,
            drop_virtual_tables=drop_virtual_tables,
        )
    finally:
        engine.dispose()


@dataclass(frozen=True)
class SnapshotHookResult:
    """Per-step observability output from :func:`snapshot_with_populate`.

    Returned so operators can grep / log exactly what each step did on a
    given snapshot invocation — most useful when diagnosing "why did the
    snapshot grow / shrink / take longer than expected" without having
    to re-read every individual primitive's logs.
    """

    canonical: Path
    """Absolute path to the clean canonical DB written by step 5."""
    harvested: list[tuple[Path, Path]]
    """``(src, dst)`` pairs for every ``.db`` moved by step 1. Empty on
    subsequent calls (state_dir has no leftover ``.db`` after the first
    snapshot — :func:`snapshot_db_via_runtime` writes clean canonicals
    only)."""
    import_rc: int | None
    """Return code from ``import_hook`` (typically ``populate_engine.main``).
    ``None`` when no hook was provided. A non-zero rc raises and never
    surfaces here (steps 3-5 don't run on import failure)."""
    index_built: bool
    """``True`` iff ``build_index_hook`` was provided AND was invoked.
    ``False`` when no hook was given or the caller's hook itself
    decided to skip (the facade doesn't introspect)."""
    pruned: list[Path]
    """Files deleted by step 4. Empty when ``config`` is ``None`` (the
    facade skips pruning entirely)."""
    import_skipped_reason: str | None = None
    """Why step 2 (``import_hook``) was skipped despite a hook being
    provided. ``None`` means either no hook was provided OR the hook
    ran normally. Set to ``"pre_built_db_shipped"`` when step 1 harvested
    a ``.db`` matching ``canonical.name`` — the SME shipped an
    authoritative DB, so re-ingesting raw sources on top of it would
    only produce PK collisions. Apps can grep this field to confirm the
    skip happened on the expected branch."""
    post_harvest_ran: bool = False
    """``True`` iff ``post_harvest_hook`` was provided AND was invoked
    (between steps 1 and 2). ``False`` when no hook was given. Apps that
    use the hook to relocate sidecar DBs (e.g. docvec ``.db`` files to a
    consumer-expected path) can assert this in their snapshot script
    to confirm the relocation actually ran."""
    overridden: dict[str, int] = field(default_factory=dict)
    """Per-table inserted-row counts from applying ``import_always`` entities
    on top of a harvested pre-built DB (step 2's ``pre_built_db_shipped``
    branch). Empty ``{}`` in every other regime: no pre-built DB, no entity
    flagged ``import_always``, or the flagged entities had no shipped source
    files present. When non-empty, ``import_skipped_reason`` is
    ``"pre_built_db_shipped"`` — the bulk import was skipped but these specific
    entities were still applied (their tables cleared and re-seeded)."""
    drained: bool | None = None
    """Result of the pre-harvest live-server pool drain (step 0.5). ``None``
    when no ``checkpoint_url`` / ``server_port`` was provided (drain skipped).
    ``True`` when :func:`~mcp_middleware.runtime_db.drain_server_pool` reported
    it was safe to harvest (server drained OR unreachable). A ``False`` never
    surfaces here — a refused drain raises before harvest."""
    handoff_ran: bool = False
    """``True`` iff the post-snapshot runtime handoff (step 6) was invoked —
    only when a ``checkpoint_url`` / ``server_port`` was provided AND the
    binding is RUNTIME mode (not aliased/direct). ``False`` otherwise. The
    handoff's own success is best-effort and logged, not reflected here."""
    default_user_action: str | None = None
    """What the default-user identity step (step 3.5) did on the runtime DB.
    ``None`` when ``default_user_table`` was not provided (step skipped). One of
    ``"applied_csv"`` (a shipped ``default_user_csv`` cleared + re-seeded the
    table, CSV authoritative), ``"cleared"`` (no CSV and ``trust_baked_rows``
    was False → table emptied, fail-closed), or ``"trusted_baked"`` (no CSV and
    ``trust_baked_rows`` was True → baked row left as-is). After this action the
    strict :func:`~mcp_middleware.require_default_user` assert runs; a missing
    identity raises before the snapshot is written."""


def snapshot_with_populate(
    state_dir: str | Path,
    canonical: str | Path,
    config: SnapshotConfig | None = None,
    *,
    import_hook: Callable[[], int] | None = None,
    build_index_hook: Callable[[], None] | None = None,
    post_harvest_hook: Callable[[list[tuple[Path, Path]]], None] | None = None,
    drop_tables: list[str] | None = None,
    drop_virtual_tables: bool = True,
    runtime: str | Path | None = None,
    checkpoint_url: str | None = None,
    server_port: int | str | None = None,
    default_user_table: str | None = None,
    default_user_csv: str | None = None,
    default_user_trust_baked_rows: bool = False,
    default_user_ref: DefaultUserRef | None = None,
    enforce_default_user: bool | None = None,
) -> SnapshotHookResult:
    """One-shot snapshot orchestration for the snapshot-only deploy lifecycle.

    Composes the five primitives every Foundry-* MCP server needs into
    a single call, so each app's ``snapshot.py`` becomes a one-liner
    passing its app-specific ``populate_engine.main`` and FTS rebuilder
    as callbacks.

    The orchestration shape is validated against Foundry-Google-Workspace
    PR #185 (``scripts/snapshot_engine.py``). The five steps are all
    idempotent, so the same hook fires correctly whether the Modal
    ``populate`` lifecycle hook ran beforehand or not, and whether the
    SME shipped pre-built ``.db`` files or CSV/JSON sources:

    1. :func:`mcp_middleware.runtime_db.harvest_db_files` — move every
       ``.db`` (and its WAL/SHM/srcmeta sidecars) out of ``state_dir``
       to its hashed ``/tmp`` runtime path. Pre-indexed input: the
       SME-uploaded DB lands at the runtime path. Cold-CSV / warm-reboot
       input: no ``.db`` to move; no-op.

    2. ``import_hook()`` — app-specific. Typically
       ``lambda: populate_engine.main([state_dir])`` for apps that
       ingest CSV/JSON sources. Must return an ``int`` exit code; a
       non-zero rc raises :class:`RuntimeError` and steps 3-5 don't run
       (the DB is in an unknown state). Omit for apps whose state is
       purely DB-uploaded.

       **Auto-skipped when step 1 harvested a DB matching
       ``canonical.name``.** The SME-shipped DB is the source of truth;
       re-ingesting raw sources on top of it would only produce PK
       collisions. The facade detects this case by inspecting the
       :func:`harvest_db_files` return value. If raw source files are
       *also* present in ``state_dir`` (matched by
       :func:`discover_snapshot` against ``config``), a warning is
       logged — the sources are deleted unread by step 4. The current
       contract is exclusive: pre-built DB OR raw sources, not both.
       (Future delta-application would be a separate explicit API.)

    3. ``build_index_hook()`` — app-specific. Builds the app's FTS5 /
       vec0 / per-app derived indexes on the runtime DB. Typically a
       closure over an app's ``init_fts`` + ``rebuild_fts`` + an
       ``index_needs_build`` guard. Omit for apps without an indexed
       schema.

    4. :func:`prune_imported_files` — delete every file in ``state_dir``
       that the importer just consumed (CSVs / JSONs / binaries
       declared as entities). Agent-created files in ``state_dir`` —
       which land AFTER this prune step — are preserved. Skipped
       entirely when ``config`` is ``None``.

    5. :func:`snapshot_db_via_runtime` — cold-seed the canonical to
       its runtime (no-op when the runtime is already present from
       step 1) then write a clean copy back to ``canonical`` with
       virtual tables + ``drop_tables`` stripped. The runtime is
       untouched — it keeps its indexes for the live engine to read
       during the next agent task.

    Net effect on every invocation: ``canonical`` is a clean indexless
    DB ready for the host's filesystem capture (Modal ``format=files``),
    and the runtime DB at
    :func:`runtime_paths_for(canonical).runtime` keeps its indexes for
    the live engine.

    .. warning::
       **Do not write to the runtime between** :func:`bind_engine` **and
       this call.** This facade rebuilds ``canonical`` from scratch via
       ``import_hook`` and step 1's harvest moves ``state_dir/<canonical>``
       onto the runtime path via :func:`shutil.move`. If something wrote
       to the runtime after a prior :func:`bind_engine` cold-seed (but
       before this call), the harvest will overwrite those writes — the
       ``.srcmeta`` marker only fingerprints the canonical at seed time
       and does not track subsequent runtime mutations. This is by design:
       the facade is a build pipeline, not an incremental writer. Either
       (a) keep population effects inside ``import_hook`` so step 1
       sequencing is honoured, or (b) use :func:`refresh_runtime_from_canonical`
       (worker pattern) if the intent is to roll forward live writes.

    Args:
        state_dir: Directory the snapshot hook operates on — typically
            ``$STATE_LOCATION``. Harvest scans it (step 1) and prune
            cleans it (step 4).
        canonical: Path the host's filesystem capture will grab —
            typically ``state_dir / "workspace.db"``. The runtime path
            is derived from this via
            :func:`runtime_paths_for(canonical).runtime`.
        config: Snapshot config used by step 4 to identify which files
            in ``state_dir`` the importer would have consumed.
            ``None`` skips pruning entirely (correct for apps with no
            CSV/JSON sources, e.g. pure DB-uploaded state).
        import_hook: Step 2 callable. Returns an ``int`` exit code; a
            non-zero return raises :class:`RuntimeError` and aborts the
            rest of the orchestration. ``None`` skips step 2.
        build_index_hook: Step 3 callable. Returns nothing; raises on
            failure. ``None`` skips step 3.
        post_harvest_hook: Optional step-1b callable that fires
            **between** harvest (step 1) and import (step 2). Receives
            the ``list[(src, dst)]`` tuples harvest produced; can rename
            or relocate harvested files so the import / index / snapshot
            consumers find them at app-specific paths. The canonical use
            case is sidecar DBs that don't fit the standard hashed-runtime
            layout — e.g. Foundry-Google-Workspace's docvec sidecar lands
            at ``db.vec.vec_db_path(canonical)`` not at the harvest-
            computed hashed runtime path. Exceptions propagate; hooks
            should be idempotent. ``None`` (default) skips the hook.
        drop_tables: Forwarded to :func:`snapshot_db_via_runtime` —
            additional regular tables to drop on the snapshot copy
            (e.g. ``["docvec_documents", "docvec_chunks"]``).
        drop_virtual_tables: Forwarded to :func:`snapshot_db_via_runtime`
            — default ``True`` drops every CREATE VIRTUAL TABLE on the
            snapshot copy.
        runtime: Optional override for the live runtime path. Forwarded
            to :func:`snapshot_db_via_runtime`. Also used as the
            ``protect_paths`` argument to :func:`harvest_db_files`, so
            a direct-mode binding (``runtime == canonical`` under
            ``state_dir``) doesn't have its live DB moved out from
            under the engine at step 1. ``None`` (default) preserves
            the historical runtime-mode behaviour: hashed ``/tmp``
            runtime path, harvest moves everything.
        checkpoint_url: Optional ``/_internal/checkpoint`` URL of the *live
            server* (a separate process/uid) sharing this runtime DB. When
            provided (or derivable from ``server_port``), two extra steps run:

            * **Step 0.5 (pre-harvest drain).** Before harvest moves any
              ``.db``, POST the checkpoint route so the live server folds its
              WAL and disposes its pool (see
              :func:`~mcp_middleware.runtime_db.drain_server_pool`). If the
              server is *reachable but never drains*, this **raises**
              :class:`RuntimeError` — harvesting under a live pool would poison
              it. An unreachable server (cold populate) is treated as safe.
            * **Step 6 (post-snapshot handoff).** In RUNTIME mode only (not
              aliased/direct), relax the runtime perms to ``0o666`` and drive
              one more checkpoint so the live server drops any poisoned
              read-only connection and reopens the file RW (see
              :func:`~mcp_middleware.runtime_db.handoff_runtime_to_server`).

            ``None`` (default) skips both steps — correct for single-process
            deploys with no concurrently-running server. Note this facade does
            **not** call :func:`~mcp_middleware.runtime_db.unlink_stale_runtime`
            or :func:`~mcp_middleware.runtime_db.estimated_post_import_bytes`:
            those must run *before* the caller's ``bind_engine`` and so stay in
            the caller's pre-facade code.
        server_port: Convenience alternative to ``checkpoint_url`` — the live
            server's port. When ``checkpoint_url`` is ``None`` and this is set,
            the URL is built as
            ``http://127.0.0.1:{server_port}/_internal/checkpoint``. Ignored
            when ``checkpoint_url`` is given.
        default_user_table: When set, run the default-user identity step (step
            3.5) on the runtime DB before prune/snapshot: apply the shipped
            ``default_user_csv`` (CSV-authoritative), then assert the singleton
            identity row exists via
            :func:`~mcp_middleware.require_default_user` — a missing identity
            **raises** (no snapshot is written). ``None`` (default) skips the
            step entirely (apps with no default-user concept). Must be a plain
            SQL identifier.
        default_user_csv: Filename (relative to ``state_dir``) of the
            default-user CSV, e.g. ``"default_user.csv"``. When present with a
            data row it is authoritative: the table is cleared and re-seeded
            from it (wins over a baked/stale row a pre-built DB shipped). When
            absent / blank, behaviour is governed by
            ``default_user_trust_baked_rows``. Ignored unless
            ``default_user_table`` is set.
        default_user_trust_baked_rows: Governs ONLY the no-CSV-this-run case.
            ``False`` (default, fail-closed) → clear the table so an untrusted
            baked row can't satisfy the assert (identity must come from
            populate). ``True`` → leave a baked row as-is (a deliberate signal
            to use it). The CSV-present path clears + re-seeds identically in
            both modes. Ignored unless ``default_user_table`` is set.
        default_user_ref: Optional :class:`~mcp_middleware.DefaultUserRef`. When
            set, the step-3.5 assert additionally requires the identity row's
            foreign key to resolve, raising ``DefaultUserDanglingReferenceError``
            (distinct from the empty-table ``DefaultUserNotConfiguredError``)
            when it points at a missing row. Ignored unless ``default_user_table``
            is set and enforcement is on.
        enforce_default_user: Per-call override for the step-3.5 identity assert.
            ``True`` forces the assert on, ``False`` forces it off, ``None``
            (default) defers to the global default. The ``MCP_ENFORCE_DEFAULT_USER``
            environment variable, when set to a truthy/falsey value, takes
            precedence over this argument (precedence: env > this arg > default).

    Returns:
        :class:`SnapshotHookResult` with per-step observability data
        (what got harvested, what got pruned, whether the index was
        rebuilt, the final canonical path).

    Raises:
        RuntimeError: When ``import_hook`` returns a non-zero exit
            code (the DB is in an unknown state; steps 3-5 don't run),
            OR when a ``checkpoint_url`` / ``server_port`` was provided
            and the live server is reachable but its pool never drains
            (step 0.5 refuses to harvest under a live pool).
        DefaultUserNotConfiguredError: When ``default_user_table`` is set
            but no identity row exists after step 3.5 (no ``default_user_csv``
            and either an empty table or ``trust_baked_rows=False``). Raised
            before the snapshot so a deploy with no identity fails loudly.
        DefaultUserDanglingReferenceError: When ``default_user_ref`` is set and
            the identity row's FK points at a missing referenced row. Raised
            before the snapshot, distinct from the no-identity case above.
        FileNotFoundError: From step 5 — see
            :func:`snapshot_db_via_runtime` for the conditions.
        Anything ``build_index_hook`` raises (propagated unchanged so
            FTS rebuild failures are loud, not swallowed).

    Example:
        Minimal app-side ``scripts/snapshot.py``::

            import os
            from pathlib import Path
            from mcp_middleware.csv_engine import load_config, snapshot_with_populate

            import populate_engine
            from db.fts import index_needs_build, init_fts, rebuild_fts
            from db.session import engine

            def build_index() -> None:
                init_fts(engine)
                if index_needs_build(engine):
                    rebuild_fts(engine)

            state_dir = Path(os.environ["STATE_LOCATION"])
            snapshot_with_populate(
                state_dir=state_dir,
                canonical=state_dir / "workspace.db",
                config=load_config(Path("snapshot_config.yaml")),
                import_hook=lambda: populate_engine.main([str(state_dir)]),
                build_index_hook=build_index,
                drop_tables=["docvec_documents", "docvec_chunks"],
            )

        Note: the app must arrange for ``db.session.engine`` (and any
        other module that runs ``cold_seed_runtime`` at import time)
        to be imported AFTER step 1 — typically by deferring the
        import into the ``build_index_hook`` closure body so harvest
        runs first.
    """
    from pathlib import Path as _Path

    from mcp_middleware.runtime_db.harvest import harvest_db_files
    from mcp_middleware.runtime_db.paths import (
        fingerprint_canonical,
        read_marker,
        runtime_paths_for,
    )

    # ── MemoryMode short-circuit (belt-and-suspenders) ──────────────────────
    # A caller that mis-routes the ``":memory:"`` SQLite sentinel into this
    # facade would otherwise hit ``Path(":memory:").resolve()`` on the next
    # line and silently get ``$PWD/:memory:`` — a real filesystem path. The
    # ``mkdir`` above would create ``$PWD`` (no-op for cwd) and the harvest /
    # snapshot pipeline would then run against the wrong file. Detect and
    # short-circuit instead. Returns a no-op :class:`SnapshotHookResult` with
    # ``import_skipped_reason="memory_mode"`` so the caller's observability
    # surface (logs, dashboards) shows exactly why nothing happened.
    #
    # Defence-in-depth for the typed
    # :func:`~mcp_middleware.runtime_db.resolve_canonical_db_path` API: even
    # when callers branch on the resolver's sum type at the boundary, an
    # intermediate refactor that drops the ``MemoryMode`` branch and just
    # passes the raw sentinel through would otherwise re-introduce the bug.
    # The short-circuit makes the bug class structurally unreachable in this
    # codepath regardless of caller discipline.
    if str(canonical) == ":memory:":
        logger.info(
            "snapshot_with_populate: canonical is ':memory:' — short-circuiting "
            "(no harvest / import / build_index / prune / snapshot in memory mode)"
        )
        return SnapshotHookResult(
            canonical=_Path(":memory:"),
            harvested=[],
            import_rc=None,
            index_built=False,
            pruned=[],
            import_skipped_reason="memory_mode",
            post_harvest_ran=False,
        )

    state_path = _Path(state_dir).expanduser()
    state_path.mkdir(parents=True, exist_ok=True)
    canonical_path = _Path(canonical).expanduser().resolve()

    # Direct-mode detection: the live engine is bound to canonical itself
    # (e.g. ``bind_engine`` in direct mode with runtime == canonical).
    # In that case the canonical under state_dir IS the live DB; the
    # step-0 guard below MUST NOT delete it, and step 5's snapshot writes
    # in place rather than copying. The harvest in step 1 already gets a
    # ``protect_paths`` entry that covers the file.
    aliased_mode = False
    if runtime is not None:
        try:
            aliased_mode = _Path(runtime).expanduser().resolve() == canonical_path
        except OSError:
            aliased_mode = False

    # ── Step 0: protect the live runtime from a stale state_dir copy ──────
    # On the 2nd+ call within a container's lifetime, snapshot_db_via_runtime
    # has already written the *clean* canonical back into state_dir. If
    # we let harvest_db_files run unconditionally it would see that
    # clean .db, move it onto the hashed /tmp runtime path, and
    # shutil.move overwrites — clobbering the live indexed runtime
    # (which has the agent task's writes + rebuilt FTS5 / vec0 indexes).
    #
    # Detect "the runtime is the source of truth" by asking: does a
    # runtime already exist at the hashed path for this canonical? If
    # yes, the canonical in state_dir (if present and directly under
    # state_dir so harvest would see it) is a stale snapshot artifact;
    # delete it WITH its sidecars before harvest runs.
    #
    # We only remove the file harvest would have picked up — the
    # canonical .db at state_dir/<name>.db. Any *other* .db files
    # someone dropped into state_dir (test fixtures, sidecar DBs)
    # still get harvested normally; this guard is specifically about
    # not feeding the previous-call's own output back onto its runtime.
    #
    # Marker-fingerprint discriminator (added to fix the bind_engine
    # composition bug reported by Foundry-zoho): the original guard
    # assumed the only way a runtime exists at facade-entry is that a
    # *prior* snapshot_with_populate cold-seeded it AND something wrote
    # to it since. ``bind_engine``'s cold-seed-on-call broke that
    # assumption — the runtime can exist at entry as a byte-for-byte
    # copy of state_dir/<canonical>, with zero divergence. Deleting
    # state_dir/<canonical> in that case strips the SME-shipped pre-built
    # DB that step 1's harvest needs to detect for PR #136's auto-skip,
    # which then re-runs the importer on top of pre-built rows and PK-
    # collides.
    #
    # The fix: compare the runtime's ``.srcmeta`` marker fingerprint
    # against state_dir/<canonical>'s current fingerprint. cold_seed_runtime
    # writes the marker with the source canonical's fingerprint, so the
    # marker has three possible states with three different meanings:
    #
    #   - MATCH (marker == canonical fp)
    #              → runtime is a faithful copy of state_dir/<canonical>;
    #              harvest moving state_dir/<canonical> onto the runtime
    #              path is a no-op for the underlying bytes (same data),
    #              and PR #136's auto-skip will correctly fire on the
    #              harvest result. Skip step 0's deletion.
    #   - MISMATCH (marker present, fp differs)
    #              → runtime has diverged from state_dir/<canonical>
    #              (post-snapshot clean canonical, new SME deploy, live
    #              writes after a marker stamp). state_dir is stale →
    #              delete (preserves runtime's divergence).
    #   - MISSING (marker file absent OR empty)
    #              → runtime origin is unknown. Could be (a) an empty
    #              runtime lazy-created by SQLAlchemy when ``bind_engine``
    #              cold-seeded before the SME canonical was downloaded
    #              (the Modal boot race that caused the Atlassian /
    #              FGW empty-DB-in-Studio symptom), or (b) a faithful
    #              copy whose marker got lost. The safe default in
    #              EITHER case is: prefer canonical. Harvest will move
    #              canonical onto the runtime path, replacing whatever
    #              was there; PR #136's auto-skip then fires.
    #
    # Why prefer canonical when marker is missing rather than delete it:
    # canonical lives on persistent storage (Modal volume), runtime on
    # tmpfs. If the marker is absent, we can't tell whether runtime has
    # authoritative writes — but tmpfs is volatile and the marker is
    # the contract that says "this runtime was authoritatively stamped".
    # No marker == no contract == defer to canonical, which is the only
    # surface we KNOW has data we want.
    runtime_for_canonical = runtime_paths_for(canonical_path).runtime
    canonical_under_state = canonical_path.parent.resolve() == state_path.resolve()

    marker_fp = ""
    canonical_fp = ""
    if runtime_for_canonical.exists() and canonical_path.exists():
        try:
            marker_fp = read_marker(runtime_paths_for(canonical_path).marker)
            canonical_fp = fingerprint_canonical(canonical_path)
        except OSError:
            # Can't read marker or stat canonical — fall through to the
            # "marker missing" branch (treat as origin unknown, prefer
            # canonical, do not delete).
            marker_fp = ""
            canonical_fp = ""

    marker_present_and_diverged = bool(marker_fp) and marker_fp != canonical_fp

    if (
        not aliased_mode
        and runtime_for_canonical.exists()
        and canonical_under_state
        and canonical_path.exists()
        and marker_present_and_diverged
    ):
        for suffix in ("", "-wal", "-shm", ".srcmeta"):
            sidecar = _Path(str(canonical_path) + suffix)
            try:
                sidecar.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning(
                    "snapshot_with_populate: could not remove stale state_dir artifact %s: %s",
                    sidecar,
                    exc,
                )
        logger.info(
            "snapshot_with_populate: removed stale state_dir copy of %s "
            "(runtime %s is the source of truth — harvest would have "
            "clobbered it)",
            canonical_path,
            runtime_for_canonical,
        )

    # ── Resolve the live server's base URL ONCE ───────────────────────────
    # persist (step 0.4) and drain/checkpoint (step 0.5) are sibling routes
    # under a single ``/_internal`` mount. Both callers name that base the same
    # way — a loopback ``server_port`` or an explicit ``checkpoint_url`` — so we
    # resolve it to one base here and derive every route from it. That keeps
    # persist and drain pointed at the SAME host + mount structurally, instead
    # of each deriving its own URL and risking divergence when both kwargs are
    # passed. ``None`` means no live server was named (cold populate).
    from mcp_middleware.runtime_db.lifecycle import server_base_from_locator

    server_base = server_base_from_locator(server_port=server_port, checkpoint_url=checkpoint_url)

    # ── Step 0.4: persist the live server's runtime → canonical ───────────
    # The runtime DB lives under a per-uid ``0o700`` dir, so a cross-uid
    # snapshot / fold hook (this process, often a different uid than the live
    # server) can neither read the runtime nor compute its path. Only the server
    # can fold its own tool-call mutations onto the canonical. POST the shared
    # /_internal/persist route so it does exactly that, in-process, under the
    # correct uid — BEFORE harvest reads the canonical. This must run before the
    # drain (step 0.5): drain disposes the pool, but persist needs the live pool
    # to checkpoint the WAL. A reachable server whose persist genuinely failed
    # means we MUST NOT harvest a canonical missing its writes — raise. An
    # unreachable server (cold populate) or an older/engine-only server (404/501
    # → legacy fallback) is safe to proceed past. Skipped when no server named.
    if server_base is not None:
        from mcp_middleware.runtime_db.lifecycle import persist_server_runtime

        persist_url = f"{server_base}/persist"
        persisted = persist_server_runtime(persist_url)
        if not persisted:
            raise RuntimeError(
                f"snapshot_with_populate: refusing to harvest — the live server "
                f"at {persist_url} is reachable and has the persist route, but "
                f"folding its runtime DB onto the canonical failed. Harvesting now "
                f"would snapshot a canonical missing the server's latest writes. "
                f"Retry once the server can checkpoint, or stop it before running "
                f"populate."
            )

    # ── Step 0.5: drain the live server's pool before harvest ─────────────
    # When a live server (separate process/uid) shares this runtime DB, harvest
    # moving the .db out from under its connection pool poisons the pool with
    # SQLITE_IOERR. POST the shared /_internal/checkpoint route first so the
    # server folds its WAL and disposes its pool. A reachable-but-undrained
    # server means we MUST NOT harvest — raise. An unreachable server (cold
    # populate, no live reader) is safe. Skipped when no server named.
    drained: bool | None = None
    if server_base is not None:
        from mcp_middleware.runtime_db.lifecycle import drain_server_pool

        drain_url = f"{server_base}/checkpoint"
        drained = drain_server_pool(drain_url)
        if not drained:
            raise RuntimeError(
                f"snapshot_with_populate: refusing to harvest — the live server "
                f"at {drain_url} is reachable but its connection pool never "
                f"drained. Harvesting the runtime DB now would poison its pool "
                f"(SQLITE_IOERR until restart). Retry once the server is idle, or "
                f"stop it before running populate."
            )

    # ── Step 1: harvest stale .db files into hashed /tmp runtime paths ─────
    # After step 0 the canonical's stale copy is gone, so harvest only
    # picks up genuinely-pre-existing files (SME-shipped DBs on the
    # first call; any other .db sidecars on every call).
    #
    # When the caller passed a ``runtime`` override (direct-mode binding
    # where runtime == canonical), pass it as ``protect_paths`` so harvest
    # leaves the live engine's bound file alone. In normal runtime mode
    # the override is the hashed /tmp path, which is never inside
    # ``state_dir`` — protect_paths is a no-op.
    harvest_protected: list[Path] | None = None
    if runtime is not None:
        harvest_protected = [_Path(runtime).expanduser()]
    harvested = harvest_db_files(state_path, protect_paths=harvest_protected)
    for src, dst in harvested:
        logger.info("snapshot_with_populate: harvested %s → %s", src, dst)
    if not harvested:
        logger.debug("snapshot_with_populate: no pre-existing .db files in %s", state_path)

    # ── Step 1b: post-harvest hook (between harvest and import) ───────────
    # App-specific relocation / rename of harvested files before the
    # importer + index-builder + snapshot consumers expect them at their
    # consumer-defined paths. The canonical use case is sidecar DBs that
    # don't fit the standard hashed-runtime layout — e.g. Foundry-Google-
    # Workspace's docvec.db lands at ``db.vec.vec_db_path(canonical)`` not
    # at ``runtime_paths_for(docvec.db).runtime``; the hook receives the
    # ``[(src, dst)]`` tuples harvest produced and can move files into
    # their consumer-expected locations.
    #
    # Runs between steps 1 and 2 (not after 2) so the import_hook and
    # build_index_hook can observe the relocated files at their final
    # paths. Exceptions propagate — a hook failure means the layout is
    # ambiguous and continuing would produce a snapshot of mis-located
    # state. Hooks should be idempotent (a re-entrant call with the same
    # ``harvested`` list must be safe).
    post_harvest_ran = False
    if post_harvest_hook is not None:
        logger.info(
            "snapshot_with_populate: running post_harvest_hook (%d harvested file(s))",
            len(harvested),
        )
        post_harvest_hook(harvested)
        post_harvest_ran = True
        logger.info("snapshot_with_populate: post_harvest_hook completed")

    # ── Step 2: import any source files via the app's populate path ────────
    # We do NOT swallow non-zero exit codes — a failed import means the
    # DB is in an unknown state and continuing to steps 3-5 would write
    # a snapshot of garbage. Raise loudly so the snapshot hook fails
    # visibly (caller's bash wrapper exits non-zero).
    #
    # AUTO-SKIP when an SME-shipped DB was harvested in step 1: re-running
    # the importer on top of a pre-built canonical only produces PK
    # collisions (the engine has no way to reconcile raw sources against
    # rows that already exist). The contract is exclusive — either ship a
    # populated DB or ship raw sources, not both — so we skip the import
    # entirely and let step 4 (prune) delete any sources that snuck in.
    # We log a warning rather than raise on the mixed case because (a) the
    # SME contract calls it ambiguous, not invalid, and (b) prune handles
    # the cleanup either way; failing the whole snapshot would be worse
    # than running it correctly with a visible warning.
    pre_built_db_shipped = any(src.name == canonical_path.name for src, _ in harvested)
    import_rc: int | None = None
    import_skipped_reason: str | None = None
    overridden: dict[str, int] = {}
    if pre_built_db_shipped:
        import_skipped_reason = "pre_built_db_shipped"
        logger.info(
            "snapshot_with_populate: SME-shipped %s harvested in step 1 — "
            "skipping import_hook (the DB is authoritative; re-ingest would "
            "only collide on existing PKs)",
            canonical_path.name,
        )
        # Entities flagged ``import_always: true`` are the *exception* to the
        # skip: the SME ships a small override CSV/JSON alongside the pre-built
        # DB (e.g. a ``current_user.csv`` re-pointing a singleton). Apply them
        # to the runtime DB now — strictly before step 4 prune deletes the
        # sources — via the clear strategy (empty the flagged tables, insert the
        # shipped rows). All the FK-ordering / dedup / hook machinery is reused
        # from the normal clear-import path; see :func:`apply_import_always`.
        import_always = config.import_always_entities() if config is not None else []
        if import_always:
            from .importer import apply_import_always

            # Resolve the runtime DB the pre-built canonical was harvested onto:
            # honour an explicit ``runtime`` override, else the hashed path.
            if runtime is not None:
                runtime_db = _Path(runtime).expanduser().resolve()
            else:
                runtime_db = runtime_paths_for(canonical_path).runtime

            async def _apply() -> dict[str, int]:
                engine = create_async_engine(_to_async_url(f"sqlite:///{runtime_db}"))
                try:
                    return await apply_import_always(
                        state_path, engine, config, entities=import_always
                    )
                finally:
                    await engine.dispose()

            overridden = _run(_apply())
            if overridden:
                logger.info(
                    "snapshot_with_populate: applied import_always override for "
                    "entities %s on top of pre-built %s → %s",
                    import_always,
                    canonical_path.name,
                    overridden,
                )

        if config is not None and import_hook is not None:
            # discover_snapshot is the same primitive prune uses, so the
            # warning surfaces exactly the files step 4 will delete. Exclude
            # ``import_always`` entities — their sources were applied above,
            # not deleted unread, so they aren't part of the ambiguous mix.
            from .importer import discover_snapshot

            override_set = set(import_always)
            discovered = discover_snapshot(state_path, config)
            import_source_count = sum(
                len(entries)
                for bucket, entries in discovered.items()
                if bucket != "_unrecognized" and bucket not in override_set
            )
            if import_source_count:
                logger.warning(
                    "snapshot_with_populate: pre-built DB %s was shipped AND "
                    "%d raw source file(s) matching entity globs are present "
                    "in %s — the sources will be deleted unread by step 4. "
                    "The current contract is exclusive: ship a populated DB "
                    "OR raw sources, not both.",
                    canonical_path.name,
                    import_source_count,
                    state_path,
                )
    elif import_hook is not None:
        logger.info("snapshot_with_populate: running import_hook")
        import_rc = import_hook()
        if import_rc != 0:
            raise RuntimeError(
                f"snapshot_with_populate: import_hook returned non-zero exit "
                f"code {import_rc}. The runtime DB is in an unknown state; "
                f"aborting before FTS rebuild / prune / snapshot to avoid "
                f"writing a snapshot of partially-imported data."
            )
        logger.info("snapshot_with_populate: import_hook completed (rc=0)")
    else:
        logger.debug("snapshot_with_populate: no import_hook provided — skipping step 2")

    # ── Step 3: build derived indexes (FTS5 / vec0 / etc.) ────────────────
    # Hook is responsible for its own idempotency guard (e.g. checking
    # ``index_needs_build`` before calling ``rebuild_fts``). Exceptions
    # propagate — a half-built FTS is a corrupt DB, the snapshot should
    # not proceed.
    index_built = False
    if build_index_hook is not None:
        logger.info("snapshot_with_populate: running build_index_hook")
        build_index_hook()
        index_built = True
        logger.info("snapshot_with_populate: build_index_hook completed")
    else:
        logger.debug("snapshot_with_populate: no build_index_hook provided — skipping step 3")

    # ── Step 3.5: default-user identity (CSV-authoritative apply + assert) ─
    # Run BEFORE prune (which would delete default_user_csv) on the runtime DB
    # that step 5 snapshots into the canonical. Apply the shipped CSV
    # authoritatively (clear + re-seed), or clear/trust a baked row per
    # ``default_user_trust_baked_rows``, then strictly assert the identity row
    # exists. A missing identity raises here — before the snapshot — so a deploy
    # with no default user fails loudly instead of shipping an identity-less DB.
    default_user_action: str | None = None
    if default_user_table is not None:
        from sqlalchemy import create_engine

        from mcp_middleware.runner import (
            apply_default_user_from_csv,
            default_user_enforced,
            require_default_user,
        )

        if runtime is not None:
            du_db = _Path(runtime).expanduser().resolve()
        else:
            du_db = runtime_paths_for(canonical_path).runtime
        du_csv = state_path / default_user_csv if default_user_csv else None
        du_enforced = default_user_enforced(enforce_default_user)
        du_engine = create_engine(f"sqlite:///{du_db}", future=True)
        try:
            # Always apply a shipped CSV (harmless seeding, keeps data), but only
            # RAISE on a missing identity when enforcement is on. Enforcement is
            # disabled by default (see default_user_enforced) so apps may populate
            # with no default user while the identity issues are worked out; flip
            # the switch back on to restore the fail-loud assert.
            default_user_action = apply_default_user_from_csv(
                du_engine,
                default_user_table,
                du_csv,
                trust_baked_rows=default_user_trust_baked_rows,
            )
            if du_enforced:
                # Strict fail-loud assert — raises DefaultUserNotConfiguredError
                # if the table is still empty (propagates out of the facade →
                # populate exits non-zero, no snapshot written). With a
                # default_user_ref it also raises DefaultUserDanglingReferenceError
                # when the identity row's FK doesn't resolve.
                require_default_user(du_engine, default_user_table, ref=default_user_ref)
            else:
                logger.info(
                    "snapshot_with_populate: default-user enforcement disabled — "
                    "skipping identity assert (set MCP_ENFORCE_DEFAULT_USER=true "
                    "to re-enable)"
                )
        finally:
            du_engine.dispose()
        # Only the enforced path *guarantees* an identity (the assert above would
        # have raised otherwise). When enforcement is off the table may still be
        # empty, so don't claim "ensured" — report the action + the enforced flag
        # so logs/metrics don't misstate the populate outcome.
        if du_enforced:
            logger.info(
                "snapshot_with_populate: default-user identity ensured on %s (action=%s)",
                du_db,
                default_user_action,
            )
        else:
            logger.info(
                "snapshot_with_populate: default-user step complete on %s "
                "(action=%s, enforcement disabled — identity not asserted)",
                du_db,
                default_user_action,
            )

    # ── Step 4: prune the source files the importer just consumed ─────────
    # Skipped entirely when ``config`` is ``None`` (correct for apps
    # whose state is purely DB-uploaded — they have no source files
    # to prune).
    pruned: list[Path] = []
    if config is not None:
        pruned = prune_imported_files(state_path, config)
        logger.info(
            "snapshot_with_populate: pruned %d source file(s) from %s",
            len(pruned),
            state_path,
        )
    else:
        logger.debug("snapshot_with_populate: no config provided — skipping step 4 prune")

    # ── Step 5: write the clean canonical via the runtime facade ──────────
    # Forward the runtime override so direct-mode (runtime == canonical)
    # routes through snapshot_db_only's aliased branch (in-place
    # checkpoint + drops + VACUUM, no copy).
    written = snapshot_db_via_runtime(
        canonical_path,
        runtime=runtime,
        drop_tables=drop_tables,
        drop_virtual_tables=drop_virtual_tables,
    )
    logger.info("snapshot_with_populate: wrote clean canonical → %s", written)

    # ── Step 6: hand the runtime back to the live server (RUNTIME mode) ────
    # Populate may have run as a different uid than the live server; the runtime
    # DB it (re)created can be un-writable by the server, poisoning its pool with
    # a read-only connection. Relax perms + drive one more checkpoint so the
    # server drops the poisoned connection and reopens RW. Only meaningful in
    # RUNTIME mode: an aliased/direct binding has no separate tmpfs runtime, and
    # memory mode short-circuited far above. Best-effort inside the helper.
    handoff_ran = False
    if server_base is not None and not aliased_mode:
        from mcp_middleware.runtime_db.lifecycle import handoff_runtime_to_server

        if runtime is not None:
            handoff_runtime = _Path(runtime).expanduser().resolve()
        else:
            handoff_runtime = runtime_paths_for(canonical_path).runtime
        handoff_runtime_to_server(handoff_runtime, f"{server_base}/checkpoint")
        handoff_ran = True

    return SnapshotHookResult(
        canonical=written,
        harvested=harvested,
        import_rc=import_rc,
        index_built=index_built,
        pruned=pruned,
        import_skipped_reason=import_skipped_reason,
        post_harvest_ran=post_harvest_ran,
        overridden=overridden,
        drained=drained,
        handoff_ran=handoff_ran,
        default_user_action=default_user_action,
    )


def _source_db_path(db: DbSource) -> str | None:
    """Return the SQLite file path backing ``db``, or None if non-file."""
    from sqlalchemy.ext.asyncio import AsyncEngine as _AsyncEngine

    if isinstance(db, Engine):
        url = db.url
    elif isinstance(db, _AsyncEngine):
        url = db.url
    else:
        from sqlalchemy.engine import make_url

        url = make_url(str(db))

    if not url.drivername.startswith("sqlite"):
        return None
    database = url.database
    if not database or database == ":memory:":
        return None
    return database


def _drop_computed_on_dest(
    dst_path: Path,
    *,
    drop_tables: list[str],
    drop_virtual_tables: bool,
) -> None:
    """Drop virtual + named regular tables on the snapshot copy.

    Runs entirely on ``dst_path`` so the source engine never sees these
    DDLs. sqlite-vec is loaded so ``DROP TABLE`` on a vec0 table can
    call its ``xDestroy`` — if sqlite-vec is unavailable AND the
    destination contains vec0 tables, the function raises rather than
    silently leaving the index in the snapshot.
    """
    import sqlite3

    conn = sqlite3.connect(str(dst_path), timeout=30.0)
    try:
        # Discover which virtual tables exist on the destination. We
        # need this list before attempting any extension load so we can
        # decide whether the load *needs* to succeed.
        virtual_names = (
            [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE sql IS NOT NULL AND upper(sql) LIKE 'CREATE VIRTUAL%'"
                )
            ]
            if drop_virtual_tables
            else []
        )

        # Best-effort vec0 module load. The extension is only required
        # when (a) drop_virtual_tables=True AND (b) a vec0 virtual table
        # actually exists. Detecting the second condition via the SQL
        # prefix lets us skip the noisy load entirely on FTS5-only DBs.
        needs_sqlite_vec = drop_virtual_tables and any(
            _is_vec0_table(conn, name) for name in virtual_names
        )
        vec_load_failure: Exception | None = None
        if needs_sqlite_vec:
            try:
                import sqlite_vec  # type: ignore[import-not-found]

                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)
            except Exception as exc:  # noqa: BLE001 - we'll re-raise below with context
                vec_load_failure = exc

        virtual_dropped: list[str] = []
        virtual_failed: list[tuple[str, Exception]] = []
        if drop_virtual_tables:
            for name in virtual_names:
                esc = name.replace('"', '""')
                try:
                    conn.execute(f'DROP TABLE IF EXISTS "{esc}"')
                    virtual_dropped.append(name)
                except sqlite3.Error as exc:
                    virtual_failed.append((name, exc))
            if virtual_dropped:
                conn.commit()
                # Drop triggers whose body references a dropped virtual
                # table. They live as independent sqlite_master objects
                # and are NOT cleaned up by xDestroy on the table itself.
                dropped_set = set(virtual_dropped)
                for trig_name, trig_sql in conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL"
                ):
                    upper_sql = trig_sql.upper()
                    if any(vt.upper() in upper_sql for vt in dropped_set):
                        esc = trig_name.replace('"', '""')
                        try:
                            conn.execute(f'DROP TRIGGER IF EXISTS "{esc}"')
                        except sqlite3.Error as exc:
                            virtual_failed.append((f"trigger:{trig_name}", exc))
                conn.commit()

        if drop_tables:
            for name in drop_tables:
                esc = name.replace('"', '""')
                try:
                    conn.execute(f'DROP TABLE IF EXISTS "{esc}"')
                except sqlite3.Error as exc:
                    # Regular-table drops also fail loud — if the caller
                    # asked us to drop docvec_documents and we can't,
                    # they need to know so the snapshot isn't shipped
                    # with stale computed state.
                    virtual_failed.append((name, exc))
            conn.commit()

        # Surface every drop failure together so the caller sees the
        # full picture (which tables, which errors). Silently shipping
        # the index — the old behaviour — let snapshots ship with FTS5
        # / vec0 still present and was the bug we're fixing here.
        if virtual_failed:
            details = "; ".join(f"{name} ({exc})" for name, exc in virtual_failed)
            hint = ""
            if vec_load_failure is not None:
                hint = (
                    " — likely cause: sqlite-vec could not be loaded "
                    f"({vec_load_failure}); ensure the package is installed "
                    "and SQLite's extension loading is enabled"
                )
            raise RuntimeError(
                f"snapshot_db_only: failed to drop {len(virtual_failed)} "
                f"computed table(s) on {dst_path} — snapshot is incomplete: "
                f"{details}{hint}"
            )

        # Fold the DROP commits' journal into the main file so the
        # produced .db is self-contained even if SQLite's auto-cleanup
        # didn't fire.
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
    finally:
        conn.close()


def _is_vec0_table(conn, name: str) -> bool:  # type: ignore[no-untyped-def]
    """Return True if ``name`` is a ``CREATE VIRTUAL TABLE ... USING vec0`` table.

    Cheap string check on the stored DDL. ``sqlite-vec`` produces table
    definitions whose SQL contains ``USING vec0`` (case-insensitive); we
    use that to decide whether the extension *must* be loaded before we
    can drop it.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    if not row or not row[0]:
        return False
    return "USING VEC0" in row[0].upper()


def prune_imported_files(
    state_dir: str | Path,
    config: SnapshotConfig,
) -> list[Path]:
    """Delete every file in ``state_dir`` that the importer just consumed.

    **Call this immediately after populate**, not at snapshot time. The
    importer has just written its content into the DB; the source files
    are now redundant. Pruning here means:

    * The next snapshot ships only the ``.db`` (via
      :func:`snapshot_db_only`) plus whatever the agent actually
      *creates* during the task — no leftover source CSVs.
    * Agent-created files matching an entity's import shape are
      **preserved**. They appear in ``state_dir`` AFTER this function
      runs, so the next snapshot ships them. Pruning at snapshot time
      instead would delete those agent artefacts, silently losing
      task output.

    Coverage (every file :func:`discover_snapshot` would route to an
    entity, regardless of format):

    * Files matched by an entity's ``files:`` glob — including CSVs,
      JSONs, and binary documents (PDFs, DOCX, …) registered via the
      ``mcp_files`` reader.
    * Files matched by header-signature detection
      (:func:`detect_entity_type`).
    * Multi-entity reader files (one file carrying many tables).

    Files in the ``_unrecognized`` bucket — i.e. nothing in
    ``config.entities`` claims them — are **left alone**. Sidecar files
    the snapshot itself produces (``workspace.db``, ``workspace.db.srcmeta``,
    ``database_dump.sql``, manifests, …) are not declared as import
    sources so they survive.

    Args:
        state_dir: Directory the snapshot was written to.
        config: Same :class:`SnapshotConfig` the next populate will use.
            Matching is driven by ``config.entities[*].files`` and
            ``config.sources``; mismatched configs will under- or
            over-prune accordingly.

    Returns:
        Sorted list of absolute :class:`Path` objects for the files that
        were deleted. Useful for log lines and post-snapshot assertions.

    Best-effort delete: a per-file failure (locked / vanished /
    permission denied) is logged and skipped rather than aborting — a
    partially-pruned snapshot is strictly better than a broken one, and
    the caller can re-run the function (it's idempotent — deleting an
    already-deleted file is a no-op).
    """
    from pathlib import Path as _Path

    from .importer import discover_snapshot

    state = _Path(state_dir).expanduser().resolve()
    if not state.is_dir():
        # Empty / missing dir → nothing to prune; treat as no-op rather
        # than raising. snapshot_db_only callers may legitimately point
        # at a state_dir that doesn't exist yet (offline test fixtures).
        return []

    discovered = discover_snapshot(state, config)

    deleted: list[Path] = []
    for bucket, entries in discovered.items():
        if bucket == "_unrecognized":
            # Sidecars / unknown files survive — they weren't going to be
            # imported anyway, and a snapshot may legitimately ship them
            # (manifest.json, database_dump.sql, .srcmeta provenance).
            continue
        for entry in entries:
            # discover_snapshot tuples are (filename, rel_path, content).
            # rel_path is the canonical key — reconstruct the absolute
            # path from state_dir so nested layouts work correctly.
            rel_str = entry[1] if len(entry) >= 2 else entry[0]
            target = state / rel_str
            try:
                target.unlink()
            except FileNotFoundError:
                # Vanished between discover_snapshot and unlink — fine,
                # it's already gone.
                continue
            except OSError as exc:
                logger.warning("prune_imported_files: could not delete %s: %s", target, exc)
                continue
            deleted.append(target)

    return sorted(deleted)


def export_snapshot_zip_sync(
    db: DbSource,
    config: SnapshotConfig,
    *,
    entities: list[str] | None = None,
) -> bytes:
    """Synchronous :func:`export_snapshot_zip`.

    Like :func:`export_snapshot_sync`, but returns a ZIP archive
    (``bytes``) containing one CSV per entity instead of writing files
    to disk. Convenient for in-memory pipelines or for serving a
    snapshot from an HTTP endpoint.

    Args:
        db: Database URL, sync ``Engine``, or ``AsyncEngine``.
        config: Snapshot configuration.
        entities: Optional subset of entity names. ``None`` = all.

    Returns:
        ZIP archive bytes (``application/zip``) — empty entities are
        excluded from the archive.
    """

    async def _op() -> bytes:
        engine, created = _resolve_engine(db)
        try:
            return await export_snapshot_zip(engine, config, entities=entities)
        finally:
            if created:
                await engine.dispose()

    return _run(_op())
