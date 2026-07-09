"""Move stale ``.db`` files out of a snapshot / state directory.

Use this at the **start of populate** so two invariants hold throughout
the populate → snapshot cycle:

1. **Pre-built DBs in ``state_dir`` become available as runtime DBs**
   in ``/tmp`` (typically tmpfs — random writes on gp2 EBS are capped
   around 300 IOPS while tmpfs is pure RAM). An uploaded
   ``workspace.db``, a ``docvec.db`` from a previous container, etc.,
   all land where the server / index builders expect to find them.

2. **``state_dir`` is left clean of ``.db`` files**. This is the
   load-bearing half: ``state_dir`` is typically also the snapshot
   *output* directory, so anything left behind here gets shipped in the
   next snapshot — including transient sidecars (``docvec.db``) the
   snapshot should never contain, and a stale ``workspace.db`` whose
   FTS5 / vec0 indexing was baked in by a previous populate.

After ``harvest_db_files`` runs, the canonical write-path is:

* Server boots, runs ``cold_seed_runtime`` (no-op — runtime DB is
  already at the resolved runtime path).
* Tasks mutate the runtime DB in place.
* Snapshot phase calls :func:`mcp_middleware.csv_engine.sync.snapshot_db_only`
  to write a *fresh* clean ``.db`` into ``state_dir`` (just the main DB,
  WAL folded, FTS5 dropped on the copy). ``docvec.db`` is never produced
  in ``state_dir`` — it lives only at the runtime path.

Each ``foo.db`` is moved together with its WAL-mode sidecars
(``foo.db-wal``, ``foo.db-shm``) and the
:mod:`mcp_middleware.runtime_db.paths` provenance marker
(``foo.db.srcmeta``). Moving them independently would let a later open
create a fresh ``-shm`` that no longer matches the moved ``-wal`` (salt
mismatch → ``SQLITE_CORRUPT``).

Concurrency contract: the caller MUST guarantee no live process holds
the source DBs open at harvest time. SQLite uses POSIX byte-range
locks (``fcntl.lockf``) rather than advisory ``flock``, so a flock
sentinel here would NOT catch a server's open SQLite fd. The race the
caller must prevent: a server's connection pool holds a memory map
into ``source_dir/foo.db-shm``; harvest renames the file to a new path
(``mv`` on Linux preserves the inode for held fds, but creates a new
inode at the source path); the server now reads stale state until its
pool turns over. The orchestration fix: drain the server's pool via
the shared ``/_internal/checkpoint`` endpoint immediately before
calling harvest, or restart the server immediately after.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Iterable
from pathlib import Path

from .paths import fingerprint_canonical, runtime_paths_for

__all__ = ["harvest_db_files"]

logger = logging.getLogger(__name__)

# SQLite sidecar suffixes (WAL mode + our provenance marker). Order
# matters for logging only; semantics are the same either way.
_SIDECAR_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", ".srcmeta")


def harvest_db_files(
    source_dir: str | os.PathLike[str],
    *,
    dst_dir: str | os.PathLike[str] | None = None,
    recursive: bool = False,
    protect_paths: Iterable[str | os.PathLike[str]] | None = None,
) -> list[tuple[Path, Path]]:
    """Move every ``*.db`` (+ sidecars) out of ``source_dir``.

    Args:
        source_dir: Directory to scan — typically ``STATE_LOCATION`` /
            the snapshot directory. A missing directory is treated as
            empty (no-op, returns ``[]``); safer than raising for the
            populate-time pre-step that may run before any state exists.
        dst_dir: Destination policy.

            * ``None`` (default, **recommended**): each ``.db`` lands at
              its own collision-free hashed runtime path via
              :func:`runtime_paths_for(source_dir/<name>.db).runtime`.
              An ``app-A/workspace.db`` and ``app-B/workspace.db`` on
              the same host get distinct destinations because the hash
              embeds the full canonical source path. The server then
              locates the moved file via
              :func:`resolve_runtime_path(source_dir/<name>.db)`.

            * A directory path: each ``.db`` is moved to
              ``dst_dir / <name>.db`` (flat layout, original filename
              preserved). Use only when you control all consumers and
              can guarantee filename uniqueness on the host.

            The hashed default avoids the multi-app collision that
            plain ``/tmp/workspace.db`` would cause when several MCP
            servers share a host.
        recursive: When ``True``, walk ``source_dir`` recursively. Default
            ``False`` — most snapshot layouts keep DBs at the top level,
            and recursive scans pick up unwanted matches (test fixtures,
            extracted archives, …).
        protect_paths: Optional iterable of absolute paths the harvest
            MUST NOT touch. Any ``.db`` whose resolved path matches an
            entry is skipped (sidecars too). Use this when the live
            engine is bound directly to a path under ``source_dir`` —
            e.g. :class:`~mcp_middleware.runtime_db.bind_engine`'s
            "direct" mode where ``binding.runtime`` IS the canonical
            sitting in ``state_dir``. Without the guard, harvest would
            move the live DB out from under the engine and the next
            request would open a freshly-created empty file. The
            existing in-loop ``src.resolve() == dst.resolve()`` check
            protects against a same-source-and-dest move, but doesn't
            cover this case (dst is a different hashed ``/tmp`` path).

    Returns:
        Sorted list of ``(original_db_path, new_db_path)`` tuples — one
        entry per ``.db`` file moved. Sidecars are not in the returned
        list (they're implied — moved with their parent ``.db``).

    Collisions: when the destination already has a same-named file (e.g.
    stale ``/tmp/<stem>_runtime_<hash>.db`` from a previous container),
    the source wins — ``shutil.move`` overwrites the destination. Source
    is the fresh state; clobbering the stale ``/tmp`` copy is the
    desired behaviour at populate start.

    Best-effort sidecars: a missing or unreadable sidecar is logged at
    DEBUG and skipped. The main ``.db`` move is the only thing that
    determines the function's return value.
    """
    src = Path(os.fspath(source_dir)).expanduser()
    if not src.is_dir():
        # Pre-populate state dir may not exist yet (blank world). Treat
        # as no-op rather than raising — this function is the *first*
        # populate step in the recommended flow.
        return []

    # Resolve protect_paths once. We compare against absolute resolved
    # paths inside the loop; entries that fail resolve() (e.g. broken
    # symlink) are dropped silently — they couldn't equal any real file
    # we're about to move anyway.
    protected: set[Path] = set()
    if protect_paths is not None:
        for raw in protect_paths:
            try:
                protected.add(Path(os.fspath(raw)).resolve())
            except OSError:
                continue

    # rglob() walks subdirectories; glob() stays flat.
    walker = src.rglob if recursive else src.glob
    db_paths = sorted(p for p in walker("*.db") if p.is_file())

    # Resolve a fixed dst directory once (flat-layout mode). For hashed
    # mode (dst_dir is None) we resolve per-DB below — each .db gets its
    # own hashed runtime path keyed off its source.
    fixed_dst: Path | None = None
    if dst_dir is not None:
        fixed_dst = Path(os.fspath(dst_dir))
        fixed_dst.mkdir(parents=True, exist_ok=True)

    moved: list[tuple[Path, Path]] = []
    for db_src in db_paths:
        # Aliased-mode guard: skip any source whose resolved path is the
        # live engine's bound file. Direct-mode bindings put canonical
        # under state_dir and the engine reads it in place; moving it
        # here would obliterate the live DB on the engine's next access.
        if protected:
            try:
                resolved_src = db_src.resolve()
            except OSError:
                resolved_src = db_src
            if resolved_src in protected:
                logger.info(
                    "harvest_db_files: skipping %s — protected (live engine bound here)",
                    db_src,
                )
                continue

        if fixed_dst is not None:
            # Flat layout: preserve original filename inside dst_dir.
            db_dst = fixed_dst / db_src.name
        else:
            # Hashed-runtime layout (default): each .db lands at its own
            # collision-free path so two apps on the same host don't both
            # write to /tmp/workspace.db. The hash is computed off the
            # full source path, so app-A/workspace.db ≠ app-B/workspace.db.
            db_dst = runtime_paths_for(db_src).runtime
            db_dst.parent.mkdir(parents=True, exist_ok=True)

        # If the source and destination resolve to the same file, skip:
        # the caller passed dst_dir == source_dir, which would `mv x → x`
        # and is almost certainly a bug.
        try:
            if db_src.resolve() == db_dst.resolve():
                logger.warning(
                    "harvest_db_files: src %s and dst %s resolve to the same "
                    "path — skipping (caller passed dst_dir==source_dir)",
                    db_src,
                    db_dst,
                )
                continue
        except OSError:
            # resolve() of a non-existent dst path can raise on some
            # platforms; we already created the parent dir above so this
            # should be rare. Fall through to the move and let it error loudly.
            pass

        # Capture the source fingerprint BEFORE the move — after
        # ``shutil.move`` the source path is gone. We stamp the marker
        # with this fingerprint AFTER the move so step 0 of
        # ``snapshot_with_populate`` can distinguish three runtime states:
        #
        #   * marker matches state_dir/<canonical> fp → runtime is a
        #     faithful copy of canonical
        #   * marker mismatches state_dir/<canonical> fp → runtime was
        #     sync'd from a canonical that's since changed (e.g. step 5
        #     wrote a clean canonical or the SME re-uploaded one), so
        #     runtime is the authoritative source → step 0 deletes
        #     state_dir/<canonical> to prevent harvest from clobbering
        #     the runtime
        #   * marker MISSING → no harvest/cold-seed has ever placed
        #     data at this runtime path → runtime may be empty (the
        #     Modal boot-race case where the live server lazy-created
        #     an empty DB before SME mount) → step 0 defers to
        #     canonical, doesn't delete
        #
        # The third case is exactly the Atlassian / FGW production
        # symptom this stamp-on-harvest closes off: harvest moving
        # canonical → runtime is one of the two ways the runtime gets
        # authoritatively populated (cold_seed_runtime being the other),
        # so it must stamp the marker the same way.
        try:
            src_fp = fingerprint_canonical(db_src)
        except OSError as exc:
            logger.debug("harvest_db_files: could not fingerprint src %s: %s", db_src, exc)
            src_fp = None

        try:
            shutil.move(str(db_src), str(db_dst))
        except OSError as exc:
            logger.warning("harvest_db_files: could not move %s → %s: %s", db_src, db_dst, exc)
            continue

        # Move sidecars together so WAL stays paired with its DB. A
        # sidecar move failure isn't fatal — log and continue. WAL mode
        # is self-healing on a missing -wal (SQLite starts a new one
        # from a clean state); a stale -shm next to a fresh -wal is the
        # only truly bad case, and that requires both to exist.
        #
        # Critically: when the SOURCE has no sidecar (the common case — an
        # uploaded/snapshot ``workspace.db`` ships WAL-folded with no
        # ``-wal``/``-shm``) but a STALE sidecar survives at the destination
        # from a previous occupant of this runtime path, we must DROP it.
        # The classic trigger: the server cold-seeded an empty runtime DB at
        # boot (canonical not present yet), opened it in WAL mode, and left a
        # ``-shm``/``-wal`` describing that empty database. If we move only
        # the main ``.db`` over it, SQLite re-attaches the stale ``-shm`` on
        # the next open and serves the OLD (empty) database's pages — the
        # freshly-moved DB is shadowed and every read comes back empty. This
        # mirrors the orphan-sidecar drop that ``cold_seed_runtime`` already
        # performs on its copy path.
        for suffix in _SIDECAR_SUFFIXES:
            sidecar_src = db_src.with_name(db_src.name + suffix)
            sidecar_dst = db_dst.with_name(db_dst.name + suffix)
            if sidecar_src.exists():
                try:
                    shutil.move(str(sidecar_src), str(sidecar_dst))
                except OSError as exc:
                    logger.debug(
                        "harvest_db_files: could not move sidecar %s: %s",
                        sidecar_src,
                        exc,
                    )
            elif sidecar_dst.exists():
                # Source has no such sidecar but the destination carries a
                # stale one — drop it so the moved DB stands alone (see the
                # block comment above for why a surviving -shm corrupts reads).
                try:
                    sidecar_dst.unlink()
                    logger.info(
                        "harvest_db_files: dropped stale dst sidecar %s "
                        "(source had none — prevents stale-shm shadowing)",
                        sidecar_dst,
                    )
                except OSError as exc:
                    logger.debug(
                        "harvest_db_files: could not drop stale dst sidecar %s: %s",
                        sidecar_dst,
                        exc,
                    )

        # Stamp the marker so step 0 of ``snapshot_with_populate`` can
        # reliably distinguish a faithfully-sync'd runtime from one
        # that was lazy-created empty by SQLAlchemy (the boot race).
        # We do this only when the destination is the hashed runtime
        # path for this source — when the caller routes through a
        # ``fixed_dst``, the marker convention is theirs to own. See
        # the block comment above the ``shutil.move`` call for the
        # three-state semantic this enables.
        if src_fp is not None and fixed_dst is None:
            marker_path = db_dst.with_name(db_dst.name + ".srcmeta")
            try:
                marker_path.write_text(src_fp, encoding="utf-8")
            except OSError as exc:
                logger.debug(
                    "harvest_db_files: could not stamp marker %s: %s",
                    marker_path,
                    exc,
                )

        moved.append((db_src, db_dst))
        logger.info("harvest_db_files: moved %s → %s", db_src, db_dst)

    return moved
